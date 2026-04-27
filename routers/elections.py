"""
Agora — Committee election system.
Seats are filled by: open candidacy → ranked-choice vote → board ratification.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from db import get_db, User, Committee, CommitteeElection, ElectionCandidate, CommitteeMember
from auth import get_current_user

router = APIRouter(prefix="/elections", tags=["elections"])


# ── Open an election ──────────────────────────────────────────────────────────

class ElectionOpen(BaseModel):
    committee_slug: str
    seat: str = "head"

@router.post("")
def open_election(payload: ElectionOpen, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Open a committee seat election. Board members only."""
    from routers.committees import get_board_members
    board = get_board_members()
    if current_user.handle not in board:
        raise HTTPException(status_code=403, detail="Board members only can open elections")

    committee = db.query(Committee).filter(Committee.slug == payload.committee_slug, Committee.is_active == True).first()
    if not committee:
        raise HTTPException(status_code=404, detail="Committee not found")

    # Check no open election already
    existing = db.query(CommitteeElection).filter(
        CommitteeElection.committee_id == committee.id,
        CommitteeElection.status.in_(["open", "voting", "ratification"])
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Election already open (#{existing.id})")

    election = CommitteeElection(
        committee_id=committee.id,
        committee_slug=payload.committee_slug,
        seat=payload.seat,
        announced_by=current_user.handle,
    )
    db.add(election)
    db.commit()
    db.refresh(election)
    return {
        "ok": True,
        "election_id": election.id,
        "committee": committee.name,
        "seat": payload.seat,
        "status": "open",
        "message": f"Election open for {committee.name} {payload.seat}. Candidates may now declare."
    }


# ── Declare candidacy ─────────────────────────────────────────────────────────

class CandidacyDeclare(BaseModel):
    election_id: int
    statement: Optional[str] = None

@router.post("/declare")
def declare_candidacy(payload: CandidacyDeclare, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """Declare candidacy for an open election."""
    election = db.query(CommitteeElection).filter(
        CommitteeElection.id == payload.election_id,
        CommitteeElection.status == "open"
    ).first()
    if not election:
        raise HTTPException(status_code=404, detail="No open election with this ID")

    # Judicial committee: candidates cannot be node operators
    if election.committee_slug == "judicial":
        from routers.committees import get_board_members
        if current_user.handle in get_board_members():
            raise HTTPException(status_code=403, detail="Node operators cannot serve on the Judicial Committee (conflict of interest)")

    # Check not already declared
    existing = db.query(ElectionCandidate).filter(
        ElectionCandidate.election_id == payload.election_id,
        ElectionCandidate.handle == current_user.handle,
        ElectionCandidate.is_active == True
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already declared candidacy")

    candidate = ElectionCandidate(
        election_id=payload.election_id,
        handle=current_user.handle,
        statement=payload.statement,
    )
    db.add(candidate)
    db.commit()
    db.refresh(candidate)
    return {"ok": True, "candidate": current_user.handle, "election_id": payload.election_id}


# ── Withdraw candidacy ────────────────────────────────────────────────────────

@router.delete("/declare/{election_id}")
def withdraw_candidacy(election_id: int, db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    candidate = db.query(ElectionCandidate).filter(
        ElectionCandidate.election_id == election_id,
        ElectionCandidate.handle == current_user.handle,
        ElectionCandidate.is_active == True
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="No active candidacy found")
    candidate.is_active = False
    db.commit()
    return {"ok": True, "withdrawn": current_user.handle}


# ── List elections ────────────────────────────────────────────────────────────

@router.get("")
def list_elections(db: Session = Depends(get_db)):
    elections = db.query(CommitteeElection).order_by(CommitteeElection.opened_at.desc()).limit(20).all()
    result = []
    for e in elections:
        candidates = db.query(ElectionCandidate).filter(
            ElectionCandidate.election_id == e.id,
            ElectionCandidate.is_active == True
        ).all()
        result.append({
            "id": e.id,
            "committee_slug": e.committee_slug,
            "seat": e.seat,
            "status": e.status,
            "opened_at": e.opened_at.isoformat(),
            "announced_by": e.announced_by,
            "winner": e.winner_handle,
            "candidate_count": len(candidates),
            "candidates": [{"handle": c.handle, "statement": c.statement, "declared_at": c.declared_at.isoformat()} for c in candidates],
        })
    return {"elections": result}


# ── Close candidacy → move to voting ─────────────────────────────────────────

@router.post("/{election_id}/close-candidacy")
def close_candidacy(election_id: int, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """Close candidacy period and create a governance proposal for the vote. Board only."""
    from routers.committees import get_board_members
    board = get_board_members()
    if current_user.handle not in board:
        raise HTTPException(status_code=403, detail="Board members only")

    election = db.query(CommitteeElection).filter(
        CommitteeElection.id == election_id,
        CommitteeElection.status == "open"
    ).first()
    if not election:
        raise HTTPException(status_code=404, detail="Open election not found")

    candidates = db.query(ElectionCandidate).filter(
        ElectionCandidate.election_id == election_id,
        ElectionCandidate.is_active == True
    ).all()

    if len(candidates) < 1:
        raise HTTPException(status_code=422, detail="No candidates declared — cannot close")

    # Create governance proposal for the vote
    from db import Proposal, ProposalOption
    import routers.governance as gov_router

    committee = db.query(Committee).filter(Committee.id == election.committee_id).first()
    proposal = Proposal(
        title=f"Election: {committee.name} {election.seat.title()} — Rank your choice",
        description=f"Ranked-choice election for {committee.name} {election.seat} position. Rank candidates 1 (most preferred) to {len(candidates)} (least). The candidate with the lowest total rank wins.",
        proposer_id=db.query(User).filter(User.handle == current_user.handle).first().id,
        quorum=gov_router.QUORUM_OVERRIDE,
    )
    db.add(proposal)
    db.flush()

    for c in candidates:
        opt = ProposalOption(
            proposal_id=proposal.id,
            label=f"@{c.handle}" + (f" — {c.statement[:100]}" if c.statement else "")
        )
        db.add(opt)

    election.status = "voting"
    election.voting_opens_at = datetime.now(timezone.utc)
    db.commit()

    return {
        "ok": True,
        "election_id": election_id,
        "proposal_id": proposal.id,
        "candidates": [c.handle for c in candidates],
        "message": f"Candidacy closed. Governance proposal #{proposal.id} created for the vote."
    }


# ── Record winner after governance vote closes ────────────────────────────────

@router.post("/{election_id}/ratify")
def ratify_winner(election_id: int, winner_handle: str, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """After governance vote closes, record winner and move to board ratification. Board only."""
    from routers.committees import get_board_members
    board = get_board_members()
    if current_user.handle not in board:
        raise HTTPException(status_code=403, detail="Board members only")

    election = db.query(CommitteeElection).filter(
        CommitteeElection.id == election_id,
        CommitteeElection.status == "voting"
    ).first()
    if not election:
        raise HTTPException(status_code=404, detail="Election in voting status not found")

    winner = db.query(User).filter(User.handle == winner_handle).first()
    if not winner:
        raise HTTPException(status_code=404, detail="Winner not found")

    election.winner_handle = winner_handle
    election.status = "ratification"
    db.commit()

    return {
        "ok": True,
        "winner": winner_handle,
        "status": "ratification",
        "message": f"Winner recorded. Board must now ratify @{winner_handle} to complete the election."
    }


# ── Board confirms → member added ─────────────────────────────────────────────

@router.post("/{election_id}/confirm")
def confirm_election(election_id: int, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """Board confirms the election result — installs the winner as committee head."""
    from routers.committees import get_board_members
    board = get_board_members()
    if current_user.handle not in board:
        raise HTTPException(status_code=403, detail="Board members only")

    election = db.query(CommitteeElection).filter(
        CommitteeElection.id == election_id,
        CommitteeElection.status == "ratification"
    ).first()
    if not election:
        raise HTTPException(status_code=404, detail="Election in ratification status not found")

    winner = election.winner_handle
    committee = db.query(Committee).filter(Committee.id == election.committee_id).first()

    # Deactivate any existing head
    existing_heads = db.query(CommitteeMember).filter(
        CommitteeMember.committee_id == election.committee_id,
        CommitteeMember.role == "head",
        CommitteeMember.is_active == True
    ).all()
    for h in existing_heads:
        h.is_active = False

    # Install winner
    new_member = CommitteeMember(
        committee_id=election.committee_id,
        user_handle=winner,
        role=election.seat,
        approved_by="election",
    )
    db.add(new_member)

    election.status = "closed"
    election.closed_at = datetime.now(timezone.utc)
    db.commit()

    return {
        "ok": True,
        "winner_installed": winner,
        "committee": committee.name,
        "role": election.seat,
        "message": f"@{winner} is now {election.seat} of {committee.name}."
    }
