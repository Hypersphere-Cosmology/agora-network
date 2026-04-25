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
    memo: Optional[str] = None   # e.g. "first new user to join gets this"


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

    if current_user.token_balance < payload.amount:
        raise HTTPException(status_code=402, detail="Insufficient token balance")

    fee = round(payload.amount * TRADE_FEE_RATE, 6)
    net = round(payload.amount - fee, 6)

    current_user.token_balance = round(current_user.token_balance - payload.amount, 6)
    recipient.token_balance = round(recipient.token_balance + net, 6)

    db.add(BankLedger(event_type="transfer_fee", amount=fee,
                      note=f"from={current_user.handle} to={recipient.handle}"))
    db.add(TokenEvent(event_type="transfer_out", user_id=current_user.id, amount=-payload.amount,
                      note=f"sent to {recipient.handle}" + (f" — {payload.memo}" if payload.memo else "")))
    db.add(TokenEvent(event_type="transfer_in", user_id=recipient.id, amount=net,
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
        "amount": payload.amount,
        "fee": fee,
        "net": net,
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
    """Post a public bounty — tokens held in escrow, claimable by the first eligible user."""
    if payload.amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")

    if current_user.token_balance < payload.amount:
        raise HTTPException(status_code=402, detail="Insufficient token balance")

    # Escrow: deduct immediately, store in a listing record (seller=poster, asset_id=0 sentinel)
    current_user.token_balance = round(current_user.token_balance - payload.amount, 6)

    listing = Listing(
        asset_id=None,         # no asset — this is a bounty
        seller_id=current_user.id,
        price=payload.amount,
        memo=payload.memo,
        is_active=True,
    )
    db.add(listing)
    db.add(TokenEvent(event_type="bounty_escrow", user_id=current_user.id, amount=-payload.amount,
                      note=f"bounty posted: {payload.memo or ''}"))
    db.commit()
    db.refresh(listing)

    return {
        "bounty_id": listing.id,
        "posted_by": current_user.handle,
        "amount": payload.amount,
        "memo": payload.memo,
        "status": "active — claimable via POST /marketplace/bounties/{id}/claim",
    }


@router.post("/bounties/{bounty_id}/claim", status_code=200)
@limiter.limit("10/hour")
def claim_bounty(
    request: Request,
    bounty_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Claim an active bounty. Poster decides eligibility via memo; network enforces first-claim."""
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

    fee = round(listing.price * TRADE_FEE_RATE, 6)
    net = round(listing.price - fee, 6)

    current_user.token_balance = round(current_user.token_balance + net, 6)
    listing.is_active = False

    db.add(BankLedger(event_type="bounty_fee", amount=fee,
                      note=f"bounty {bounty_id} claimed by {current_user.handle}"))
    db.add(TokenEvent(event_type="bounty_claimed", user_id=current_user.id, amount=net,
                      note=f"claimed bounty {bounty_id} from {poster.handle}"))

    db.commit()
    recalculate_all_user_scores(db)

    notify(db, poster.id, "bounty_claimed",
           f"{current_user.handle} claimed your bounty #{bounty_id} ({net:.4f} tokens transferred).")
    db.commit()

    return {
        "bounty_id": bounty_id,
        "claimed_by": current_user.handle,
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
        result.append({
            "bounty_id": b.id,
            "posted_by": poster.handle if poster else "?",
            "amount": b.price,
            "memo": b.memo,
            "claim_endpoint": f"POST /marketplace/bounties/{b.id}/claim",
        })
    return result


@router.get("/listings")
def list_listings(db: Session = Depends(get_db)):
    """Legacy endpoint — returns bounties for backward compat."""
    return list_bounties(db)
