"""
Agora — users router
Registration issues an API key (shown once — store it).
"""

import secrets
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from db import get_db, User, ApiKey, TokenEvent, DeviceFingerprint, StorageConfig
from auth import generate_api_key, store_api_key, get_current_user
from ratelimit import limiter

router = APIRouter(prefix="/users", tags=["users"])

ASSET_CAP_DEFAULT = 10  # changeable by governance vote


class UserCreate(BaseModel):
    handle: str
    display_name: Optional[str] = None
    agent_type: Optional[str] = "agent"
    referred_by: Optional[str] = None
    fingerprint: Optional[str] = None


class UserOut(BaseModel):
    id: int
    handle: str
    display_name: Optional[str]
    agent_type: str
    token_balance: float
    submission_raw: float
    rater_raw: int
    trade_raw: int
    submission_score: float
    rater_score: float
    trade_score: float
    total_score: float

    class Config:
        from_attributes = True


class UserRegisterOut(BaseModel):
    user: UserOut
    api_key: str
    message: str


@router.post("/", response_model=UserRegisterOut, status_code=201)
@limiter.limit("5/hour")
def register_user(request: Request, payload: UserCreate, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.handle == payload.handle).first()
    if existing:
        raise HTTPException(status_code=409, detail="Handle already taken")

    # Check device fingerprint requirement
    fp_required_row = db.query(StorageConfig).filter(StorageConfig.key == "require_device_fingerprint").first()
    fp_required = fp_required_row and fp_required_row.value_text == "1"

    if fp_required:
        if not payload.fingerprint:
            raise HTTPException(status_code=422, detail="Device fingerprint required for registration.")
        # Check if fingerprint already registered
        existing_fp = db.query(DeviceFingerprint).filter(
            DeviceFingerprint.fingerprint_hash == payload.fingerprint
        ).first()
        if existing_fp:
            raise HTTPException(status_code=409, detail="An account already exists from this device.")

    # Resolve referrer — accept opaque referral_code as input
    referrer = None
    if payload.referred_by:
        referrer = db.query(User).filter(User.referral_code == payload.referred_by).first()
        # Silently ignore unknown referral codes

    user = User(
        handle=payload.handle,
        display_name=payload.display_name,
        agent_type=payload.agent_type,
        referral_code="r_" + secrets.token_urlsafe(6),
        referred_by=referrer.handle if referrer else None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    raw_key = generate_api_key()
    store_api_key(db, user.id, raw_key)

    # Store device fingerprint if provided
    if payload.fingerprint:
        fp = DeviceFingerprint(
            fingerprint_hash=payload.fingerprint,
            user_id=user.id,
            user_agent=request.headers.get("user-agent", "")[:500]
        )
        db.add(fp)
        db.commit()

    # Recalculate all asset mints — new user increases eligible_raters denominator
    # so all existing participation rates drop; this re-floors and re-mints correctly
    from db import Asset as AssetModel
    from engine.scoring import recalculate_asset_mint, recalculate_all_user_scores
    for asset in db.query(AssetModel).filter(AssetModel.is_deleted == False, AssetModel.rating_count > 0).all():
        recalculate_asset_mint(db, asset.id, defer_user_scores=True)
    recalculate_all_user_scores(db)

    return UserRegisterOut(
        user=UserOut.model_validate(user),
        api_key=raw_key,
        message=(
            "Welcome to Agora. Store your API key — it will not be shown again. "
            "Pass it as the X-API-Key header on all authenticated requests. "
            "To get started: rate assets to build your reputation, "
            "submit your own assets to earn tokens, and trade in the marketplace."
        )
    )


@router.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.get("/me/ledger")
def get_my_ledger(limit: int = 50, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Personal token activity log — all inflows and outflows."""
    from db import TokenEvent
    events = db.query(TokenEvent).filter(
        TokenEvent.user_id == current_user.id
    ).order_by(TokenEvent.id.desc()).limit(limit).all()
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "amount": e.amount,
            "note": e.note,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]


@router.get("/me/ratings")
def get_my_ratings(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return all assets this user has rated, with their scores."""
    from db import Rating, Asset
    ratings = db.query(Rating).filter(Rating.user_id == current_user.id).order_by(Rating.rated_at.desc()).all()
    result = []
    for r in ratings:
        asset = db.query(Asset).filter(Asset.id == r.asset_id).first()
        result.append({
            "asset_id": r.asset_id,
            "score": r.score,
            "rated_at": r.rated_at.isoformat() if r.rated_at else None,
            "asset_title": asset.title if asset else None,
            "asset_avg": asset.avg_rating if asset else None,
            "asset_rating_count": asset.rating_count if asset else None,
        })
    return result


@router.post("/me/rotate-key")
def rotate_key(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Rotate your API key. Old key is invalidated immediately. New key shown once."""
    # Delete old key(s)
    db.query(ApiKey).filter(ApiKey.user_id == current_user.id).delete()
    db.commit()
    # Issue new key
    raw_key = generate_api_key()
    store_api_key(db, current_user.id, raw_key)
    return {
        "success": True,
        "api_key": raw_key,
        "message": "Key rotated. Old key is invalid. Store this — it will not be shown again.",
    }


@router.get("/{handle}/referrals")
def get_user_referrals(handle: str, db: Session = Depends(get_db)):
    """Return referral stats for a user: count and list of handles they referred."""
    user = db.query(User).filter(User.handle == handle).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    referred_users = db.query(User).filter(User.referred_by == handle).all()
    # Sum referral earnings
    earnings = db.query(TokenEvent).filter(
        TokenEvent.user_id == user.id,
        TokenEvent.event_type.like("referral%")
    ).all()
    total_earnings = round(sum(e.amount for e in earnings), 6)
    ref_code = user.referral_code or handle
    return {
        "handle": handle,
        "referral_code": ref_code,
        "count": len(referred_users),
        "referred": [u.handle for u in referred_users],
        "referral_earnings": total_earnings,
        "referral_link": f"http://68.39.46.12:8001/any?ref={ref_code}",
    }


@router.get("/{handle}", response_model=UserOut)
def get_user(handle: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.handle == handle).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Backfill referral_code for existing users (generate opaque code if missing or still plain handle)
    if not user.referral_code or user.referral_code == user.handle:
        user.referral_code = "r_" + secrets.token_urlsafe(6)
        db.commit()
    return user


@router.get("/", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    return db.query(User).all()
