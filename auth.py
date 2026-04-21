"""
Agora — API key authentication
Each user gets one API key on registration.
Pass as header: X-API-Key: agora_<key>
"""

import secrets
import hashlib
from fastapi import Header, HTTPException, Depends
from sqlalchemy.orm import Session
from db import get_db, ApiKey, User
from datetime import datetime, timezone


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_api_key() -> str:
    """Generate a new raw API key. Returns the plaintext (shown once)."""
    return "agora_" + secrets.token_urlsafe(32)


def store_api_key(db: Session, user_id: int, raw_key: str) -> ApiKey:
    """Hash and store an API key for a user."""
    entry = ApiKey(user_id=user_id, key_hash=_hash_key(raw_key))
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def get_current_user(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency — resolves X-API-Key to a User or raises 401."""
    key_hash = _hash_key(x_api_key)
    entry = db.query(ApiKey).filter(ApiKey.key_hash == key_hash).first()
    if not entry:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    # Update last used
    entry.last_used_at = datetime.now(timezone.utc)
    db.commit()

    return entry.user


def get_current_user_optional(
    x_api_key: str = Header(None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> User | None:
    """Optional auth — returns None if no key provided."""
    if not x_api_key:
        return None
    return get_current_user(x_api_key=x_api_key, db=db)
