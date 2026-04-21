"""
Agora — bank router
View bank balance and ledger. Spending governed by vote.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from db import get_db, BankLedger

router = APIRouter(prefix="/bank", tags=["bank"])


@router.get("/balance")
def get_bank_balance(db: Session = Depends(get_db)):
    entries = db.query(BankLedger).all()
    balance = sum(e.amount for e in entries)
    return {"balance": round(balance, 6)}


@router.get("/ledger")
def get_ledger(limit: int = 50, db: Session = Depends(get_db)):
    entries = db.query(BankLedger).order_by(BankLedger.id.desc()).limit(limit).all()
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "amount": e.amount,
            "note": e.note,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]
