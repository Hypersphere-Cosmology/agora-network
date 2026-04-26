"""
Agora — Committee governance system.
Committees have delegated authority over specific domains.
Board (node operators) votes on committee actions.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone, timedelta
from db import get_db, Committee, CommitteeMember, CommitteeAction, BoardVote, User
from auth import get_current_user
from routers.federation import load_registry

router = APIRouter(prefix="/committees", tags=["committees"])

TERM_DAYS = 90  # default term length in days


def get_board_members() -> list:
    """Board = node operators. Returns list of operator handles."""
    reg = load_registry()
    board = ["viralsatan"]  # Node 1 operator always on board
    for node in reg.get("nodes", {}).values():
        handle = node.get("operator_handle", "")
        if handle and handle not in board:
            board.append(handle)
    return board


def board_required_votes(board: list) -> int:
    """Simple majority of board."""
    return max(1, (len(board) // 2) + 1)


# ── List & Get ────────────────────────────────────────────────────────────────

@router.get("")
def list_committees(db: Session = Depends(get_db)):
    committees = db.query(Committee).filter(Committee.is_active == True).all()
    result = []
    for c in committees:
        members = db.query(CommitteeMember).filter(
            CommitteeMember.committee_id == c.id,
            CommitteeMember.is_active == True
        ).all()
        result.append({
            "id": c.id,
            "name": c.name,
            "slug": c.slug,
            "description": c.description,
            "domain": c.domain,
            "charter": c.charter,
            "created_by": c.created_by,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "members": [{"handle": m.user_handle, "role": m.role, "term_ends_at": m.term_ends_at.isoformat() if m.term_ends_at else None} for m in members],
            "member_count": len(members),
        })
    return {"committees": result, "board": get_board_members()}


@router.get("/{slug}")
def get_committee(slug: str, db: Session = Depends(get_db)):
    c = db.query(Committee).filter(Committee.slug == slug, Committee.is_active == True).first()
    if not c:
        raise HTTPException(status_code=404, detail="Committee not found")
    members = db.query(CommitteeMember).filter(CommitteeMember.committee_id == c.id, CommitteeMember.is_active == True).all()
    actions = db.query(CommitteeAction).filter(CommitteeAction.committee_id == c.id).order_by(CommitteeAction.created_at.desc()).limit(20).all()
    return {
        "id": c.id, "name": c.name, "slug": c.slug, "description": c.description,
        "domain": c.domain, "charter": c.charter, "created_by": c.created_by,
        "members": [{"handle": m.user_handle, "role": m.role, "joined_at": m.joined_at.isoformat(), "term_ends_at": m.term_ends_at.isoformat() if m.term_ends_at else None} for m in members],
        "recent_actions": [{"id": a.id, "title": a.title, "type": a.action_type, "status": a.status, "created_at": a.created_at.isoformat()} for a in actions],
        "board": get_board_members(),
    }


# ── Propose an action ────────────────────────────────────────────────────────

class ActionCreate(BaseModel):
    title: str
    description: Optional[str] = None
    action_type: str = "proposal"  # proposal | decision | audit | review


@router.post("/{slug}/propose")
def propose_action(slug: str, payload: ActionCreate, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    c = db.query(Committee).filter(Committee.slug == slug, Committee.is_active == True).first()
    if not c:
        raise HTTPException(status_code=404, detail="Committee not found")
    # Must be a committee member
    member = db.query(CommitteeMember).filter(
        CommitteeMember.committee_id == c.id,
        CommitteeMember.user_handle == current_user.handle,
        CommitteeMember.is_active == True
    ).first()
    if not member:
        raise HTTPException(status_code=403, detail="Must be a committee member to propose actions")

    board = get_board_members()
    action = CommitteeAction(
        committee_id=c.id,
        action_type=payload.action_type,
        title=payload.title,
        description=payload.description,
        proposed_by=current_user.handle,
        board_required=board_required_votes(board),
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return {"ok": True, "action_id": action.id, "board_required": action.board_required, "board": board}


# ── Board vote on an action ───────────────────────────────────────────────────

class BoardVoteCreate(BaseModel):
    vote: str  # yes | no | abstain
    reason: Optional[str] = None


@router.post("/actions/{action_id}/vote")
def board_vote(action_id: int, payload: BoardVoteCreate, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    action = db.query(CommitteeAction).filter(CommitteeAction.id == action_id).first()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.status != "pending":
        raise HTTPException(status_code=409, detail=f"Action already {action.status}")

    board = get_board_members()
    if current_user.handle not in board:
        raise HTTPException(status_code=403, detail="Board members only. Operate a node to join the Board.")

    # Check already voted
    existing = db.query(BoardVote).filter(
        BoardVote.action_id == action_id,
        BoardVote.voter_handle == current_user.handle
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already voted on this action")

    # Get node_id for this operator
    reg = load_registry()
    node_id = "node_1" if current_user.handle == "viralsatan" else next(
        (nid for nid, n in reg.get("nodes", {}).items() if n.get("operator_handle") == current_user.handle),
        "unknown"
    )

    vote = BoardVote(
        action_id=action_id, node_id=node_id,
        voter_handle=current_user.handle,
        vote=payload.vote, reason=payload.reason
    )
    db.add(vote)

    if payload.vote == "yes":
        action.board_votes_for += 1
    elif payload.vote == "no":
        action.board_votes_against += 1

    # Check if resolved
    if action.board_votes_for >= action.board_required:
        action.status = "approved"
        action.resolved_at = datetime.now(timezone.utc)
    elif action.board_votes_against >= action.board_required:
        action.status = "rejected"
        action.resolved_at = datetime.now(timezone.utc)

    db.commit()
    return {
        "ok": True, "vote": payload.vote,
        "status": action.status,
        "votes_for": action.board_votes_for,
        "votes_against": action.board_votes_against,
        "required": action.board_required,
    }


# ── Add committee member (Board approves) ────────────────────────────────────

class MemberAdd(BaseModel):
    handle: str
    role: str = "member"  # member | head
    term_days: int = 90


@router.post("/{slug}/members")
def add_member(slug: str, payload: MemberAdd, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    board = get_board_members()
    if current_user.handle not in board:
        raise HTTPException(status_code=403, detail="Board members only can add committee members")

    c = db.query(Committee).filter(Committee.slug == slug, Committee.is_active == True).first()
    if not c:
        raise HTTPException(status_code=404, detail="Committee not found")

    # Check user exists
    user = db.query(User).filter(User.handle == payload.handle).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check not already member
    existing = db.query(CommitteeMember).filter(
        CommitteeMember.committee_id == c.id,
        CommitteeMember.user_handle == payload.handle,
        CommitteeMember.is_active == True
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already a member")

    term_ends = datetime.now(timezone.utc) + timedelta(days=payload.term_days)
    member = CommitteeMember(
        committee_id=c.id, user_handle=payload.handle,
        role=payload.role, approved_by=current_user.handle,
        term_ends_at=term_ends
    )
    db.add(member)
    db.commit()
    return {"ok": True, "member": payload.handle, "role": payload.role, "term_ends_at": term_ends.isoformat()}


# ── Remove/deactivate member ─────────────────────────────────────────────────

@router.delete("/{slug}/members/{handle}")
def remove_member(slug: str, handle: str, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    board = get_board_members()
    if current_user.handle not in board:
        raise HTTPException(status_code=403, detail="Board members only")

    c = db.query(Committee).filter(Committee.slug == slug).first()
    if not c:
        raise HTTPException(status_code=404, detail="Committee not found")

    member = db.query(CommitteeMember).filter(
        CommitteeMember.committee_id == c.id,
        CommitteeMember.user_handle == handle,
        CommitteeMember.is_active == True
    ).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    member.is_active = False
    db.commit()
    return {"ok": True, "removed": handle}


# ── Term renewal / performance review ────────────────────────────────────────

class TermRenewal(BaseModel):
    handle: str
    extend_days: int = 90
    performance_notes: Optional[str] = None


@router.post("/{slug}/renew")
def renew_term(slug: str, payload: TermRenewal, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    board = get_board_members()
    if current_user.handle not in board:
        raise HTTPException(status_code=403, detail="Board members only")

    c = db.query(Committee).filter(Committee.slug == slug).first()
    if not c:
        raise HTTPException(status_code=404, detail="Committee not found")

    member = db.query(CommitteeMember).filter(
        CommitteeMember.committee_id == c.id,
        CommitteeMember.user_handle == payload.handle,
        CommitteeMember.is_active == True
    ).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    new_term = datetime.now(timezone.utc) + timedelta(days=payload.extend_days)
    member.term_ends_at = new_term
    if payload.performance_notes:
        member.performance_notes = payload.performance_notes
    db.commit()
    return {"ok": True, "handle": payload.handle, "new_term_ends": new_term.isoformat()}
