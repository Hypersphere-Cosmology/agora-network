"""
Agora — assets router
Auth required for submit/rate/flag.
Asset cap: 100 per user (governance-adjustable).
Notifications on rating events.
"""

import hashlib
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from db import get_db, Asset, User, Rating, PlagiarismFlag
from auth import get_current_user
from notifications import notify
from ratelimit import limiter
from engine.scoring import recalculate_asset_mint, recalculate_all_user_scores, check_and_prune, run_zombie_check

router = APIRouter(prefix="/assets", tags=["assets"])

ASSET_CAP = 100  # per user; changeable by governance vote


def compute_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode()).hexdigest()


class AssetSubmit(BaseModel):
    title: str
    description: Optional[str] = None
    content: str
    asset_type: Optional[str] = "concept"
    parent_id: Optional[int] = None
    tags: Optional[str] = None


class AssetOut(BaseModel):
    id: int
    title: str
    description: Optional[str]
    content: Optional[str] = None
    asset_type: str
    submitter_id: int
    parent_id: Optional[int]
    is_genesis: bool
    is_deleted: bool
    tokens_minted: float
    avg_rating: float
    rating_count: int
    tags: Optional[str] = None

    class Config:
        from_attributes = True


class RatingSubmit(BaseModel):
    score: float  # 1-10


class FlagSubmit(BaseModel):
    reason: Optional[str] = None


@router.post("/", status_code=201)
@limiter.limit("20/hour")
def submit_asset(
    request: Request,
    payload: AssetSubmit,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Asset cap check
    user_asset_count = db.query(Asset).filter(
        Asset.submitter_id == current_user.id,
        Asset.is_deleted == False,
    ).count()
    if user_asset_count >= ASSET_CAP:
        raise HTTPException(
            status_code=429,
            detail=f"Asset cap reached ({ASSET_CAP} assets per user). "
                   f"The network can raise this limit via governance vote."
        )

    content_hash = compute_hash(payload.content)
    if db.query(Asset).filter(Asset.content_hash == content_hash).first():
        raise HTTPException(status_code=409, detail="Duplicate content — hash already exists")

    # Semantic plagiarism check (lazy imports to avoid slow startup)
    import logging
    logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
    from engine.plagiarism import check_plagiarism
    from db import StorageConfig as SC

    block_row = db.query(SC).filter(SC.key == "plagiarism_block_threshold").first()
    warn_row  = db.query(SC).filter(SC.key == "plagiarism_warn_threshold").first()
    block_t = float(block_row.value_text) if block_row else 0.92
    warn_t  = float(warn_row.value_text)  if warn_row  else 0.75

    plg = check_plagiarism(payload.content, db, block_t, warn_t)

    if plg["status"] == "block":
        raise HTTPException(status_code=409, detail=plg["message"])

    # Warn but allow — carry the message into the response
    plagiarism_warning = plg.get("message") if plg["status"] == "warn" else None

    if payload.parent_id:
        parent = db.query(Asset).filter(
            Asset.id == payload.parent_id, Asset.is_deleted == False
        ).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent asset not found")

    asset = Asset(
        title=payload.title,
        description=payload.description,
        content=payload.content,
        content_hash=content_hash,
        asset_type=payload.asset_type,
        submitter_id=current_user.id,
        parent_id=payload.parent_id,
        tags=payload.tags if payload.tags else None,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)

    # Build response — include plagiarism warning if content was flagged but allowed
    if plagiarism_warning:
        return {
            "id": asset.id,
            "title": asset.title,
            "description": asset.description,
            "content": asset.content,
            "asset_type": asset.asset_type,
            "submitter_id": asset.submitter_id,
            "parent_id": asset.parent_id,
            "is_genesis": asset.is_genesis,
            "is_deleted": asset.is_deleted,
            "tokens_minted": asset.tokens_minted,
            "avg_rating": asset.avg_rating,
            "rating_count": asset.rating_count,
            "warning": plagiarism_warning,
        }
    return asset


@router.get("/tags")
def get_tags(db: Session = Depends(get_db)):
    """Return all unique tags in use across non-deleted assets."""
    assets = db.query(Asset).filter(Asset.is_deleted == False, Asset.tags != None).all()
    tag_set = set()
    for a in assets:
        if a.tags:
            for t in a.tags.split(","):
                tag_set.add(t.strip().lower())
    return {"tags": sorted(tag_set)}


@router.get("/", response_model=list[AssetOut])
def list_assets(
    request: Request,
    sort: str = "new",           # new | top | unrated
    q: str = None,               # search query (title + description)
    asset_type: str = None,      # filter by type
    submitter: str = None,       # filter by handle
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """List assets with optional sort, search, and filter."""
    from sqlalchemy import or_
    query = db.query(Asset).filter(Asset.is_deleted == False)

    if q:
        query = query.filter(
            (Asset.title.ilike(f"%{q}%")) | (Asset.description.ilike(f"%{q}%"))
        )
    if asset_type:
        query = query.filter(Asset.asset_type == asset_type)
    if submitter:
        user = db.query(User).filter(User.handle == submitter).first()
        if user:
            query = query.filter(Asset.submitter_id == user.id)

    # Tag filter: ?tags=infrastructure,code — match assets with at least one tag
    tags_param = request.query_params.get("tags")
    if tags_param:
        tag_list = [t.strip().lower() for t in tags_param.split(",") if t.strip()]
        if tag_list:
            conditions = [Asset.tags.contains(t) for t in tag_list]
            query = query.filter(or_(*conditions))

    if sort == "top":
        query = query.order_by(Asset.avg_rating.desc().nullslast(), Asset.rating_count.desc())
    elif sort == "unrated":
        query = query.filter(Asset.rating_count == 0).order_by(Asset.id.desc())
    else:  # new
        query = query.order_by(Asset.id.desc())

    return query.limit(limit).all()


@router.delete("/{asset_id}")
def delete_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Soft-delete an asset. Only owner can delete, and only if unrated."""
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.is_deleted == False).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.submitter_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own assets")
    if asset.rating_count > 0 and asset.avg_rating > 0:
        raise HTTPException(status_code=409, detail="Rated assets cannot be deleted. Use governance vote to remove.")
    asset.is_deleted = True
    db.commit()
    return {"ok": True, "deleted": asset_id}


@router.get("/{asset_id}", response_model=AssetOut)
def get_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Viewing requires having rated (except genesis and own assets)."""
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.is_deleted == False).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Content is always visible — rate after reading
    return asset


@router.post("/{asset_id}/rate", response_model=AssetOut)
@limiter.limit("120/hour")
def rate_asset(
    request: Request,
    asset_id: int,
    payload: RatingSubmit,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not (1.0 <= payload.score <= 10.0):
        raise HTTPException(status_code=422, detail="Score must be between 1 and 10")

    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.is_deleted == False).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    if current_user.id == asset.submitter_id:
        raise HTTPException(status_code=403, detail="Cannot rate your own asset")

    if db.query(Rating).filter(
        Rating.user_id == current_user.id, Rating.asset_id == asset_id
    ).first():
        raise HTTPException(status_code=409, detail="Already rated — re-rating is not permitted")

    db.add(Rating(user_id=current_user.id, asset_id=asset_id, score=payload.score))
    db.commit()

    # Recalculate
    prev_minted = asset.tokens_minted
    recalculate_asset_mint(db, asset_id)
    recalculate_all_user_scores(db)
    pruned = check_and_prune(db)
    run_zombie_check(db)  # Check for zombie threshold after every score update

    db.refresh(asset)

    # Notify submitter: rating received + tokens earned
    submitter = db.query(User).filter(User.id == asset.submitter_id).first()
    if submitter:
        token_delta = round(asset.tokens_minted - prev_minted, 6)
        notify(db, submitter.id, "asset_rated",
               f"Your asset '{asset.title}' was rated {payload.score}/10 by {current_user.handle}. "
               f"Token change: {token_delta:+.4f}. Current avg: {asset.avg_rating:.2f}.")

    # Notify if pruned
    for pid in pruned:
        pruned_asset = db.query(Asset).filter(Asset.id == pid).first()
        if pruned_asset:
            owner = db.query(User).filter(User.id == pruned_asset.submitter_id).first()
            if owner:
                notify(db, owner.id, "pruned",
                       f"Your asset '{pruned_asset.title}' was auto-pruned "
                       f"(avg rating ≤ 1.0 with sufficient rater coverage). "
                       f"You may re-submit with improvements.")

    db.commit()
    return asset


@router.post("/{asset_id}/flag", status_code=201)
def flag_plagiarism(
    asset_id: int,
    payload: FlagSubmit,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    db.add(PlagiarismFlag(
        asset_id=asset_id,
        flagged_by=current_user.id,
        reason=payload.reason
    ))
    db.commit()

    # Notify asset owner
    owner = db.query(User).filter(User.id == asset.submitter_id).first()
    if owner:
        notify(db, owner.id, "plagiarism_flag",
               f"Your asset '{asset.title}' has been flagged for plagiarism by {current_user.handle}. "
               f"Reason: {payload.reason or 'not specified'}. A community vote may follow.")
    db.commit()

    return {"ok": True, "message": "Flag recorded. Community vote may be triggered."}
