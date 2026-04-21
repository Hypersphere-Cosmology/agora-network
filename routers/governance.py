"""
Agora — governance router
Proposals as assets. Plurality voting. 50% quorum of eligible voters (total_score >= 20).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from db import get_db, User, Proposal, ProposalOption, Vote, Asset
from auth import get_current_user
from notifications import notify
from engine.scoring import recalculate_asset_mint

MIN_SCORE_TO_VOTE = 20.0
QUORUM_OVERRIDE = 1.0  # 100% until founders manually lower it via governance

FOUNDER_HANDLES = {"sean", "ava"}  # veto control

router = APIRouter(prefix="/governance", tags=["governance"])


class ProposalCreate(BaseModel):
    title: str
    description: Optional[str] = None
    proposer_handle: str
    options: list[str]   # list of option labels
    quorum: Optional[float] = 0.5


class ProposalOut(BaseModel):
    id: int
    title: str
    description: Optional[str]
    proposer_id: int
    quorum: float
    is_closed: bool
    winning_option: Optional[str]

    class Config:
        from_attributes = True


class OptionOut(BaseModel):
    id: int
    label: str
    vote_count: int

    class Config:
        from_attributes = True


class VoteSubmit(BaseModel):
    voter_handle: str
    option_id: int


@router.post("/proposals", response_model=ProposalOut, status_code=201)
def create_proposal(payload: ProposalCreate, db: Session = Depends(get_db)):
    proposer = db.query(User).filter(User.handle == payload.proposer_handle).first()
    if not proposer:
        raise HTTPException(status_code=404, detail="Proposer not found")

    if proposer.total_score < MIN_SCORE_TO_VOTE:
        raise HTTPException(
            status_code=403,
            detail=f"Total score must be >= {MIN_SCORE_TO_VOTE} to propose"
        )

    if len(payload.options) < 2:
        raise HTTPException(status_code=422, detail="At least 2 options required")

    proposal = Proposal(
        title=payload.title,
        description=payload.description,
        proposer_id=proposer.id,
        quorum=QUORUM_OVERRIDE,  # Locked at 100% until founders lower it
    )
    db.add(proposal)
    db.flush()

    for label in payload.options:
        opt = ProposalOption(proposal_id=proposal.id, label=label)
        db.add(opt)

    db.commit()
    db.refresh(proposal)
    return proposal


@router.get("/proposals", response_model=list[ProposalOut])
def list_proposals(db: Session = Depends(get_db)):
    return db.query(Proposal).all()


@router.get("/proposals/{proposal_id}/options", response_model=list[OptionOut])
def get_options(proposal_id: int, db: Session = Depends(get_db)):
    proposal = db.query(Proposal).filter(Proposal.id == proposal_id).first()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return proposal.options


@router.post("/proposals/{proposal_id}/vote", response_model=dict)
def cast_vote(proposal_id: int, payload: VoteSubmit, db: Session = Depends(get_db)):
    proposal = db.query(Proposal).filter(Proposal.id == proposal_id).first()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal.is_closed:
        raise HTTPException(status_code=409, detail="Proposal is closed")

    voter = db.query(User).filter(User.handle == payload.voter_handle).first()
    if not voter:
        raise HTTPException(status_code=404, detail="Voter not found")

    if voter.total_score < MIN_SCORE_TO_VOTE:
        raise HTTPException(
            status_code=403,
            detail=f"Total score must be >= {MIN_SCORE_TO_VOTE} to vote"
        )

    option = db.query(ProposalOption).filter(
        ProposalOption.id == payload.option_id,
        ProposalOption.proposal_id == proposal_id
    ).first()
    if not option:
        raise HTTPException(status_code=404, detail="Option not found on this proposal")

    existing = db.query(Vote).filter(
        Vote.user_id == voter.id,
        Vote.proposal_id == proposal_id
    ).first()
    if existing:
        # Change vote: decrement old option
        old_option = db.query(ProposalOption).filter(ProposalOption.id == existing.option_id).first()
        if old_option:
            old_option.vote_count = max(0, old_option.vote_count - 1)
        existing.option_id = payload.option_id
        existing.voted_at = datetime.now(timezone.utc)
    else:
        vote = Vote(user_id=voter.id, proposal_id=proposal_id, option_id=payload.option_id)
        db.add(vote)

    option.vote_count += 1
    db.commit()

    # Check quorum
    eligible_voters = db.query(User).filter(User.total_score >= MIN_SCORE_TO_VOTE).count()
    total_votes = db.query(Vote).filter(Vote.proposal_id == proposal_id).count()

    quorum_met = eligible_voters > 0 and (total_votes / eligible_voters) >= proposal.quorum

    return {
        "ok": True,
        "total_votes": total_votes,
        "eligible_voters": eligible_voters,
        "quorum_met": quorum_met,
        "quorum_required": proposal.quorum,
    }


@router.post("/proposals/{proposal_id}/close", response_model=ProposalOut)
def close_proposal(proposal_id: int, closer_handle: str, db: Session = Depends(get_db)):
    """Close a proposal and determine the winner (plurality — highest vote count)."""
    proposal = db.query(Proposal).filter(Proposal.id == proposal_id).first()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal.is_closed:
        raise HTTPException(status_code=409, detail="Already closed")

    closer = db.query(User).filter(User.handle == closer_handle).first()
    if not closer:
        raise HTTPException(status_code=404, detail="User not found")
    if closer.total_score < MIN_SCORE_TO_VOTE:
        raise HTTPException(status_code=403, detail="Insufficient score to close proposal")

    # Check quorum
    eligible_voters = db.query(User).filter(User.total_score >= MIN_SCORE_TO_VOTE).count()
    total_votes = db.query(Vote).filter(Vote.proposal_id == proposal_id).count()

    if eligible_voters > 0 and (total_votes / eligible_voters) < proposal.quorum:
        raise HTTPException(
            status_code=409,
            detail=f"Quorum not met ({total_votes}/{eligible_voters} votes, need {proposal.quorum*100:.0f}%)"
        )

    # Plurality: highest vote count wins
    winning = max(proposal.options, key=lambda o: o.vote_count)
    proposal.winning_option = winning.label
    proposal.is_closed = True
    proposal.closed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(proposal)
    return proposal


@router.post("/quorum")
def set_quorum(new_quorum: float, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    """Founders only — manually adjust the global quorum override."""
    global QUORUM_OVERRIDE
    if current_user.handle not in FOUNDER_HANDLES:
        raise HTTPException(status_code=403, detail="Founders only")
    if not (0.0 < new_quorum <= 1.0):
        raise HTTPException(status_code=422, detail="Quorum must be between 0.01 and 1.0")
    QUORUM_OVERRIDE = new_quorum
    return {"ok": True, "quorum": QUORUM_OVERRIDE, "set_by": current_user.handle}
