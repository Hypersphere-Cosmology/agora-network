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
import config as _config
from auth import get_current_user
from notifications import notify
from engine.scoring import recalculate_asset_mint

MIN_SCORE_TO_VOTE = 10.0   # lowered from 20 — network too new for 20 threshold
QUORUM_OVERRIDE = 0.95    # 95% quorum required

FOUNDER_HANDLES = {"sean", "ava"}  # veto control

FOUNDER_SUNSET_THRESHOLD = 100  # founders lose special close power at 100 users


def founders_active(db: Session) -> bool:
    """Returns True if founder power is still active (< 100 users)."""
    user_count = db.query(User).count()
    return user_count < FOUNDER_SUNSET_THRESHOLD

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
    vote_count: int     # distinct voters who ranked this option
    borda_points: int   # unused legacy field
    rank_total: int     # sum of ranks (lower = more preferred)

    class Config:
        from_attributes = True


class RankedVoteSubmit(BaseModel):
    voter_handle: str
    rankings: list[int]   # list of option_ids in preference order: [top_choice_id, second_id, ...]


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


@router.get("/proposals/{proposal_id}/votes")
def get_my_votes(proposal_id: int, voter: str, db: Session = Depends(get_db)):
    """Get a voter's full ranked ballot for a proposal."""
    user = db.query(User).filter(User.handle == voter).first()
    if not user:
        return {"rankings": []}
    votes = db.query(Vote).filter(
        Vote.user_id == user.id,
        Vote.proposal_id == proposal_id
    ).order_by(Vote.rank.asc()).all()
    return {"rankings": [{"option_id": v.option_id, "rank": v.rank} for v in votes]}


@router.get("/proposals/{proposal_id}/options", response_model=list[OptionOut])
def get_options(proposal_id: int, db: Session = Depends(get_db)):
    proposal = db.query(Proposal).filter(Proposal.id == proposal_id).first()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return proposal.options


@router.post("/proposals/{proposal_id}/vote", response_model=dict)
def cast_ranked_vote(proposal_id: int, payload: RankedVoteSubmit, db: Session = Depends(get_db)):
    """
    Submit a full ranked ballot (Borda count).
    rankings = [top_choice_option_id, second_choice_id, ...]
    Must rank ALL options. Points assigned: N-1 for 1st, N-2 for 2nd, ... 0 for last.
    Can re-vote — old ballot is replaced.
    """
    proposal = db.query(Proposal).filter(Proposal.id == proposal_id).first()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal.is_closed:
        raise HTTPException(status_code=409, detail="Proposal is closed")

    voter = db.query(User).filter(User.handle == payload.voter_handle).first()
    if not voter:
        raise HTTPException(status_code=404, detail="Voter not found")
    if voter.total_score < MIN_SCORE_TO_VOTE:
        raise HTTPException(status_code=403, detail=f"Score ≥ {MIN_SCORE_TO_VOTE} required to vote")

    options = db.query(ProposalOption).filter(ProposalOption.proposal_id == proposal_id).all()
    option_ids = {o.id for o in options}
    n = len(options)

    if len(payload.rankings) != n:
        raise HTTPException(status_code=422,
            detail=f"Must rank all {n} options. Got {len(payload.rankings)}.")
    if set(payload.rankings) != option_ids:
        raise HTTPException(status_code=422,
            detail="rankings must contain each option ID exactly once.")

    # Remove old ballot for this voter on this proposal
    old_votes = db.query(Vote).filter(Vote.user_id == voter.id, Vote.proposal_id == proposal_id).all()
    if old_votes:
        # Subtract old rank contributions
        for v in old_votes:
            opt = db.query(ProposalOption).filter(ProposalOption.id == v.option_id).first()
            if opt:
                opt.rank_total = max(0, opt.rank_total - v.rank)
                opt.vote_count = max(0, opt.vote_count - 1)
        db.query(Vote).filter(Vote.user_id == voter.id, Vote.proposal_id == proposal_id).delete()
        db.flush()

    # Add new ballot — accumulate rank numbers (lower rank total = more preferred)
    for rank_pos, option_id in enumerate(payload.rankings, start=1):
        vote = Vote(
            user_id=voter.id,
            proposal_id=proposal_id,
            option_id=option_id,
            rank=rank_pos,
        )
        db.add(vote)
        opt = db.query(ProposalOption).filter(ProposalOption.id == option_id).first()
        if opt:
            opt.rank_total += rank_pos
            opt.vote_count += 1

    db.commit()

    # Count distinct voters (not rows)
    from sqlalchemy import func, distinct
    distinct_voters = db.query(func.count(distinct(Vote.user_id))).filter(
        Vote.proposal_id == proposal_id).scalar()
    eligible_voters = db.query(User).filter(User.total_score >= MIN_SCORE_TO_VOTE).count()
    quorum_met = eligible_voters > 0 and (distinct_voters / eligible_voters) >= proposal.quorum

    return {
        "ok": True,
        "voters": distinct_voters,
        "eligible_voters": eligible_voters,
        "quorum_met": quorum_met,
        "quorum_required": proposal.quorum,
        "rank_totals": {o.label: o.rank_total for o in
                        db.query(ProposalOption).filter(ProposalOption.proposal_id == proposal_id).all()},
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

    # Check quorum (count distinct voters)
    from sqlalchemy import func, distinct
    eligible_voters = db.query(User).filter(User.total_score >= MIN_SCORE_TO_VOTE).count()
    distinct_voters = db.query(func.count(distinct(Vote.user_id))).filter(
        Vote.proposal_id == proposal_id).scalar()

    is_founder = closer.handle in FOUNDER_HANDLES
    quorum_fraction = distinct_voters / eligible_voters if eligible_voters > 0 else 0.0
    quorum_ok = quorum_fraction >= proposal.quorum

    if not quorum_ok:
        # Founders can bypass quorum only while founders_active
        if is_founder and founders_active(db):
            pass  # founder override while < 100 users
        else:
            raise HTTPException(
                status_code=409,
                detail=f"Quorum not met ({distinct_voters}/{eligible_voters} voters, need {proposal.quorum*100:.0f}%)"
            )

    # Ranked choice: lowest rank_total wins
    # Only consider options that received at least one vote
    voted_options = [o for o in proposal.options if o.vote_count > 0]
    if not voted_options:
        voted_options = proposal.options
    min_total = min(o.rank_total for o in voted_options)
    leaders = [o for o in voted_options if o.rank_total == min_total]

    if len(leaders) == 1:
        winning = leaders[0]
    else:
        # Tiebreaker 1: most first-place votes
        def first_place_votes(opt):
            return db.query(Vote).filter(
                Vote.proposal_id == proposal_id,
                Vote.option_id == opt.id,
                Vote.rank == 1
            ).count()

        fp_counts = {o.id: first_place_votes(o) for o in leaders}
        max_fp = max(fp_counts.values())
        fp_leaders = [o for o in leaders if fp_counts[o.id] == max_fp]

        if len(fp_leaders) == 1:
            winning = fp_leaders[0]
        else:
            # Tiebreaker 2: status quo (no change) — mark as tie, don't auto-execute
            proposal.winning_option = "TIE — status quo prevails (no change enacted)"
            proposal.is_closed = True
            proposal.closed_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(proposal)
            return proposal

        winning = fp_leaders[0]
    proposal.winning_option = winning.label
    proposal.is_closed = True
    proposal.closed_at = datetime.now(timezone.utc)
    db.commit()

    # Auto-execute known proposal types based on title keywords
    _auto_execute(proposal, winning.label)

    db.refresh(proposal)
    return proposal


def _auto_execute(proposal: "Proposal", winning_label: str):
    """Auto-apply the result of known governance proposals."""
    title_lower = proposal.title.lower()

    # Fee rate proposals: title contains "fee" and winning label is a percentage
    if "fee" in title_lower:
        import re
        match = re.search(r'(\d+(?:\.\d+)?)\s*%', winning_label)
        if match:
            new_rate = float(match.group(1)) / 100.0
            _config.set_fee_rate(new_rate)
            print(f"[governance] Fee rate updated to {new_rate*100:.2f}% by proposal #{proposal.id}")


@router.get("/status")
def governance_status(db: Session = Depends(get_db)):
    """Current governance parameters and founder sunset status."""
    user_count = db.query(User).count()
    active = founders_active(db)
    return {
        "quorum_required": QUORUM_OVERRIDE,
        "min_score_to_vote": MIN_SCORE_TO_VOTE,
        "founder_sunset_threshold": FOUNDER_SUNSET_THRESHOLD,
        "founders_active": active,
        "user_count": user_count,
        "users_until_sunset": max(0, FOUNDER_SUNSET_THRESHOLD - user_count),
    }


@router.get("/proposals/{proposal_id}/result")
def get_proposal_result(proposal_id: int, db: Session = Depends(get_db)):
    """
    Deterministic result computation — any node can call this.
    Returns the winner based on current votes without closing.
    """
    proposal = db.query(Proposal).filter(Proposal.id == proposal_id).first()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    from sqlalchemy import func, distinct
    eligible_voters = db.query(User).filter(User.total_score >= MIN_SCORE_TO_VOTE).count()
    distinct_voters = db.query(func.count(distinct(Vote.user_id))).filter(
        Vote.proposal_id == proposal_id).scalar()

    quorum_met = eligible_voters > 0 and (distinct_voters / eligible_voters) >= proposal.quorum

    voted_options = [o for o in proposal.options if o.vote_count > 0]
    if not voted_options:
        voted_options = proposal.options

    winner = None
    if voted_options:
        min_total = min(o.rank_total for o in voted_options)
        leaders = [o for o in voted_options if o.rank_total == min_total]
        if len(leaders) == 1:
            winner = leaders[0].label
        else:
            winner = "TIE"

    return {
        "proposal_id": proposal_id,
        "title": proposal.title,
        "is_closed": proposal.is_closed,
        "winning_option": proposal.winning_option if proposal.is_closed else winner,
        "is_deterministic": True,
        "eligible_voters": eligible_voters,
        "distinct_voters": distinct_voters,
        "quorum_required": proposal.quorum,
        "quorum_met": quorum_met,
        "options": [
            {"id": o.id, "label": o.label, "vote_count": o.vote_count, "rank_total": o.rank_total}
            for o in proposal.options
        ],
    }
