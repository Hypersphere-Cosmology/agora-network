"""
Agora — token purchase / fiat on-ramp
Users request token purchases, submit payment txid, founders confirm → tokens minted.
Supports SOL, BTC, ETH, and manual (Venmo/CashApp).
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from db import get_db, User, TokenPurchase, PaymentAddress, TokenEvent, StorageConfig
from auth import get_current_user
from notifications import notify
from ratelimit import limiter

router = APIRouter(prefix="/fiat", tags=["fiat"])

FOUNDER_HANDLES = {"sean", "ava"}


def get_usd_rate(db: Session) -> float:
    row = db.query(StorageConfig).filter(StorageConfig.key == "usd_per_token").first()
    return float(row.value_text) if row and row.value_text else 1.00


class PurchaseRequest(BaseModel):
    amount_tokens: float          # how many tokens they want
    payment_method: str           # 'sol', 'btc', 'eth', 'manual'


class SubmitTxid(BaseModel):
    txid: str
    notes: Optional[str] = None


class AddAddress(BaseModel):
    currency: str
    address: str
    label: Optional[str] = None


class ConfirmPurchase(BaseModel):
    notes: Optional[str] = None


@router.get("/rate")
def get_rate(db: Session = Depends(get_db)):
    """Current token price in USD."""
    rate = get_usd_rate(db)
    return {
        "usd_per_token": rate,
        "token_per_usd": round(1 / rate, 4),
        "note": "Reference rate. Governance-adjustable by vote.",
        "payment_methods": ["sol", "btc", "eth", "manual"],
    }


@router.get("/addresses")
def list_addresses(db: Session = Depends(get_db)):
    """List active payment addresses."""
    addrs = db.query(PaymentAddress).filter(PaymentAddress.is_active == True).all()
    return [
        {"currency": a.currency, "address": a.address, "label": a.label}
        for a in addrs
    ]


@router.post("/addresses")
def add_address(
    payload: AddAddress,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Founders only — add a payment address."""
    if current_user.handle not in FOUNDER_HANDLES:
        raise HTTPException(status_code=403, detail="Founders only")
    addr = PaymentAddress(
        currency=payload.currency,
        address=payload.address,
        label=payload.label,
    )
    db.add(addr)
    db.commit()
    db.refresh(addr)
    return {"id": addr.id, "currency": addr.currency, "address": addr.address}


@router.post("/buy", status_code=201)
@limiter.limit("10/hour")
def request_purchase(
    request: Request,
    payload: PurchaseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Request a token purchase. Returns payment address and amount due.
    After sending payment, submit the txid via POST /fiat/buy/{id}/txid.
    Founders confirm → tokens minted to your balance.
    """
    if payload.amount_tokens <= 0:
        raise HTTPException(status_code=422, detail="Must request at least 0.01 tokens")
    if payload.amount_tokens > 10000:
        raise HTTPException(status_code=422, detail="Max single purchase: 10,000 tokens")

    valid_methods = {"sol", "btc", "eth", "manual"}
    if payload.payment_method not in valid_methods:
        raise HTTPException(status_code=422, detail=f"Payment method must be one of: {', '.join(valid_methods)}")

    rate = get_usd_rate(db)
    usd_due = round(payload.amount_tokens * rate, 2)

    # Get payment address for this method
    addr_row = db.query(PaymentAddress).filter(
        PaymentAddress.currency == payload.payment_method,
        PaymentAddress.is_active == True
    ).first()

    purchase = TokenPurchase(
        buyer_id=current_user.id,
        amount_tokens=payload.amount_tokens,
        amount_usd=usd_due,
        payment_method=payload.payment_method,
        payment_address=addr_row.address if addr_row else None,
        status="pending",
    )
    db.add(purchase)
    db.commit()
    db.refresh(purchase)

    # Notify founders
    for handle in FOUNDER_HANDLES:
        founder = db.query(User).filter(User.handle == handle).first()
        if founder:
            notify(db, founder.id, "purchase_request",
                   f"@{current_user.handle} wants to buy {payload.amount_tokens} A "
                   f"(${usd_due:.2f} via {payload.payment_method}). Purchase #{purchase.id}.")
    db.commit()

    return {
        "purchase_id": purchase.id,
        "tokens_requested": payload.amount_tokens,
        "usd_due": usd_due,
        "rate": f"1 A = ${rate:.2f}",
        "payment_method": payload.payment_method,
        "send_to": addr_row.address if addr_row else "No address configured — contact @sean or @ava",
        "next_step": f"Send ${usd_due:.2f} worth of {payload.payment_method.upper()} to the address above, then POST /fiat/buy/{purchase.id}/txid with your transaction ID.",
        "status": "pending",
    }


@router.post("/buy/{purchase_id}/txid")
def submit_txid(
    purchase_id: int,
    payload: SubmitTxid,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Buyer submits their transaction ID after sending payment."""
    purchase = db.query(TokenPurchase).filter(
        TokenPurchase.id == purchase_id,
        TokenPurchase.buyer_id == current_user.id,
    ).first()
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")
    if purchase.status not in ("pending",):
        raise HTTPException(status_code=409, detail=f"Purchase is already {purchase.status}")

    purchase.txid = payload.txid
    purchase.notes = payload.notes
    purchase.status = "confirming"
    purchase.updated_at = datetime.now(timezone.utc)
    db.commit()

    # Notify founders to confirm
    for handle in FOUNDER_HANDLES:
        founder = db.query(User).filter(User.handle == handle).first()
        if founder:
            notify(db, founder.id, "purchase_txid",
                   f"@{current_user.handle} submitted txid for purchase #{purchase_id} "
                   f"({purchase.amount_tokens} A / ${purchase.amount_usd:.2f}): {payload.txid}. "
                   f"Confirm: POST /fiat/buy/{purchase_id}/confirm")
    db.commit()

    return {
        "purchase_id": purchase_id,
        "status": "confirming",
        "txid": payload.txid,
        "message": "Transaction submitted. A founder will verify and mint your tokens shortly.",
    }


@router.post("/buy/{purchase_id}/confirm")
def confirm_purchase(
    purchase_id: int,
    payload: ConfirmPurchase,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Founders only — verify payment and mint tokens to buyer."""
    if current_user.handle not in FOUNDER_HANDLES:
        raise HTTPException(status_code=403, detail="Founders only")

    purchase = db.query(TokenPurchase).filter(TokenPurchase.id == purchase_id).first()
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")
    if purchase.status == "complete":
        raise HTTPException(status_code=409, detail="Already confirmed")
    if purchase.status == "rejected":
        raise HTTPException(status_code=409, detail="Purchase was rejected")

    buyer = db.query(User).filter(User.id == purchase.buyer_id).first()
    if not buyer:
        raise HTTPException(status_code=404, detail="Buyer not found")

    # Mint tokens
    buyer.token_balance = round(buyer.token_balance + purchase.amount_tokens, 6)
    purchase.status = "complete"
    purchase.confirmed_by = current_user.id
    purchase.notes = (purchase.notes or "") + (f"\nConfirmed by @{current_user.handle}" + (f": {payload.notes}" if payload.notes else ""))
    purchase.updated_at = datetime.now(timezone.utc)

    db.add(TokenEvent(
        event_type="purchase_mint",
        user_id=buyer.id,
        amount=purchase.amount_tokens,
        note=f"purchased {purchase.amount_tokens} A for ${purchase.amount_usd:.2f} via {purchase.payment_method}"
    ))
    db.commit()

    notify(db, buyer.id, "tokens_minted",
           f"{purchase.amount_tokens} A minted to your account! "
           f"Payment of ${purchase.amount_usd:.2f} confirmed by @{current_user.handle}. "
           f"New balance: {buyer.token_balance:.4f} A.")
    db.commit()

    return {
        "purchase_id": purchase_id,
        "status": "complete",
        "tokens_minted": purchase.amount_tokens,
        "buyer": buyer.handle,
        "new_balance": buyer.token_balance,
        "confirmed_by": current_user.handle,
    }


@router.post("/buy/{purchase_id}/reject")
def reject_purchase(
    purchase_id: int,
    reason: str = "Payment not verified",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Founders only — reject a purchase (payment not received / invalid txid)."""
    if current_user.handle not in FOUNDER_HANDLES:
        raise HTTPException(status_code=403, detail="Founders only")

    purchase = db.query(TokenPurchase).filter(TokenPurchase.id == purchase_id).first()
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")
    if purchase.status in ("complete", "rejected"):
        raise HTTPException(status_code=409, detail=f"Purchase is already {purchase.status}")

    purchase.status = "rejected"
    purchase.notes = (purchase.notes or "") + f"\nRejected by @{current_user.handle}: {reason}"
    purchase.updated_at = datetime.now(timezone.utc)
    db.commit()

    buyer = db.query(User).filter(User.id == purchase.buyer_id).first()
    if buyer:
        notify(db, buyer.id, "purchase_rejected",
               f"Purchase #{purchase_id} rejected: {reason}. Contact @sean or @ava if you believe this is an error.")
        db.commit()

    return {"purchase_id": purchase_id, "status": "rejected", "reason": reason}


@router.get("/buy/mine")
def my_purchases(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Your purchase history."""
    purchases = db.query(TokenPurchase).filter(
        TokenPurchase.buyer_id == current_user.id
    ).order_by(TokenPurchase.created_at.desc()).all()

    return [
        {
            "purchase_id": p.id,
            "tokens": p.amount_tokens,
            "usd": p.amount_usd,
            "method": p.payment_method,
            "txid": p.txid,
            "status": p.status,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in purchases
    ]


@router.get("/buy/pending")
def pending_purchases(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Founders only — list all pending/confirming purchases."""
    if current_user.handle not in FOUNDER_HANDLES:
        raise HTTPException(status_code=403, detail="Founders only")

    purchases = db.query(TokenPurchase).filter(
        TokenPurchase.status.in_(["pending", "confirming"])
    ).order_by(TokenPurchase.created_at.asc()).all()

    result = []
    for p in purchases:
        buyer = db.query(User).filter(User.id == p.buyer_id).first()
        result.append({
            "purchase_id": p.id,
            "buyer": buyer.handle if buyer else "?",
            "tokens": p.amount_tokens,
            "usd": p.amount_usd,
            "method": p.payment_method,
            "send_to": p.payment_address,
            "txid": p.txid,
            "status": p.status,
            "notes": p.notes,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "confirm_endpoint": f"POST /fiat/buy/{p.id}/confirm",
            "reject_endpoint": f"POST /fiat/buy/{p.id}/reject?reason=...",
        })
    return result
