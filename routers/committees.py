"""
Agora — Committee governance system.
Committees have delegated authority over specific domains.
Board (node operators) votes on committee actions.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from db import get_db, Committee, CommitteeMember, CommitteeAction, BoardVote, User
from auth import get_current_user
from routers.federation import load_registry

router = APIRouter(prefix="/committees", tags=["committees"])


def get_board_members() -> list:
    """Board = node operators. Returns list of operator handles."""
    reg = load_registry()
    board = ["viralsatan"]  # Node 1 operator always on board
    for node in reg.get("nodes", {}).values():
        handle = node.get("operator_handle", "")
        if handle and handle not in board:
            board.append(handle)
    return board


def board_quorum_threshold(board: list) -> float:
    """70% of ALL nodes must participate (yes + no + abstain) for quorum."""
    return len(board) * 0.7


def board_yes_threshold(board: list) -> float:
    """70% of ALL nodes must vote YES for a proposal to pass.
    Abstentions count toward quorum but NOT toward yes.
    Effectively: abstaining = voting no for the purpose of passage."""
    return len(board) * 0.7


# ── Board vote (MUST be before /{slug} routes) ────────────────────────────────

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

    # Record vote
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
    db.flush()

    # Tally all votes for this action
    all_votes = db.query(BoardVote).filter(BoardVote.action_id == action_id).all()
    yes_count = sum(1 for v in all_votes if v.vote == "yes")
    no_count = sum(1 for v in all_votes if v.vote == "no")
    abstain_count = sum(1 for v in all_votes if v.vote == "abstain")
    total_participated = yes_count + no_count + abstain_count
    total_board = len(board)

    threshold = total_board * 0.7  # 70% of all nodes

    # Quorum: 70% of all nodes must participate
    quorum_met = total_participated >= threshold

    # Check resolution
    remaining_possible_yes = total_board - total_participated  # board members who haven't voted yet

    if quorum_met:
        if yes_count >= threshold:
            action.status = "approved"
            action.resolved_at = datetime.now(timezone.utc)
        elif (yes_count + remaining_possible_yes) < threshold:
            # Even if all remaining voted yes, can't reach threshold
            action.status = "rejected"
            action.resolved_at = datetime.now(timezone.utc)

    # Update action tallies for display (board_votes_for = yes, board_votes_against = no+abstain)
    action.board_votes_for = yes_count
    action.board_votes_against = no_count + abstain_count
    action.board_required = threshold

    db.commit()
    return {
        "ok": True,
        "vote": payload.vote,
        "status": action.status,
        "yes_votes": yes_count,
        "no_votes": no_count,
        "abstain_votes": abstain_count,
        "total_participated": total_participated,
        "total_board": total_board,
        "threshold": threshold,
        "quorum_met": quorum_met,
        "votes_for": yes_count,
        "votes_against": no_count + abstain_count,
        "required": threshold,
    }


# ── List & Get ────────────────────────────────────────────────────────────────

@router.get("/list")
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
            "members": [{
                "handle": m.user_handle,
                "role": m.role,
                "term_ends_at": m.term_ends_at.isoformat() if m.term_ends_at else None,
                "actions_since_review": m.actions_since_review or 0,
                "review_threshold": m.review_threshold or 100,
                "last_reviewed_at": m.last_reviewed_at.isoformat() if m.last_reviewed_at else None,
            } for m in members],
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
        "members": [{
            "handle": m.user_handle,
            "role": m.role,
            "joined_at": m.joined_at.isoformat(),
            "term_ends_at": m.term_ends_at.isoformat() if m.term_ends_at else None,
            "actions_since_review": m.actions_since_review or 0,
            "review_threshold": m.review_threshold or 100,
            "last_reviewed_at": m.last_reviewed_at.isoformat() if m.last_reviewed_at else None,
        } for m in members],
        "recent_actions": [{"id": a.id, "title": a.title, "description": a.description, "type": a.action_type, "status": a.status, "proposed_by": a.proposed_by, "votes_for": a.board_votes_for, "votes_against": a.board_votes_against, "required": a.board_required, "created_at": a.created_at.isoformat()} for a in actions],
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
        board_required=round(len(board) * 0.7, 4),
    )
    db.add(action)
    db.flush()

    # Increment action count for proposing member
    member.actions_since_review = (member.actions_since_review or 0) + 1
    db.commit()
    db.refresh(action)
    return {"ok": True, "action_id": action.id, "board_required": action.board_required, "board": board}


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

    member = CommitteeMember(
        committee_id=c.id, user_handle=payload.handle,
        role=payload.role, approved_by=current_user.handle,
    )
    db.add(member)
    db.commit()
    return {"ok": True, "member": payload.handle, "role": payload.role}


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


# ── Activity-based review ─────────────────────────────────────────────────────

class MemberReview(BaseModel):
    handle: str
    approved: bool = True           # True = renew, False = remove
    performance_notes: Optional[str] = None
    new_threshold: Optional[int] = None  # optionally change review threshold


@router.post("/{slug}/review")
def review_member(slug: str, payload: MemberReview, db: Session = Depends(get_db),
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

    if not payload.approved:
        member.is_active = False
        if payload.performance_notes:
            member.performance_notes = payload.performance_notes
        db.commit()
        return {"ok": True, "result": "removed", "handle": payload.handle}

    # Renew — reset counter
    member.actions_since_review = 0
    member.last_reviewed_at = datetime.now(timezone.utc)
    if payload.performance_notes:
        member.performance_notes = payload.performance_notes
    if payload.new_threshold:
        member.review_threshold = payload.new_threshold
    db.commit()
    return {
        "ok": True, "result": "renewed", "handle": payload.handle,
        "next_review_at": f"after {member.review_threshold} more actions"
    }
