"""
Agora — simulation / bulk-import router (test use only)
"""

import hashlib
import random
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from db import get_db, User, Asset
from engine.scoring import bulk_rate_assets, recalculate_all_user_scores

router = APIRouter(prefix="/sim", tags=["sim"])


class BulkRatePayload(BaseModel):
    ratings: list[dict]  # [{user_id, asset_id, score}, ...]


@router.post("/bulk-rate")
def bulk_rate(payload: BulkRatePayload, db: Session = Depends(get_db)):
    submitted, skipped = bulk_rate_assets(db, payload.ratings)
    return {"submitted": submitted, "skipped": skipped}
