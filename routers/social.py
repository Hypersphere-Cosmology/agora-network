"""
Agora — social router
Profile walls and direct messaging.
"""

import httpx
import socket
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import get_current_user
from db import get_db, User, ProfilePost, DirectMessage

router = APIRouter(tags=["social"])


def _get_my_ip() -> str:
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relay_to_peers(sender_handle: str, recipient_handle: str,
                    content: str, thread_id: str, sent_at: str):
    """Synchronously relay DM to all known peer nodes (fire-and-forget).
    Skips peers on the same machine to prevent duplicate storage."""
    try:
        from routers.federation import load_registry
        reg = load_registry()
        my_ip = _get_my_ip()
        for node_id, node in reg.get("nodes", {}).items():
            peer_url = node.get("public_url", "")
            if not peer_url:
                continue
            # Skip peers on the same machine — both nodes share same users via federation sync
            try:
                parsed = urlparse(peer_url)
                peer_ip = parsed.hostname or ""
                if peer_ip == my_ip or peer_ip in ("localhost", "127.0.0.1"):
                    continue
            except Exception:
                pass
            try:
                with httpx.Client(timeout=5) as client:
                    client.post(f"{peer_url}/federation/relay-dm", json={
                        "sender_handle": sender_handle,
                        "recipient_handle": recipient_handle,
                        "content": content,
                        "thread_id": thread_id,
                        "sent_at": sent_at,
                    })
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class WallPostBody(BaseModel):
    content: str
    parent_id: Optional[int] = None


class DMBody(BaseModel):
    content: str
    thread_id: Optional[str] = None


class ReadThreadBody(BaseModel):
    thread_id: str


class RelayDMBody(BaseModel):
    sender_handle: str
    recipient_handle: str
    content: str
    thread_id: str
    sent_at: str


# ---------------------------------------------------------------------------
# Profile wall endpoints
# ---------------------------------------------------------------------------

@router.get("/users/{handle}/wall")
def get_wall(handle: str, db: Session = Depends(get_db)):
    """Returns posts on a user's profile wall."""
    posts = (
        db.query(ProfilePost)
        .filter(ProfilePost.target_handle == handle, ProfilePost.is_deleted == False)
        .order_by(ProfilePost.posted_at.desc())
        .all()
    )

    result = []
    for p in posts:
        author = db.query(User).filter(User.id == p.author_id).first()
        reply_count = (
            db.query(ProfilePost)
            .filter(ProfilePost.parent_id == p.id, ProfilePost.is_deleted == False)
            .count()
        )
        result.append({
            "id": p.id,
            "author_handle": author.handle if author else "unknown",
            "content": p.content,
            "posted_at": p.posted_at.isoformat() if p.posted_at else None,
            "reply_count": reply_count,
            "parent_id": p.parent_id,
        })

    return {"handle": handle, "posts": result}


@router.post("/users/{handle}/wall")
def post_to_wall(
    handle: str,
    body: WallPostBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Post on someone's wall. Auth required."""
    if len(body.content) > 2000:
        raise HTTPException(status_code=400, detail="Content exceeds 2000 character limit")

    # Rate limit: 100 DMs per sender per rolling 24h
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_count = db.query(DirectMessage).filter(
        DirectMessage.sender_id == current_user.id,
        DirectMessage.sent_at >= cutoff
    ).count()
    if recent_count >= 100:
        raise HTTPException(status_code=429, detail="DM limit reached: 100 messages per 24 hours.")

    # Ensure target user exists
    target = db.query(User).filter(User.handle == handle).first()
    if not target:
        raise HTTPException(status_code=404, detail=f"User @{handle} not found")

    post = ProfilePost(
        target_handle=handle,
        author_id=current_user.id,
        content=body.content,
        parent_id=body.parent_id,
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    return {
        "id": post.id,
        "author_handle": current_user.handle,
        "content": post.content,
        "posted_at": post.posted_at.isoformat() if post.posted_at else None,
        "reply_count": 0,
        "parent_id": post.parent_id,
    }


@router.delete("/users/{handle}/wall/{post_id}")
def delete_wall_post(
    handle: str,
    post_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Soft-delete a wall post. Only author or wall owner can delete."""
    post = db.query(ProfilePost).filter(
        ProfilePost.id == post_id,
        ProfilePost.target_handle == handle,
        ProfilePost.is_deleted == False,
    ).first()

    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    author = db.query(User).filter(User.id == post.author_id).first()
    is_author = author and author.id == current_user.id
    is_wall_owner = current_user.handle == handle

    if not (is_author or is_wall_owner):
        raise HTTPException(status_code=403, detail="Only the post author or wall owner can delete this post")

    post.is_deleted = True
    db.commit()
    return {"ok": True, "post_id": post_id}


# ---------------------------------------------------------------------------
# Direct message endpoints
# ---------------------------------------------------------------------------

@router.get("/users/{handle}/inbox")
def get_inbox(
    handle: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get DMs for a user. Auth required — must be handle owner."""
    if current_user.handle != handle:
        raise HTTPException(status_code=403, detail="You can only read your own inbox")

    messages = (
        db.query(DirectMessage)
        .filter(DirectMessage.recipient_handle == handle)
        .order_by(DirectMessage.sent_at.asc())
        .all()
    )

    # Also include messages sent by this user (outbox), grouped with their threads
    sent = (
        db.query(DirectMessage)
        .filter(DirectMessage.sender_id == current_user.id)
        .order_by(DirectMessage.sent_at.asc())
        .all()
    )

    # Group by thread_id
    threads: dict = {}

    def _add_to_thread(msg, role):
        tid = msg.thread_id or f"direct_{msg.sender_id}_{msg.recipient_handle}"
        sender = db.query(User).filter(User.id == msg.sender_id).first()
        sender_handle = sender.handle if sender else "unknown"

        if role == "received":
            other_party = sender_handle
        else:
            other_party = msg.recipient_handle

        if tid not in threads:
            threads[tid] = {
                "thread_id": tid,
                "other_party": other_party,
                "last_message": "",
                "unread": 0,
                "messages": [],
            }

        threads[tid]["last_message"] = msg.content
        if role == "received" and not msg.is_read:
            threads[tid]["unread"] += 1

        threads[tid]["messages"].append({
            "id": msg.id,
            "sender_handle": sender_handle,
            "content": msg.content,
            "sent_at": msg.sent_at.isoformat() if msg.sent_at else None,
            "is_read": msg.is_read,
        })

    for msg in messages:
        _add_to_thread(msg, "received")
    for msg in sent:
        _add_to_thread(msg, "sent")

    return {"threads": list(threads.values())}


@router.post("/users/{handle}/dm")
def send_dm(
    handle: str,
    body: DMBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send a DM to a user. Auth required."""
    if len(body.content) > 2000:
        raise HTTPException(status_code=400, detail="Content exceeds 2000 character limit")

    # Rate limit: 100 DMs per sender per rolling 24h
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_count = db.query(DirectMessage).filter(
        DirectMessage.sender_id == current_user.id,
        DirectMessage.sent_at >= cutoff
    ).count()
    if recent_count >= 100:
        raise HTTPException(status_code=429, detail="DM limit reached: 100 messages per 24 hours.")

    # Ensure recipient exists locally (may be on another node — we still store it)
    recipient = db.query(User).filter(User.handle == handle).first()

    sent_at = datetime.now(timezone.utc)

    # Generate thread_id if not provided
    thread_id = body.thread_id
    if not thread_id:
        thread_id = f"{current_user.handle}_{handle}_{int(sent_at.timestamp())}"

    msg = DirectMessage(
        sender_id=current_user.id,
        recipient_handle=handle,
        content=body.content,
        sent_at=sent_at,
        is_read=False,
        thread_id=thread_id,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    # Relay to peer nodes (fire-and-forget)
    _relay_to_peers(
        sender_handle=current_user.handle,
        recipient_handle=handle,
        content=body.content,
        thread_id=thread_id,
        sent_at=sent_at.isoformat(),
    )

    return {
        "id": msg.id,
        "sender_handle": current_user.handle,
        "recipient_handle": handle,
        "content": msg.content,
        "sent_at": msg.sent_at.isoformat(),
        "thread_id": thread_id,
        "is_read": False,
    }


@router.post("/users/{handle}/inbox/read")
def mark_thread_read(
    handle: str,
    body: ReadThreadBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark all messages in a thread as read. Auth required — must be recipient."""
    if current_user.handle != handle:
        raise HTTPException(status_code=403, detail="You can only mark your own messages as read")

    updated = (
        db.query(DirectMessage)
        .filter(
            DirectMessage.recipient_handle == handle,
            DirectMessage.thread_id == body.thread_id,
            DirectMessage.is_read == False,
        )
        .all()
    )
    for msg in updated:
        msg.is_read = True
    db.commit()

    return {"ok": True, "marked_read": len(updated)}


# ---------------------------------------------------------------------------
# Federation relay endpoint
# ---------------------------------------------------------------------------

@router.post("/federation/relay-dm")
def relay_dm(body: RelayDMBody, db: Session = Depends(get_db)):
    """
    Receive a relayed DM from another node.
    No auth — node-to-node. Find recipient locally and store message.
    """
    recipient = db.query(User).filter(User.handle == body.recipient_handle).first()
    if not recipient:
        # Recipient not on this node — silently ignore
        return {"ok": True, "stored": False, "reason": "recipient not on this node"}

    # Find or create a placeholder sender (may not exist locally)
    sender = db.query(User).filter(User.handle == body.sender_handle).first()
    sender_id = sender.id if sender else recipient.id  # fallback to self (edge case)

    # Avoid duplicate relay storms — check if message already exists
    try:
        sent_at = datetime.fromisoformat(body.sent_at.replace("Z", "+00:00"))
    except Exception:
        sent_at = datetime.now(timezone.utc)

    existing = (
        db.query(DirectMessage)
        .filter(
            DirectMessage.thread_id == body.thread_id,
            DirectMessage.recipient_handle == body.recipient_handle,
            DirectMessage.content == body.content,
        )
        .first()
    )
    if existing:
        return {"ok": True, "stored": False, "reason": "duplicate"}

    msg = DirectMessage(
        sender_id=sender_id,
        recipient_handle=body.recipient_handle,
        content=body.content,
        sent_at=sent_at,
        is_read=False,
        thread_id=body.thread_id,
    )
    db.add(msg)
    db.commit()

    return {"ok": True, "stored": True}
