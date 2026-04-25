"""
Agora — marketplace router
Peer token transfers. 1% fee to bank.
Assets are not for sale — tokens are.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from db import get_db, User, Listing, Trade, BankLedger, TokenEvent
from auth import get_current_user
from notifications import notify
from ratelimit import limiter
from engine.scoring import recalculate_all_user_scores

TRADE_FEE_RATE = 0.01

router = APIRouter(prefix="/marketplace", tags=["marketplace"])


class TransferCreate(BaseModel):
    to_handle: str
    amount: float
    memo: Optional[str] = None   # optional public note (e.g. "join bounty")


class TransferOut(BaseModel):
    id: int
    from_handle: str
    to_handle: str
    amount: float
    fee: float
    net: float
    memo: Optional[str]

    class Config:
        from_attributes = True


class ListingOut(BaseModel):
    id: int
    seller_handle: str
    amount: float
    memo: Optional[str]
    is_active: bool

    class Config:
        from_attributes = True


class BountyCreate(BaseModel):
    amount: float
    memo: Optional[str] = None
    requires_approval: bool = True   # poster must approve before tokens release


@router.post("/transfer", status_code=201)
@limiter.limit("30/hour")
def transfer_tokens(
    request: Request,
    payload: TransferCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send tokens directly to another user. 1% fee to bank."""
    if payload.amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")

    recipient = db.query(User).filter(User.handle == payload.to_handle).first()
    if not recipient:
        raise HTTPException(status_code=404, detail=f"User '{payload.to_handle}' not found")

    if recipient.id == current_user.id:
        raise HTTPException(status_code=403, detail="Cannot transfer to yourself")

    fee = round(payload.amount * TRADE_FEE_RATE, 6)
    total_cost = round(payload.amount + fee, 6)   # sender pays amount + fee

    if current_user.token_balance < total_cost:
        raise HTTPException(status_code=402, detail=f"Insufficient balance (need {total_cost:.4f} A: {payload.amount:.4f} + {fee:.4f} fee)")

    current_user.token_balance = round(current_user.token_balance - total_cost, 6)
    recipient.token_balance = round(recipient.token_balance + payload.amount, 6)   # recipient gets full amount

    db.add(BankLedger(event_type="transfer_fee", amount=fee,
                      note=f"from={current_user.handle} to={recipient.handle}"))
    db.add(TokenEvent(event_type="transfer_out", user_id=current_user.id, amount=-total_cost,
                      note=f"sent {payload.amount} to {recipient.handle} (+{fee} fee)" + (f" — {payload.memo}" if payload.memo else "")))
    db.add(TokenEvent(event_type="transfer_in", user_id=recipient.id, amount=payload.amount,
                      note=f"received from {current_user.handle}" + (f" — {payload.memo}" if payload.memo else "")))

    db.commit()
    recalculate_all_user_scores(db)

    memo_str = payload.memo or ""
    notify(db, recipient.id, "token_received",
           f"{current_user.handle} sent you {net:.4f} tokens"
           + (f' — "{memo_str}"' if memo_str else "") + ".")
    db.commit()

    return {
        "from_handle": current_user.handle,
        "to_handle": recipient.handle,
        "amount": payload.amount,       # recipient receives this exactly
        "fee": fee,                     # deducted from sender
        "total_deducted": total_cost,   # total out of sender's balance
        "memo": payload.memo,
    }


@router.post("/bounties", status_code=201)
@limiter.limit("10/hour")
def post_bounty(
    request: Request,
    payload: BountyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Post a bounty. Tokens go to escrow. Poster must approve claims (default) or set requires_approval=false for open first-come."""
    if payload.amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")
    if current_user.token_balance < payload.amount:
        raise HTTPException(status_code=402, detail="Insufficient token balance")

    current_user.token_balance = round(current_user.token_balance - payload.amount, 6)
    listing = Listing(
        asset_id=None,
        seller_id=current_user.id,
        price=payload.amount,
        memo=payload.memo,
        requires_approval=payload.requires_approval,
        is_active=True,
    )
    db.add(listing)
    db.add(TokenEvent(event_type="bounty_escrow", user_id=current_user.id, amount=-payload.amount,
                      note=f"bounty posted: {payload.memo or ''}"))
    db.commit()
    db.refresh(listing)

    approval_note = "poster must approve your claim" if payload.requires_approval else "first-come, no approval needed"
    return {
        "bounty_id": listing.id,
        "posted_by": current_user.handle,
        "amount": payload.amount,
        "memo": payload.memo,
        "requires_approval": payload.requires_approval,
        "status": f"active — {approval_note}",
    }


@router.post("/bounties/{bounty_id}/claim", status_code=200)
@limiter.limit("10/hour")
def claim_bounty(
    request: Request,
    bounty_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Request to claim a bounty.
    - If requires_approval=False: tokens transfer immediately (first-come).
    - If requires_approval=True: sets pending_claimant. Poster must approve via POST /bounties/{id}/approve.
    """
    listing = db.query(Listing).filter(
        Listing.id == bounty_id,
        Listing.is_active == True,
        Listing.asset_id == None,
    ).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Bounty not found or already claimed")

    poster = db.query(User).filter(User.id == listing.seller_id).first()
    if current_user.id == poster.id:
        raise HTTPException(status_code=403, detail="Cannot claim your own bounty")

    if listing.pending_claimant_id:
        pending = db.query(User).filter(User.id == listing.pending_claimant_id).first()
        raise HTTPException(status_code=409,
            detail=f"A claim from @{pending.handle if pending else '?'} is pending approval")

    if not listing.requires_approval:
        # Immediate transfer
        return _execute_bounty(listing, poster, current_user, db, bounty_id)

    # Queue for approval
    listing.pending_claimant_id = current_user.id
    db.commit()
    notify(db, poster.id, "bounty_claim_request",
           f"@{current_user.handle} is requesting to claim your bounty #{bounty_id} "
           f'("{listing.memo or ""}"). Approve: POST /marketplace/bounties/{bounty_id}/approve')
    db.commit()
    return {
        "bounty_id": bounty_id,
        "status": "pending_approval",
        "claimant": current_user.handle,
        "message": f"Claim submitted. @{poster.handle} must approve before tokens transfer.",
    }


@router.post("/bounties/{bounty_id}/approve", status_code=200)
@limiter.limit("30/hour")
def approve_bounty(
    request: Request,
    bounty_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Poster approves the pending claim — tokens transfer to claimant."""
    listing = db.query(Listing).filter(
        Listing.id == bounty_id,
        Listing.is_active == True,
        Listing.asset_id == None,
    ).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Bounty not found or already claimed")
    if listing.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the bounty poster can approve")
    if not listing.pending_claimant_id:
        raise HTTPException(status_code=409, detail="No pending claim to approve")

    claimant = db.query(User).filter(User.id == listing.pending_claimant_id).first()
    if not claimant:
        raise HTTPException(status_code=404, detail="Claimant not found")

    poster = current_user
    return _execute_bounty(listing, poster, claimant, db, bounty_id)


@router.post("/bounties/{bounty_id}/deny", status_code=200)
@limiter.limit("30/hour")
def deny_bounty(
    request: Request,
    bounty_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Poster denies the pending claim — bounty stays open for new claimants."""
    listing = db.query(Listing).filter(
        Listing.id == bounty_id,
        Listing.is_active == True,
        Listing.asset_id == None,
    ).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Bounty not found")
    if listing.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the bounty poster can deny")
    if not listing.pending_claimant_id:
        raise HTTPException(status_code=409, detail="No pending claim")

    denied = db.query(User).filter(User.id == listing.pending_claimant_id).first()
    listing.pending_claimant_id = None
    db.commit()
    if denied:
        notify(db, denied.id, "bounty_denied",
               f"Your claim on bounty #{bounty_id} was not approved. The bounty remains open.")
        db.commit()
    return {"bounty_id": bounty_id, "status": "open", "denied": denied.handle if denied else "?"}


@router.delete("/bounties/{bounty_id}", status_code=200)
def cancel_bounty(
    bounty_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cancel your bounty and refund escrowed tokens. Cannot cancel if a claim is pending approval."""
    listing = db.query(Listing).filter(
        Listing.id == bounty_id,
        Listing.is_active == True,
        Listing.asset_id == None,
    ).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Bounty not found or already closed")
    if listing.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the bounty poster can cancel it")
    if listing.pending_claimant_id:
        pending = db.query(User).filter(User.id == listing.pending_claimant_id).first()
        raise HTTPException(status_code=409,
            detail=f"Cannot cancel — a claim from @{pending.handle if pending else '?'} is pending. Deny it first, then cancel.")

    # Refund
    current_user.token_balance = round(current_user.token_balance + listing.price, 6)
    listing.is_active = False

    db.add(TokenEvent(event_type="bounty_cancelled", user_id=current_user.id, amount=listing.price,
                      note=f"bounty #{bounty_id} cancelled — refunded"))
    db.commit()

    return {
        "bounty_id": bounty_id,
        "status": "cancelled",
        "refunded": listing.price,
        "new_balance": current_user.token_balance,
    }


def _execute_bounty(listing, poster, claimant, db, bounty_id):
    """Internal: transfer escrow to claimant, close listing."""
    fee = round(listing.price * TRADE_FEE_RATE, 6)
    net = round(listing.price - fee, 6)

    claimant.token_balance = round(claimant.token_balance + net, 6)
    listing.is_active = False
    listing.approved_by = poster.id

    db.add(BankLedger(event_type="bounty_fee", amount=fee,
                      note=f"bounty {bounty_id} claimed by {claimant.handle}"))
    db.add(TokenEvent(event_type="bounty_claimed", user_id=claimant.id, amount=net,
                      note=f"claimed bounty {bounty_id} from {poster.handle}"))
    db.commit()
    recalculate_all_user_scores(db)

    notify(db, poster.id, "bounty_claimed",
           f"@{claimant.handle} received your bounty #{bounty_id} — {net:.4f} A transferred.")
    notify(db, claimant.id, "bounty_received",
           f"Bounty #{bounty_id} approved! {net:.4f} A added to your balance.")
    db.commit()

    return {
        "bounty_id": bounty_id,
        "claimed_by": claimant.handle,
        "approved_by": poster.handle,
        "amount": listing.price,
        "fee": fee,
        "net_received": net,
        "memo": listing.memo,
    }


@router.get("/bounties")
def list_bounties(db: Session = Depends(get_db)):
    """List all active bounties."""
    bounties = db.query(Listing).filter(
        Listing.is_active == True,
        Listing.asset_id == None,
    ).all()
    result = []
    for b in bounties:
        poster = db.query(User).filter(User.id == b.seller_id).first()
        pending = None
        if b.pending_claimant_id:
            p = db.query(User).filter(User.id == b.pending_claimant_id).first()
            pending = p.handle if p else "?"
        result.append({
            "bounty_id": b.id,
            "posted_by": poster.handle if poster else "?",
            "amount": b.price,
            "memo": b.memo,
            "requires_approval": bool(b.requires_approval),
            "pending_claim_from": pending,
            "claim_endpoint": f"POST /marketplace/bounties/{b.id}/claim",
        })
    return result


@router.get("/listings")
def list_listings(db: Session = Depends(get_db)):
    """Legacy endpoint — returns bounties for backward compat."""
    return list_bounties(db)
