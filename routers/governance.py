"""
Agora — governance router
Proposals as assets. Plurality voting. 50% quorum of eligible voters (total_score >= 20).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from db import get_db, User, Proposal, ProposalOption, Vote, Asset, StorageConfig
import config as _config
from auth import get_current_user
from notifications import notify
from engine.scoring import recalculate_asset_mint

MIN_SCORE_TO_VOTE = 10.0   # lowered from 20 — network too new for 20 threshold
QUORUM_OVERRIDE = 0.95    # 95% quorum required

FOUNDER_HANDLES = {"viralsatan", "ava"}  # veto control

FOUNDER_SUNSET_THRESHOLD = 100  # founders lose special close power at 100 users


def founders_active(db: Session) -> bool:
    """Returns True if founder power is still active.
    Threshold is ELIGIBLE voters (score >= MIN_SCORE_TO_VOTE) not total users.
    Zero-score accounts cannot trigger founder sunset — only earned participants count."""
    eligible_count = db.query(User).filter(User.total_score >= MIN_SCORE_TO_VOTE).count()
    return eligible_count < FOUNDER_SUNSET_THRESHOLD

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

    MAX_OPTIONS = 5  # governance-adjustable via StorageConfig
    max_opts_row = db.query(StorageConfig).filter(StorageConfig.key == "max_proposal_options").first()
    max_opts = int(max_opts_row.value_text) if max_opts_row else MAX_OPTIONS
    if len(payload.options) > max_opts:
        raise HTTPException(status_code=422, detail=f"Maximum {max_opts} options per proposal. Current limit is governance-adjustable.")

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
    import re
    from db import SessionLocal, StorageConfig as StorageConfigModel
    title_lower = proposal.title.lower()

    # Fee rate proposals: title contains "fee" and winning label is a percentage
    if "fee" in title_lower:
        match = re.search(r'(\d+(?:\.\d+)?)\s*%', winning_label)
        if match:
            new_rate = float(match.group(1)) / 100.0
            _config.set_fee_rate(new_rate)
            print(f"[governance] Fee rate updated to {new_rate*100:.2f}% by proposal #{proposal.id}")

    # Referral rate L1 proposals
    if "referral" in title_lower and "l1" in title_lower:
        match = re.search(r'(\d+(?:\.\d+)?)\s*%', winning_label)
        if match:
            new_rate = float(match.group(1)) / 100.0
            db = SessionLocal()
            try:
                rate_row = db.query(StorageConfigModel).filter(StorageConfigModel.key == "referral_rate_l1").first()
                if rate_row:
                    rate_row.value_text = str(new_rate)
                    db.commit()
                    print(f"[governance] Referral L1 rate updated to {new_rate*100:.2f}% by proposal #{proposal.id}")
            finally:
                db.close()

    # Referral rate L2 proposals
    if "referral" in title_lower and "l2" in title_lower:
        match = re.search(r'(\d+(?:\.\d+)?)\s*%', winning_label)
        if match:
            new_rate = float(match.group(1)) / 100.0
            db = SessionLocal()
            try:
                rate_row = db.query(StorageConfigModel).filter(StorageConfigModel.key == "referral_rate_l2").first()
                if rate_row:
                    rate_row.value_text = str(new_rate)
                    db.commit()
                    print(f"[governance] Referral L2 rate updated to {new_rate*100:.2f}% by proposal #{proposal.id}")
            finally:
                db.close()

    # Plagiarism block threshold proposals
    if "plagiarism block" in title_lower:
        match = re.search(r'(\d+(?:\.\d+)?)\s*%', winning_label)
        if match:
            new_val = str(float(match.group(1)) / 100.0)
            db = SessionLocal()
            try:
                row = db.query(StorageConfigModel).filter(StorageConfigModel.key == "plagiarism_block_threshold").first()
                if row:
                    row.value_text = new_val
                    db.commit()
                    print(f"[governance] Plagiarism block threshold updated to {new_val} by proposal #{proposal.id}")
            finally:
                db.close()

    # Plagiarism warn threshold proposals
    if "plagiarism warn" in title_lower:
        match = re.search(r'(\d+(?:\.\d+)?)\s*%', winning_label)
        if match:
            new_val = str(float(match.group(1)) / 100.0)
            db = SessionLocal()
            try:
                row = db.query(StorageConfigModel).filter(StorageConfigModel.key == "plagiarism_warn_threshold").first()
                if row:
                    row.value_text = new_val
                    db.commit()
                    print(f"[governance] Plagiarism warn threshold updated to {new_val} by proposal #{proposal.id}")
            finally:
                db.close()

    # Device fingerprint requirement
    if "fingerprint" in title_lower or "device" in title_lower:
        db = SessionLocal()
        try:
            if "require" in winning_label.lower() or "enable" in winning_label.lower():
                row = db.query(StorageConfigModel).filter(StorageConfigModel.key == "require_device_fingerprint").first()
                if row:
                    row.value_text = "1"
                    db.commit()
                    print(f"[governance] Device fingerprint requirement ENABLED by proposal #{proposal.id}")
            elif "open" in winning_label.lower() or "disable" in winning_label.lower() or "remove" in winning_label.lower():
                row = db.query(StorageConfigModel).filter(StorageConfigModel.key == "require_device_fingerprint").first()
                if row:
                    row.value_text = "0"
                    db.commit()
                    print(f"[governance] Device fingerprint requirement DISABLED by proposal #{proposal.id}")
        finally:
            db.close()


    # Max proposal options
    if "max proposal options" in title_lower:
        match = re.search(r'(\d+)', winning_label)
        if match:
            new_val = str(int(match.group(1)))
            db2 = SessionLocal()
            try:
                row = db2.query(StorageConfigModel).filter(StorageConfigModel.key == "max_proposal_options").first()
                if row:
                    row.value_text = new_val
                    db2.commit()
                    print(f"[governance] Max proposal options updated to {new_val} by proposal #{proposal.id}")
            finally:
                db2.close()

    # User removal by vote: title contains "remove user" or "ban user", winning label is the handle
    if ("remove user" in title_lower or "ban user" in title_lower or "remove account" in title_lower):
        # winning_label should be the handle to remove
        handle_to_remove = winning_label.strip().lstrip('@').lower()
        db = SessionLocal()
        try:
            from db import User as UserModel, ApiKey
            target = db.query(UserModel).filter(UserModel.handle == handle_to_remove).first()
            if target and handle_to_remove not in {"viralsatan", "ava"}:  # founders protected
                # Revoke API keys
                db.query(ApiKey).filter(ApiKey.user_id == target.id).delete()
                # Zero out scores and balance
                target.total_score = 0.0
                target.submission_score = 0.0
                target.rater_score = 0.0
                target.trade_score = 0.0
                target.token_balance = 0.0
                target.handle = f"[removed_{target.id}]"
                db.commit()
                print(f"[governance] User @{handle_to_remove} removed by proposal #{proposal.id}")
        finally:
            db.close()


@router.get("/parameters")
def get_parameters(db: Session = Depends(get_db)):
    """All governance-adjustable parameters with current values and proposal format."""
    from db import StorageConfig

    def cfg(key, default):
        row = db.query(StorageConfig).filter(StorageConfig.key == key).first()
        return float(row.value_text) if row and row.value_text else default

    user_count = db.query(User).count()
    eligible_count = db.query(User).filter(User.total_score >= MIN_SCORE_TO_VOTE).count()

    # Trade fee rate lives in config.py (in-memory), not StorageConfig
    trade_fee = _config.get_fee_rate()

    return {
        "parameters": [
            {
                "name": "Trade Fee",
                "key": "trade_fee_rate",
                "current_value": trade_fee,
                "display": f"{trade_fee*100:.1f}%",
                "description": "Fee on all token transfers and bounty claims. Goes to the bank.",
                "proposal_format": "Include 'fee' in title, winning option as '2%' or '0.5%'",
                "category": "economy"
            },
            {
                "name": "Referral Rate L1",
                "key": "referral_rate_l1",
                "current_value": cfg("referral_rate_l1", 0.05),
                "display": f"{cfg('referral_rate_l1', 0.05)*100:.1f}%",
                "description": "Direct referrer earns this % of tokens minted by users they invited.",
                "proposal_format": "Include 'referral' and 'l1' in title, winning option as '5%'",
                "category": "economy"
            },
            {
                "name": "Referral Rate L2",
                "key": "referral_rate_l2",
                "current_value": cfg("referral_rate_l2", 0.01),
                "display": f"{cfg('referral_rate_l2', 0.01)*100:.1f}%",
                "description": "Second-tier referrer earns this % of tokens minted by their referrer's invites.",
                "proposal_format": "Include 'referral' and 'l2' in title, winning option as '1%'",
                "category": "economy"
            },
            {
                "name": "Token Reference Rate",
                "key": "usd_per_token",
                "current_value": cfg("usd_per_token", 1.0),
                "display": f"${cfg('usd_per_token', 1.0):.2f}/A",
                "description": "Reference exchange rate for tokens. Affects buy/sell pricing.",
                "proposal_format": "Include 'token rate' in title, winning option as '$2.00' or '2.00'",
                "category": "economy"
            },
            {
                "name": "Governance Quorum",
                "key": "quorum",
                "current_value": QUORUM_OVERRIDE,
                "display": f"{QUORUM_OVERRIDE*100:.0f}%",
                "description": "Percentage of eligible voters required for a proposal to pass.",
                "proposal_format": "Include 'quorum' in title, winning option as '80%'",
                "category": "governance"
            },
            {
                "name": "Minimum Score to Vote",
                "key": "min_score",
                "current_value": MIN_SCORE_TO_VOTE,
                "display": str(MIN_SCORE_TO_VOTE),
                "description": "Total score threshold required to submit or vote on proposals.",
                "proposal_format": "Include 'min score' in title, winning option as '15'",
                "category": "governance"
            },
            {
                "name": "Founder Sunset Threshold",
                "key": "founder_sunset",
                "current_value": FOUNDER_SUNSET_THRESHOLD,
                "display": f"{FOUNDER_SUNSET_THRESHOLD} users",
                "description": "User count at which founders lose ability to bypass quorum. Currently immutable.",
                "proposal_format": "N/A — immutable by design",
                "category": "governance"
            },
            {
                "name": "Auto-Prune Threshold",
                "key": "prune_threshold",
                "current_value": 1.0,
                "display": "avg ≤ 1.0 AND ≥20% raters",
                "description": "Assets rated at or below this average by enough raters are auto-deleted.",
                "proposal_format": "Include 'prune' in title",
                "category": "content"
            },
            {
                "name": "Device Fingerprint Requirement",
                "key": "require_device_fingerprint",
                "current_value": 1 if cfg("require_device_fingerprint", 1.0) else 0,
                "display": "Required" if cfg("require_device_fingerprint", 1.0) else "Open",
                "description": "One account per physical device. Prevents cheap sybil attacks. Disable during growth campaigns.",
                "proposal_format": "Include 'device' or 'fingerprint' in title, winning option 'require' or 'open registration'",
                "category": "governance"
            },
            {
                "name": "Plagiarism Block Threshold",
                "key": "plagiarism_block_threshold",
                "current_value": cfg("plagiarism_block_threshold", 0.92),
                "display": f"{cfg('plagiarism_block_threshold', 0.92)*100:.0f}% similarity",
                "description": "Semantic similarity above this threshold blocks submission. 0.92 = 92% similar content rejected.",
                "proposal_format": "Include 'plagiarism block' in title, winning option as '90%'",
                "category": "content"
            },
            {
                "name": "Plagiarism Warn Threshold",
                "key": "plagiarism_warn_threshold",
                "current_value": cfg("plagiarism_warn_threshold", 0.75),
                "display": f"{cfg('plagiarism_warn_threshold', 0.75)*100:.0f}% similarity",
                "description": "Semantic similarity above this threshold warns submitter but allows submission.",
                "proposal_format": "Include 'plagiarism warn' in title, winning option as '70%'",
                "category": "content"
            },
            {
                "name": "Score Dimensions",
                "key": "score_dimensions",
                "current_value": 4,
                "display": "Submission (0-10) + Rater (0-10) + Trade (0-10) + Referral (0-10) = 40 max",
                "description": "Four equally-weighted percentile-normalized dimensions. Max total score: 40. Dimensions and weights adjustable by vote.",
                "proposal_format": "Propose 'add score dimension: <name>' or 'reweight score dimensions'",
                "category": "governance"
            },
            {
                "name": "Max Proposal Options",
                "key": "max_proposal_options",
                "current_value": int(cfg("max_proposal_options", 5.0) if db.query(StorageConfig).filter(StorageConfig.key == "max_proposal_options").first() else 5),
                "display": "5 options maximum",
                "description": "Maximum number of options in a ranked-choice governance proposal. 5 is the recommended cognitive limit for meaningful ranking.",
                "proposal_format": "Include 'max proposal options' in title, winning option as '7'",
                "category": "governance"
            },
        ],
        "founders_active": founders_active(db),
        "user_count": user_count,
        "users_until_sunset": max(0, FOUNDER_SUNSET_THRESHOLD - eligible_count),
        "eligible_user_count": eligible_count,
        "total_user_count": user_count
    }


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
