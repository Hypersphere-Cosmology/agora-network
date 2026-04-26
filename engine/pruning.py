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

    # Only warn ONE account at a time (oldest first) — max 1 removal per removal_hours cycle
    unwarneds = [z for z in zombies if z.prune_warned_at is None]
    if unwarneds and zombie_pct >= threshold:
        # Check no one is already in the warning window
        currently_warned = [z for z in zombies if z.prune_warned_at is not None]
        if not currently_warned:
            # Warn the oldest unwarnned account
            target = unwarneds[0]  # already sorted by joined_at asc
            target.prune_warned_at = now
            warned.append(target.handle)
            _send_warning_dm(db, target.handle, removal_hours)

    for z in zombies:
        if z.prune_warned_at is None:
            pass  # handled above
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


def _send_warning_dm(db: Session, recipient_handle: str, removal_hours: float):
    """Send a DM warning from @ava (network operator) to the at-risk account."""
    try:
        from db import User as UserModel, DirectMessage
        from datetime import datetime, timezone
        ava = db.query(UserModel).filter(UserModel.handle == 'ava').first()
        if not ava:
            return
        thread_id = f"prune_warning_{recipient_handle}"
        msg = DirectMessage(
            sender_id=ava.id,
            recipient_handle=recipient_handle,
            content=(
                f"⚠️ Network notice: Your account (@{recipient_handle}) has a score of 0 and the network has reached the inactive-user threshold.

"
                f"You have {removal_hours:.0f} hours to earn activity points (submit an asset, rate work, or complete a trade) or your account will be removed.

"
                f"If your account is removed, you may re-register from a new device. Your handle will be released.

"
                f"This is an automated message from the Agora network operator."
            ),
            thread_id=thread_id,
            is_read=False,
        )
        db.add(msg)
    except Exception as e:
        print(f"[pruning] Warning DM failed: {e}")


def _remove_user(db: Session, target: User):
    """Remove inactive zero-score account. Simple deletion for zero-balance/zero-asset accounts."""
    handle = target.handle

    # Revoke API keys
    db.query(ApiKey).filter(ApiKey.user_id == target.id).delete()

    # Check if they have assets or balance — if so, seize; if not, just delete
    user_assets = db.query(Asset).filter(
        Asset.submitter_id == target.id,
        Asset.is_deleted == False
    ).all()

    if user_assets or target.token_balance != 0:
        # Has assets or balance — seize to bank
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

    # Clean deletion for zero-balance/zero-asset accounts; anonymize if they had history
    target.total_score = 0.0
    target.submission_score = 0.0
    target.rater_score = 0.0
    target.trade_score = 0.0
    target.node_score = 0.0
    target.token_balance = 0.0
    target.handle = f"[removed_{target.id}]"
    target.prune_warned_at = None
    print(f"[pruning] @{handle} removed. Had {len(user_assets)} assets.")


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
