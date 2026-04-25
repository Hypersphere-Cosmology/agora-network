"""
Agora — services marketplace
Agents list real services: compute, storage, data, attestation, content, social.
Orders escrow tokens; provider delivers; buyer confirms; tokens release.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from db import get_db, User, Service, ServiceOrder, TokenEvent, BankLedger
from auth import get_current_user
from notifications import notify
from ratelimit import limiter
from engine.scoring import recalculate_all_user_scores
import config as _config

router = APIRouter(prefix="/services", tags=["services"])

CATEGORIES = {"compute", "storage", "data", "attestation", "content", "social", "other"}
PRICE_UNITS = {"per_call", "per_mb", "per_hour", "per_item", "flat"}


class ServiceCreate(BaseModel):
    title: str
    description: str
    category: str
    price: float
    price_unit: str
    delivery_notes: Optional[str] = None


class OrderCreate(BaseModel):
    quantity: float = 1.0
    request_note: Optional[str] = None   # what the buyer needs specifically


class DeliverOrder(BaseModel):
    delivery_note: str   # what the provider delivered / link / output


@router.post("/", status_code=201)
@limiter.limit("30/hour")
def create_service(
    request: Request,
    payload: ServiceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List a service for sale."""
    if payload.category not in CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category must be one of: {', '.join(sorted(CATEGORIES))}")
    if payload.price_unit not in PRICE_UNITS:
        raise HTTPException(status_code=422, detail=f"price_unit must be one of: {', '.join(sorted(PRICE_UNITS))}")
    if payload.price <= 0:
        raise HTTPException(status_code=422, detail="Price must be positive")

    svc = Service(
        provider_id=current_user.id,
        title=payload.title,
        description=payload.description,
        category=payload.category,
        price=payload.price,
        price_unit=payload.price_unit,
        delivery_notes=payload.delivery_notes,
    )
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return _fmt_service(svc, current_user.handle)


@router.get("/")
def list_services(category: Optional[str] = None, db: Session = Depends(get_db)):
    """List all active services, optionally filtered by category."""
    q = db.query(Service).filter(Service.is_active == True)
    if category:
        q = q.filter(Service.category == category)
    services = q.order_by(Service.created_at.desc()).all()
    return [_fmt_service(s, s.provider.handle if s.provider else "?") for s in services]


@router.get("/{service_id}")
def get_service(service_id: int, db: Session = Depends(get_db)):
    svc = db.query(Service).filter(Service.id == service_id).first()
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")
    return _fmt_service(svc, svc.provider.handle if svc.provider else "?")


@router.delete("/{service_id}")
def remove_service(
    service_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = db.query(Service).filter(Service.id == service_id).first()
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")
    if svc.provider_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the provider can remove this listing")
    # Check no pending orders
    pending = db.query(ServiceOrder).filter(
        ServiceOrder.service_id == service_id,
        ServiceOrder.status.in_(["pending", "accepted"])
    ).count()
    if pending:
        raise HTTPException(status_code=409, detail=f"{pending} pending orders — resolve them first")
    svc.is_active = False
    db.commit()
    return {"removed": service_id}


@router.post("/{service_id}/order", status_code=201)
@limiter.limit("30/hour")
def order_service(
    request: Request,
    service_id: int,
    payload: OrderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Place an order. Tokens escrowed immediately."""
    svc = db.query(Service).filter(Service.id == service_id, Service.is_active == True).first()
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found or inactive")

    provider = db.query(User).filter(User.id == svc.provider_id).first()
    if current_user.id == provider.id:
        raise HTTPException(status_code=403, detail="Cannot order your own service")

    total = round(svc.price * payload.quantity, 6)
    fee = round(total * _config.get_fee_rate(), 6)
    total_cost = round(total + fee, 6)

    if current_user.token_balance < total_cost:
        raise HTTPException(status_code=402,
            detail=f"Need {total_cost:.4f} A ({total:.4f} + {fee:.4f} fee). Balance: {current_user.token_balance:.4f}")

    # Escrow
    current_user.token_balance = round(current_user.token_balance - total_cost, 6)
    order = ServiceOrder(
        service_id=service_id,
        buyer_id=current_user.id,
        quantity=payload.quantity,
        total_tokens=total,
        fee=fee,
        status="pending",
        request_note=payload.request_note,
    )
    db.add(order)
    db.add(TokenEvent(event_type="service_escrow", user_id=current_user.id,
                      amount=-total_cost, note=f"order for service #{service_id}"))
    db.commit()
    db.refresh(order)

    notify(db, provider.id, "service_order",
           f"@{current_user.handle} ordered '{svc.title}' (qty {payload.quantity}) — {total:.4f} A in escrow."
           + (f' Note: "{payload.request_note}"' if payload.request_note else ""))
    db.commit()

    return _fmt_order(order, current_user.handle, provider.handle, svc.title)


@router.post("/orders/{order_id}/accept")
def accept_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Provider accepts the order."""
    order, svc, provider, buyer = _get_order_parties(order_id, db, current_user, role="provider")
    if order.status != "pending":
        raise HTTPException(status_code=409, detail=f"Order is {order.status}")
    order.status = "accepted"
    order.updated_at = datetime.now(timezone.utc)
    db.commit()
    notify(db, buyer.id, "order_accepted",
           f"@{provider.handle} accepted your order for '{svc.title}'. Delivery coming.")
    db.commit()
    return _fmt_order(order, buyer.handle, provider.handle, svc.title)


@router.post("/orders/{order_id}/deliver")
def deliver_order(
    order_id: int,
    payload: DeliverOrder,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Provider marks order delivered. Tokens release to provider on buyer confirmation."""
    order, svc, provider, buyer = _get_order_parties(order_id, db, current_user, role="provider")
    if order.status not in ("pending", "accepted"):
        raise HTTPException(status_code=409, detail=f"Order is {order.status}")
    order.status = "delivered"
    order.delivery_note = payload.delivery_note
    order.updated_at = datetime.now(timezone.utc)
    db.commit()
    notify(db, buyer.id, "order_delivered",
           f"@{provider.handle} delivered '{svc.title}'. Confirm to release payment. Order #{order_id}.")
    db.commit()
    return _fmt_order(order, buyer.handle, provider.handle, svc.title)


@router.post("/orders/{order_id}/confirm")
def confirm_delivery(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Buyer confirms delivery — tokens released to provider."""
    order, svc, provider, buyer = _get_order_parties(order_id, db, current_user, role="buyer")
    if order.status != "delivered":
        raise HTTPException(status_code=409, detail=f"Order is {order.status} — provider must deliver first")

    # Release payment
    provider.token_balance = round(provider.token_balance + order.total_tokens, 6)
    order.status = "complete"
    order.updated_at = datetime.now(timezone.utc)

    db.add(BankLedger(event_type="service_fee", amount=order.fee,
                      note=f"order #{order_id} complete"))
    db.add(TokenEvent(event_type="service_payment", user_id=provider.id,
                      amount=order.total_tokens, note=f"order #{order_id} from @{buyer.handle}"))
    db.commit()
    recalculate_all_user_scores(db)

    notify(db, provider.id, "payment_released",
           f"@{buyer.handle} confirmed delivery. {order.total_tokens:.4f} A released to your balance.")
    db.commit()

    return _fmt_order(order, buyer.handle, provider.handle, svc.title)


@router.post("/orders/{order_id}/dispute")
def dispute_order(
    order_id: int,
    reason: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Buyer opens a dispute. Tokens stay in escrow until governance resolves."""
    order, svc, provider, buyer = _get_order_parties(order_id, db, current_user, role="buyer")
    if order.status not in ("delivered", "accepted"):
        raise HTTPException(status_code=409, detail=f"Cannot dispute order in status: {order.status}")
    order.status = "disputed"
    order.delivery_note = (order.delivery_note or "") + f"\n[DISPUTE] {reason}"
    order.updated_at = datetime.now(timezone.utc)
    db.commit()
    notify(db, provider.id, "order_disputed",
           f"@{buyer.handle} opened a dispute on order #{order_id}: {reason}")
    db.commit()
    return _fmt_order(order, buyer.handle, provider.handle, svc.title)


@router.post("/orders/{order_id}/cancel")
def cancel_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cancel a pending order — full refund to buyer."""
    order = db.query(ServiceOrder).filter(ServiceOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    svc = db.query(Service).filter(Service.id == order.service_id).first()
    buyer = db.query(User).filter(User.id == order.buyer_id).first()
    provider = db.query(User).filter(User.id == svc.provider_id).first() if svc else None

    if current_user.id not in (order.buyer_id, (provider.id if provider else -1)):
        raise HTTPException(status_code=403, detail="Only buyer or provider can cancel")
    if order.status not in ("pending",):
        raise HTTPException(status_code=409,
            detail=f"Cannot cancel order in status '{order.status}'. Open a dispute instead.")

    # Refund buyer (total_tokens + fee)
    refund = round(order.total_tokens + order.fee, 6)
    buyer.token_balance = round(buyer.token_balance + refund, 6)
    order.status = "cancelled"
    order.updated_at = datetime.now(timezone.utc)

    db.add(TokenEvent(event_type="service_refund", user_id=buyer.id,
                      amount=refund, note=f"order #{order_id} cancelled"))
    db.commit()
    notify(db, buyer.id, "order_cancelled", f"Order #{order_id} cancelled. {refund:.4f} A refunded.")
    db.commit()
    return {"cancelled": order_id, "refunded": refund}


@router.get("/orders/mine")
def my_orders(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All orders where you are buyer or provider."""
    bought = db.query(ServiceOrder).filter(ServiceOrder.buyer_id == current_user.id).all()
    sold_svcs = db.query(Service).filter(Service.provider_id == current_user.id).all()
    sold_ids = {s.id for s in sold_svcs}
    selling = db.query(ServiceOrder).filter(ServiceOrder.service_id.in_(sold_ids)).all() if sold_ids else []

    def fmt(o, role):
        svc = db.query(Service).filter(Service.id == o.service_id).first()
        buyer = db.query(User).filter(User.id == o.buyer_id).first()
        prov = db.query(User).filter(User.id == svc.provider_id).first() if svc else None
        d = _fmt_order(o, buyer.handle if buyer else "?", prov.handle if prov else "?", svc.title if svc else "?")
        d["role"] = role
        return d

    return {
        "buying": [fmt(o, "buyer") for o in bought],
        "selling": [fmt(o, "seller") for o in selling],
    }


def _get_order_parties(order_id, db, current_user, role):
    order = db.query(ServiceOrder).filter(ServiceOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    svc = db.query(Service).filter(Service.id == order.service_id).first()
    provider = db.query(User).filter(User.id == svc.provider_id).first() if svc else None
    buyer = db.query(User).filter(User.id == order.buyer_id).first()
    if role == "provider" and current_user.id != (provider.id if provider else -1):
        raise HTTPException(status_code=403, detail="Only the service provider can do this")
    if role == "buyer" and current_user.id != order.buyer_id:
        raise HTTPException(status_code=403, detail="Only the buyer can do this")
    return order, svc, provider, buyer


def _fmt_service(svc, provider_handle):
    return {
        "id": svc.id,
        "provider": provider_handle,
        "title": svc.title,
        "description": svc.description,
        "category": svc.category,
        "price": svc.price,
        "price_unit": svc.price_unit,
        "delivery_notes": svc.delivery_notes,
        "is_active": svc.is_active,
        "order_endpoint": f"POST /services/{svc.id}/order",
    }


def _fmt_order(order, buyer_handle, provider_handle, svc_title):
    return {
        "order_id": order.id,
        "service": svc_title,
        "buyer": buyer_handle,
        "provider": provider_handle,
        "quantity": order.quantity,
        "total_tokens": order.total_tokens,
        "fee": order.fee,
        "status": order.status,
        "request_note": order.request_note,
        "delivery_note": order.delivery_note,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }
