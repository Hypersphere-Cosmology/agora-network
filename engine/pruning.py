"""
Agora — Dead user pruning engine.

Rules (governance-adjustable via StorageConfig):
  - ZOMBIE_THRESHOLD (default 0.20): when zero-score users reach this % of total,
    trigger warning for the oldest zero-score accounts.
  - REMOVAL_HOURS (default 72): hours after warning before removal.
  - Oldest accounts warned first (by joined_at ascending).
  - Accounts that earn any score before the deadline are safe.
  - On removal: assets seized to bank, debt absorbed, account anonymized.
"""
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from db import User, ApiKey, Asset, BankLedger, StorageConfig
import json


def get_config(db: Session, key: str, default: float) -> float:
    row = db.query(StorageConfig).filter(StorageConfig.key == key).first()
    return float(row.value_text) if row and row.value_text else default


def check_and_warn(db: Session) -> dict:
    """
    Check if zombie threshold is exceeded. If so, warn oldest zero-score users.
    Returns summary of actions taken.
    """
    threshold = get_config(db, "zombie_threshold", 0.20)
    removal_hours = get_config(db, "zombie_removal_hours", 72.0)

    total = db.query(User).count()
    if total == 0:
        return {"checked": True, "action": "none", "reason": "no users"}

    zombies = db.query(User).filter(
        User.total_score <= 0,
        User.handle.notlike("[removed_%")  # skip already removed
    ).order_by(User.joined_at.asc()).all()

    zombie_pct = len(zombies) / total

    warned = []
    removed = []
    now = datetime.now(timezone.utc)

    for z in zombies:
        if z.prune_warned_at is None:
            # Not yet warned — only warn if threshold exceeded
            if zombie_pct >= threshold:
                z.prune_warned_at = now
                warned.append(z.handle)
        else:
            # Already warned — check if deadline passed
            warned_at = z.prune_warned_at
            if warned_at.tzinfo is None:
                warned_at = warned_at.replace(tzinfo=timezone.utc)
            deadline = warned_at + timedelta(hours=removal_hours)
            if now >= deadline:
                # Remove the user
                _remove_user(db, z)
                removed.append(z.handle)

    if warned or removed:
        db.commit()

    return {
        "checked": True,
        "total_users": total,
        "zombie_count": len(zombies),
        "zombie_pct": round(zombie_pct * 100, 1),
        "threshold_pct": threshold * 100,
        "threshold_exceeded": zombie_pct >= threshold,
        "warned": warned,
        "removed": removed,
        "removal_hours": removal_hours,
    }


def _remove_user(db: Session, target: User):
    """Seize assets, absorb debt, anonymize account."""
    handle = target.handle

    # Revoke API keys
    db.query(ApiKey).filter(ApiKey.user_id == target.id).delete()

    # Seize assets → tag as bank-seized
    user_assets = db.query(Asset).filter(
        Asset.submitter_id == target.id,
        Asset.is_deleted == False
    ).all()
    for asset in user_assets:
        existing_tags = asset.tags or ""
        if "bank-seized" not in existing_tags:
            asset.tags = (existing_tags + ",bank-seized").strip(",")
        if asset.avg_rating and asset.avg_rating > 0:
            db.add(BankLedger(
                event_type="asset_seizure",
                amount=asset.avg_rating,
                note=f"Asset #{asset.id} seized from @{handle} (prune)"
            ))

    # Handle balance
    if target.token_balance > 0:
        db.add(BankLedger(
            event_type="user_prune_balance_seized",
            amount=target.token_balance,
            note=f"Balance seized from @{handle} (prune)"
        ))
    elif target.token_balance < 0:
        db.add(BankLedger(
            event_type="user_prune_debt_absorbed",
            amount=target.token_balance,
            note=f"Debt absorbed from @{handle} (prune)"
        ))

    # Anonymize
    target.total_score = 0.0
    target.submission_score = 0.0
    target.rater_score = 0.0
    target.trade_score = 0.0
    target.token_balance = 0.0
    target.handle = f"[removed_{target.id}]"
    target.prune_warned_at = None


def get_prune_status(db: Session) -> dict:
    """Current pruning status — for display in network parameters."""
    threshold = get_config(db, "zombie_threshold", 0.20)
    removal_hours = get_config(db, "zombie_removal_hours", 72.0)
    total = db.query(User).count()
    zombies = db.query(User).filter(
        User.total_score <= 0,
        User.handle.notlike("[removed_%")
    ).all()

    warned = [z for z in zombies if z.prune_warned_at is not None]
    now = datetime.now(timezone.utc)
    deadlines = []
    for w in warned:
        wa = w.prune_warned_at
        if wa.tzinfo is None:
            wa = wa.replace(tzinfo=timezone.utc)
        deadline = wa + timedelta(hours=removal_hours)
        deadlines.append({
            "handle": w.handle,
            "warned_at": wa.isoformat(),
            "removes_at": deadline.isoformat(),
            "hours_remaining": max(0, (deadline - now).total_seconds() / 3600),
        })

    return {
        "total_users": total,
        "zombie_count": len(zombies),
        "zombie_pct": round(len(zombies) / total * 100, 1) if total else 0,
        "threshold_pct": threshold * 100,
        "threshold_exceeded": (len(zombies) / total >= threshold) if total else False,
        "removal_hours": removal_hours,
        "accounts_warned": deadlines,
    }
