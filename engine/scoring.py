"""
Agora — scoring engine
Percentile-normalization for user ratings (0-10 per dimension, 0-30 total)
Token mint calculation
"""

from sqlalchemy.orm import Session
from sqlalchemy import func
from db import User, Asset, Rating, TokenEvent, BankLedger, StorageConfig
from typing import List


# ---------------------------------------------------------------------------
# Percentile normalization
# ---------------------------------------------------------------------------

def percentile_score(value: float, all_values: List[float]) -> float:
    """Normalize a value to 0-10 based on its percentile in the population."""
    if not all_values or len(all_values) == 1:
        return 0.0
    below = sum(1 for v in all_values if v < value)
    equal = sum(1 for v in all_values if v == value)
    # Midpoint percentile
    percentile = (below + 0.5 * equal) / len(all_values)
    return round(percentile * 10, 4)


def recalculate_all_user_scores(db: Session):
    """
    Recalculate all three score dimensions for every user.
    Uses bulk aggregate queries instead of per-user loops — O(n) not O(n²).
    """
    from db import Trade

    # --- Bulk aggregates via single queries ---

    # submission_raw: sum of avg_rating per submitter (non-deleted assets only)
    sub_rows = (
        db.query(Asset.submitter_id, func.sum(Asset.avg_rating))
        .filter(Asset.is_deleted == False)
        .group_by(Asset.submitter_id)
        .all()
    )
    sub_map = {row[0]: float(row[1]) for row in sub_rows}

    # rater_raw: count of ratings given per user
    rate_rows = (
        db.query(Rating.user_id, func.count(Rating.id))
        .group_by(Rating.user_id)
        .all()
    )
    rate_map = {row[0]: int(row[1]) for row in rate_rows}

    # trade_raw: count of trades (buyer + seller) per user
    buy_rows = (
        db.query(Trade.buyer_id, func.count(Trade.id))
        .group_by(Trade.buyer_id)
        .all()
    )
    sell_rows = (
        db.query(Trade.seller_id, func.count(Trade.id))
        .group_by(Trade.seller_id)
        .all()
    )
    trade_map = {}
    for uid, cnt in buy_rows:
        trade_map[uid] = trade_map.get(uid, 0) + cnt
    for uid, cnt in sell_rows:
        trade_map[uid] = trade_map.get(uid, 0) + cnt

    # --- Assign raws ---
    users = db.query(User).all()
    submission_raws = []
    rater_raws = []
    trade_raws = []

    for u in users:
        u.submission_raw = sub_map.get(u.id, 0.0)
        u.rater_raw = rate_map.get(u.id, 0)
        u.trade_raw = trade_map.get(u.id, 0)
        submission_raws.append(u.submission_raw)
        rater_raws.append(float(u.rater_raw))
        trade_raws.append(float(u.trade_raw))

    # --- Percentile normalize and assign scores ---
    for u in users:
        u.submission_score = percentile_score(u.submission_raw, submission_raws)
        u.rater_score = percentile_score(float(u.rater_raw), rater_raws)
        u.trade_score = percentile_score(float(u.trade_raw), trade_raws)
        u.total_score = round(u.submission_score + u.rater_score + u.trade_score, 4)

    db.commit()


# ---------------------------------------------------------------------------
# Token minting
# ---------------------------------------------------------------------------

def recalculate_asset_mint(db: Session, asset_id: int, defer_user_scores: bool = False):
    """
    Recalculate token mint for an asset and update submitter balance.
    Called whenever asset ratings change OR rater pool changes.

    Formula:
      pool = average rating of asset
      eligible_raters = total users - 1 (submitter excluded)
      participation_rate = raters_who_rated / eligible_raters
      submitter_share = pool × participation_rate
      bank_share = pool - submitter_share  (remainder)

    Tokens are additive diffs — we track total minted vs what submitter holds.
    """
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.is_deleted == False).first()
    if not asset:
        return

    ratings = db.query(Rating).filter(Rating.asset_id == asset_id).all()
    if not ratings:
        asset.avg_rating = 0.0
        asset.rating_count = 0
        db.commit()
        return

    total_users = db.query(User).count()
    eligible_raters = max(total_users - 1, 1)  # exclude submitter; floor at 1
    rater_count = len(ratings)
    avg = sum(r.score for r in ratings) / rater_count

    asset.avg_rating = round(avg, 4)
    asset.rating_count = rater_count

    participation_rate = rater_count / eligible_raters
    new_submitter_total = round(avg * participation_rate, 6)

    # Delta from previous state — only credit/debit the difference
    submitter_diff = round(new_submitter_total - asset.tokens_minted, 6)

    asset.tokens_minted = new_submitter_total

    # Get referral rates from StorageConfig
    l1_row = db.query(StorageConfig).filter(StorageConfig.key == "referral_rate_l1").first()
    l2_row = db.query(StorageConfig).filter(StorageConfig.key == "referral_rate_l2").first()
    l1_rate = float(l1_row.value_text) if l1_row and l1_row.value_text else 0.05
    l2_rate = float(l2_row.value_text) if l2_row and l2_row.value_text else 0.01

    # Find referral chain
    submitter = db.query(User).filter(User.id == asset.submitter_id).first()
    l1_ref = db.query(User).filter(User.handle == submitter.referred_by).first() if (submitter and submitter.referred_by) else None
    l2_ref = db.query(User).filter(User.handle == l1_ref.referred_by).first() if (l1_ref and l1_ref.referred_by) else None

    # Compute referral payouts from submitter's new total (proportional to diff)
    # Only pay when submitter_diff > 0 (new mint, not clawback)
    l1_payout = round(submitter_diff * l1_rate, 6) if (l1_ref and submitter_diff > 0) else 0.0
    l2_payout = round(submitter_diff * l2_rate, 6) if (l2_ref and submitter_diff > 0) else 0.0

    if l1_ref and l1_payout > 0:
        l1_ref.token_balance = round(l1_ref.token_balance + l1_payout, 6)
        db.add(TokenEvent(
            event_type="referral_l1",
            user_id=l1_ref.id,
            asset_id=asset_id,
            amount=l1_payout,
            note=f"referral from {submitter.handle if submitter else '?'}"
        ))

    if l2_ref and l2_payout > 0:
        l2_ref.token_balance = round(l2_ref.token_balance + l2_payout, 6)
        db.add(TokenEvent(
            event_type="referral_l2",
            user_id=l2_ref.id,
            asset_id=asset_id,
            amount=l2_payout,
            note=f"referral l2 from {submitter.handle if submitter else '?'}"
        ))

    # Bank gets remainder: pool - submitter_share - referral_payouts
    new_bank_total = round(avg * (1 - participation_rate) - l1_payout - l2_payout, 6)
    bank_diff = round(new_bank_total - asset.bank_minted, 6)
    asset.bank_minted = new_bank_total

    # Update submitter balance
    if submitter and submitter_diff != 0:
        submitter.token_balance = round(submitter.token_balance + submitter_diff, 6)
        db.add(TokenEvent(
            event_type="mint",
            user_id=submitter.id,
            asset_id=asset_id,
            amount=submitter_diff,
            note=f"avg={avg:.4f} participation={participation_rate:.4f}"
        ))

    # Bank gets its delta only
    if bank_diff != 0:
        db.add(BankLedger(
            event_type="mint_remainder",
            amount=bank_diff,
            note=f"asset={asset_id} avg={avg:.4f} participation={participation_rate:.4f}"
        ))

    db.commit()


# ---------------------------------------------------------------------------
# Bulk rating ingestion (for simulation / batch import)
# Defers user score recalc until all ratings are applied
# ---------------------------------------------------------------------------

def bulk_rate_assets(db: Session, ratings: list[dict]) -> tuple[int, int]:
    """
    ratings: list of {user_id, asset_id, score}
    Returns (submitted, skipped).
    Recalculates asset mints and user scores once at the end.
    """
    from db import Rating as RatingModel
    from datetime import datetime, timezone

    # Pre-load existing rating pairs for fast dupe check
    existing = set(
        db.query(RatingModel.user_id, RatingModel.asset_id).all()
    )

    new_ratings = []
    skipped = 0
    asset_ids_affected = set()

    for r in ratings:
        key = (r["user_id"], r["asset_id"])
        if key in existing:
            skipped += 1
            continue
        new_ratings.append(RatingModel(
            user_id=r["user_id"],
            asset_id=r["asset_id"],
            score=r["score"],
        ))
        existing.add(key)
        asset_ids_affected.add(r["asset_id"])

    if not new_ratings:
        return 0, skipped

    db.bulk_save_objects(new_ratings)
    db.commit()

    # Recalc mint for each affected asset
    for asset_id in asset_ids_affected:
        recalculate_asset_mint(db, asset_id)

    # Single user score recalc at the end
    recalculate_all_user_scores(db)
    check_and_prune(db)

    return len(new_ratings), skipped


# ---------------------------------------------------------------------------
# Pruning check
# ---------------------------------------------------------------------------

PRUNE_MIN_RATER_FRACTION = 0.50   # 50% of raters must have rated
PRUNE_MAX_AVG = 1.0               # avg ≤ 1.0

def check_and_prune(db: Session) -> List[int]:
    """
    Auto-delete assets that meet pruning criteria.
    Returns list of pruned asset IDs.
    """
    total_raters = db.query(User).count()
    if total_raters == 0:
        return []

    min_raters = total_raters * PRUNE_MIN_RATER_FRACTION
    pruned = []

    assets = db.query(Asset).filter(Asset.is_deleted == False, Asset.is_genesis == False).all()
    for asset in assets:
        if asset.rating_count >= min_raters and asset.avg_rating <= PRUNE_MAX_AVG:
            asset.is_deleted = True
            pruned.append(asset.id)

    if pruned:
        db.commit()

    return pruned
