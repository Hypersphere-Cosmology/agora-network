"""
Agora — marketplace router
List assets for sale, buy assets. 1% fee to bank.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from db import get_db, User, Asset, Listing, Trade, BankLedger, TokenEvent
from auth import get_current_user
from notifications import notify
from ratelimit import limiter
from engine.scoring import recalculate_all_user_scores

TRADE_FEE_RATE = 0.01

router = APIRouter(prefix="/marketplace", tags=["marketplace"])


class ListingCreate(BaseModel):
    asset_id: int
    price: float


class ListingOut(BaseModel):
    id: int
    asset_id: int
    seller_id: int
    price: float
    is_active: bool

    class Config:
        from_attributes = True


class TradeOut(BaseModel):
    id: int
    listing_id: int
    buyer_id: int
    seller_id: int
    asset_id: int
    price: float
    fee: float

    class Config:
        from_attributes = True


@router.post("/listings", response_model=ListingOut, status_code=201)
@limiter.limit("30/hour")
def create_listing(
    request: Request,
    payload: ListingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    seller = current_user

    asset = db.query(Asset).filter(Asset.id == payload.asset_id, Asset.is_deleted == False).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    if asset.submitter_id != seller.id:
        raise HTTPException(status_code=403, detail="Only the asset submitter can list it")

    if payload.price <= 0:
        raise HTTPException(status_code=422, detail="Price must be positive")

    listing = Listing(asset_id=payload.asset_id, seller_id=seller.id, price=payload.price)
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return listing


@router.get("/listings", response_model=list[ListingOut])
def list_listings(db: Session = Depends(get_db)):
    return db.query(Listing).filter(Listing.is_active == True).all()


@router.post("/listings/{listing_id}/buy", response_model=TradeOut)
@limiter.limit("30/hour")
def buy_asset(
    request: Request,
    listing_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    listing = db.query(Listing).filter(Listing.id == listing_id, Listing.is_active == True).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found or inactive")

    buyer = current_user
    seller = db.query(User).filter(User.id == listing.seller_id).first()

    if buyer.id == seller.id:
        raise HTTPException(status_code=403, detail="Cannot buy your own listing")

    if buyer.token_balance < listing.price:
        raise HTTPException(status_code=402, detail="Insufficient token balance")

    fee = round(listing.price * TRADE_FEE_RATE, 6)
    seller_receives = round(listing.price - fee, 6)

    # Transfer tokens
    buyer.token_balance = round(buyer.token_balance - listing.price, 6)
    seller.token_balance = round(seller.token_balance + seller_receives, 6)

    # Bank gets fee
    bank_entry = BankLedger(event_type="trade_fee", amount=fee, note=f"listing={listing_id}")
    db.add(bank_entry)

    # Token events
    db.add(TokenEvent(event_type="trade_fee", user_id=buyer.id, amount=-listing.price,
                      note=f"bought listing {listing_id}"))
    db.add(TokenEvent(event_type="trade_fee", user_id=seller.id, amount=seller_receives,
                      note=f"sold listing {listing_id}"))

    # Close listing
    listing.is_active = False

    trade = Trade(
        listing_id=listing_id,
        buyer_id=buyer.id,
        seller_id=seller.id,
        asset_id=listing.asset_id,
        price=listing.price,
        fee=fee,
    )
    db.add(trade)
    db.commit()

    # Recalculate scores (trade counts changed)
    recalculate_all_user_scores(db)

    # Notifications
    notify(db, seller.id, "trade_completed",
           f"Your listing sold: asset #{listing.asset_id} to {buyer.handle} "
           f"for {listing.price:.4f} tokens (you received {seller_receives:.4f} after 1% fee).")
    notify(db, buyer.id, "trade_completed",
           f"Purchase complete: asset #{listing.asset_id} from {seller.handle} "
           f"for {listing.price:.4f} tokens.")
    db.commit()

    db.refresh(trade)
    return trade
