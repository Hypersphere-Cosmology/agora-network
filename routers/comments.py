"""
Agora — comments router
Discussion threads on assets and bounties.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from db import get_db, Comment, User, Asset, Listing
from auth import get_current_user, get_current_user_optional
from notifications import notify
from ratelimit import limiter

router = APIRouter(prefix="/comments", tags=["comments"])


class CommentCreate(BaseModel):
    content: str


def _validate_thread(thread_type: str, thread_id: int, db: Session):
    if thread_type == "asset":
        obj = db.query(Asset).filter(Asset.id == thread_id, Asset.is_deleted == False).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Asset not found")
        return obj
    elif thread_type == "bounty":
        obj = db.query(Listing).filter(Listing.id == thread_id, Listing.asset_id == None).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Bounty not found")
        return obj
    else:
        raise HTTPException(status_code=422, detail="thread_type must be 'asset' or 'bounty'")


@router.get("/{thread_type}/{thread_id}")
def get_comments(thread_type: str, thread_id: int, db: Session = Depends(get_db)):
    """Get all comments on an asset or bounty thread."""
    _validate_thread(thread_type, thread_id, db)
    comments = (
        db.query(Comment)
        .filter(
            Comment.thread_type == thread_type,
            Comment.thread_id == thread_id,
            Comment.is_deleted == False,
        )
        .order_by(Comment.created_at.asc())
        .all()
    )
    return [
        {
            "id": c.id,
            "author": c.author.handle if c.author else "?",
            "content": c.content,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in comments
    ]


@router.post("/{thread_type}/{thread_id}", status_code=201)
@limiter.limit("60/hour")
def post_comment(
    request: Request,
    thread_type: str,
    thread_id: int,
    payload: CommentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Post a comment on an asset or bounty thread."""
    if not payload.content or not payload.content.strip():
        raise HTTPException(status_code=422, detail="Comment cannot be empty")
    if len(payload.content) > 2000:
        raise HTTPException(status_code=422, detail="Comment too long (max 2000 chars)")

    thread_obj = _validate_thread(thread_type, thread_id, db)

    comment = Comment(
        thread_type=thread_type,
        thread_id=thread_id,
        author_id=current_user.id,
        content=payload.content.strip(),
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)

    # Notify the asset/bounty owner if different from commenter
    owner_id = None
    if thread_type == "asset" and hasattr(thread_obj, "submitter_id"):
        owner_id = thread_obj.submitter_id
    elif thread_type == "bounty" and hasattr(thread_obj, "seller_id"):
        owner_id = thread_obj.seller_id

    if owner_id and owner_id != current_user.id:
        label = f"asset #{thread_id}" if thread_type == "asset" else f"bounty #{thread_id}"
        notify(db, owner_id, "comment",
               f"{current_user.handle} commented on your {label}.")
        db.commit()

    return {
        "id": comment.id,
        "author": current_user.handle,
        "content": comment.content,
        "created_at": comment.created_at.isoformat(),
    }


@router.delete("/{comment_id}", status_code=200)
def delete_comment(
    comment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete your own comment."""
    c = db.query(Comment).filter(Comment.id == comment_id, Comment.is_deleted == False).first()
    if not c:
        raise HTTPException(status_code=404, detail="Comment not found")
    if c.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Can only delete your own comments")
    c.is_deleted = True
    db.commit()
    return {"deleted": comment_id}
