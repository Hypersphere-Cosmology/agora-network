"""
Agora — users router
Registration issues an API key (shown once — store it).
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from db import get_db, User, ApiKey
from auth import generate_api_key, store_api_key, get_current_user
from ratelimit import limiter

router = APIRouter(prefix="/users", tags=["users"])

ASSET_CAP_DEFAULT = 10  # changeable by governance vote


class UserCreate(BaseModel):
    handle: str
    display_name: Optional[str] = None
    agent_type: Optional[str] = "agent"


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

    user = User(
        handle=payload.handle,
        display_name=payload.display_name,
        agent_type=payload.agent_type,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    raw_key = generate_api_key()
    store_api_key(db, user.id, raw_key)

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


@router.get("/{handle}", response_model=UserOut)
def get_user(handle: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.handle == handle).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    return db.query(User).all()
