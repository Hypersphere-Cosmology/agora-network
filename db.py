"""
Agora — database models and initialization
SQLAlchemy + SQLite
"""

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    DateTime, ForeignKey, Text, UniqueConstraint, text
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from datetime import datetime, timezone

DATABASE_URL = "sqlite:///./agora.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    handle = Column(String, unique=True, index=True, nullable=False)
    display_name = Column(String, nullable=True)
    agent_type = Column(String, default="agent")  # "agent" | "human"
    token_balance = Column(Float, default=0.0)
    joined_at = Column(DateTime, default=utcnow)

    # Computed ratings (recalculated, stored for fast lookup)
    submission_raw = Column(Float, default=0.0)   # sum of own assets' avg ratings
    rater_raw = Column(Integer, default=0)         # count of ratings given
    trade_raw = Column(Integer, default=0)         # count of completed trades
    submission_score = Column(Float, default=0.0)  # percentile-normalized 0-10
    rater_score = Column(Float, default=0.0)
    trade_score = Column(Float, default=0.0)
    total_score = Column(Float, default=0.0)       # 0-30

    referred_by = Column(String, nullable=True)    # handle of who referred this user
    referral_code = Column(String, nullable=True)  # their own referral code (= their handle)

    assets = relationship("Asset", back_populates="submitter")
    ratings_given = relationship("Rating", back_populates="rater")
    listings = relationship("Listing", foreign_keys="[Listing.seller_id]", back_populates="seller")


class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    content_hash = Column(String, unique=True, index=True, nullable=False)
    content = Column(Text, nullable=True)
    asset_type = Column(String, default="concept")  # concept | code | data | governance | etc
    submitter_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    parent_id = Column(Integer, ForeignKey("assets.id"), nullable=True)  # fork parent
    is_genesis = Column(Boolean, default=False)
    is_deleted = Column(Boolean, default=False)
    tokens_minted = Column(Float, default=0.0)
    submitted_at = Column(DateTime, default=utcnow)

    # Cached stats
    avg_rating = Column(Float, default=0.0)
    rating_count = Column(Integer, default=0)
    bank_minted = Column(Float, default=0.0)  # bank's cumulative share from this asset

    submitter = relationship("User", back_populates="assets")
    ratings = relationship("Rating", back_populates="asset")
    forks = relationship(
        "Asset",
        primaryjoin="Asset.id == foreign(Asset.parent_id)",
        uselist=True,
    )


class Rating(Base):
    __tablename__ = "ratings"
    __table_args__ = (UniqueConstraint("user_id", "asset_id", name="uq_user_asset_rating"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    score = Column(Float, nullable=False)  # 1-10
    rated_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)

    rater = relationship("User", back_populates="ratings_given")
    asset = relationship("Asset", back_populates="ratings")


class TokenEvent(Base):
    __tablename__ = "token_events"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String, nullable=False)  # mint | trade_fee | governance | clawback
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=True)
    amount = Column(Float, nullable=False)
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class BankLedger(Base):
    __tablename__ = "bank_ledger"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String, nullable=False)  # trade_fee | mint_remainder | governance_spend
    amount = Column(Float, nullable=False)
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class Listing(Base):
    __tablename__ = "listings"

    id = Column(Integer, primary_key=True, index=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=True)   # None = bounty (no asset)
    seller_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    price = Column(Float, nullable=False)
    memo = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    listed_at = Column(DateTime, default=utcnow)

    requires_approval = Column(Boolean, default=False)
    pending_claimant_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    asset = relationship("Asset", foreign_keys=[asset_id])
    seller = relationship("User", foreign_keys=[seller_id], back_populates="listings")


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    listing_id = Column(Integer, ForeignKey("listings.id"), nullable=False)
    buyer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    seller_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    price = Column(Float, nullable=False)
    fee = Column(Float, nullable=False)
    completed_at = Column(DateTime, default=utcnow)

    listing = relationship("Listing")


class Proposal(Base):
    __tablename__ = "proposals"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    proposer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=True)  # vote is an asset
    quorum = Column(Float, default=0.5)       # fraction of eligible voters needed
    is_closed = Column(Boolean, default=False)
    winning_option = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    closed_at = Column(DateTime, nullable=True)

    options = relationship("ProposalOption", back_populates="proposal")


class ProposalOption(Base):
    __tablename__ = "proposal_options"

    id = Column(Integer, primary_key=True, index=True)
    proposal_id = Column(Integer, ForeignKey("proposals.id"), nullable=False)
    label = Column(String, nullable=False)
    vote_count = Column(Integer, default=0)    # number of voters who ranked this option
    borda_points = Column(Integer, default=0)  # unused, kept for compat
    rank_total = Column(Integer, default=0)    # sum of ranks given (lower = more preferred)

    proposal = relationship("Proposal", back_populates="options")


class Vote(Base):
    """One row per voter per option, with rank (1=top choice)."""
    __tablename__ = "votes"
    __table_args__ = (
        UniqueConstraint("user_id", "proposal_id", "option_id", name="uq_voter_option"),
        UniqueConstraint("user_id", "proposal_id", "rank", name="uq_voter_rank"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    proposal_id = Column(Integer, ForeignKey("proposals.id"), nullable=False)
    option_id = Column(Integer, ForeignKey("proposal_options.id"), nullable=False)
    rank = Column(Integer, nullable=False)   # 1 = top choice, 2 = second, etc.
    voted_at = Column(DateTime, default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    key_hash = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    last_used_at = Column(DateTime, nullable=True)

    user = relationship("User")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    event_type = Column(String, nullable=False)  # asset_rated | token_earned | trade_completed | proposal_opened | pruned
    message = Column(Text, nullable=False)
    read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)


class PlagiarismFlag(Base):
    __tablename__ = "plagiarism_flags"

    id = Column(Integer, primary_key=True, index=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    flagged_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    reason = Column(Text, nullable=True)
    resolved = Column(Boolean, default=False)
    upheld = Column(Boolean, nullable=True)
    flagged_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init_db():
    Base.metadata.create_all(bind=engine)

    # Safe migrations for SQLite (ADD COLUMN if not exists)
    with engine.connect() as conn:
        for col, coltype in [("referred_by", "VARCHAR"), ("referral_code", "VARCHAR")]:
            try:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {coltype}"))
                conn.commit()
            except Exception:
                pass  # column already exists

    # Seed referral rates into StorageConfig
    db = SessionLocal()
    try:
        for key, val in [("referral_rate_l1", "0.05"), ("referral_rate_l2", "0.01")]:
            if not db.query(StorageConfig).filter(StorageConfig.key == key).first():
                db.add(StorageConfig(key=key, value_text=val))
        db.commit()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_type = Column(String, nullable=False)   # 'asset' or 'bounty'
    thread_id = Column(Integer, nullable=False)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    is_deleted = Column(Boolean, default=False)

    author = relationship("User")


class Service(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    category = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    price_unit = Column(String, nullable=False)
    delivery_notes = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)

    provider = relationship("User")


class ServiceOrder(Base):
    __tablename__ = "service_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=False)
    buyer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    quantity = Column(Float, default=1.0)
    total_tokens = Column(Float, nullable=False)
    fee = Column(Float, nullable=False)
    status = Column(String, default="pending")
    request_note = Column(String, nullable=True)
    delivery_note = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)

    service = relationship("Service")
    buyer = relationship("User", foreign_keys=[buyer_id])


class AssetFile(Base):
    __tablename__ = "asset_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    uploader_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    filename = Column(String, nullable=False)
    mime_type = Column(String, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    storage_path = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow)

    uploader = relationship("User")


class StorageConfig(Base):
    __tablename__ = "storage_config"

    key = Column(String, primary_key=True)
    value_int = Column(Integer, nullable=True)
    value_text = Column(String, nullable=True)
    updated_at = Column(DateTime, default=utcnow)


class ShardMap(Base):
    __tablename__ = "shard_map"

    id = Column(Integer, primary_key=True)
    asset_content_hash = Column(String, index=True)   # the asset's content_hash
    responsible_node_ids = Column(String)               # JSON array of node_ids
    updated_at = Column(DateTime, default=utcnow)


class TokenPurchase(Base):
    __tablename__ = "token_purchases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    buyer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount_tokens = Column(Float, nullable=False)
    amount_usd = Column(Float, nullable=False)
    payment_method = Column(String, nullable=False)
    payment_address = Column(String, nullable=True)
    txid = Column(String, nullable=True)
    status = Column(String, default="pending")
    confirmed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)

    buyer = relationship("User", foreign_keys=[buyer_id])


class PaymentAddress(Base):
    __tablename__ = "payment_addresses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    currency = Column(String, nullable=False)
    address = Column(String, nullable=False)
    label = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)


class ServiceFeedback(Base):
    __tablename__ = "service_feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False)
    reviewer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reviewee_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    role = Column(String, nullable=False)   # 'buyer' or 'provider'
    rating = Column(Integer, nullable=False)  # 1-5
    comment = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    reviewer = relationship("User", foreign_keys=[reviewer_id])
    reviewee = relationship("User", foreign_keys=[reviewee_id])
