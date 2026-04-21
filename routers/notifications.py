"""
Agora — notifications router
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from db import get_db, Notification, User
from auth import get_current_user

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: int
    event_type: str
    message: str
    read: bool
    created_at: str

    class Config:
        from_attributes = True


@router.get("/", response_model=list[NotificationOut])
def get_notifications(
    unread_only: bool = False,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Notification).filter(Notification.user_id == current_user.id)
    if unread_only:
        q = q.filter(Notification.read == False)
    items = q.order_by(Notification.id.desc()).limit(limit).all()
    return [
        NotificationOut(
            id=n.id,
            event_type=n.event_type,
            message=n.message,
            read=n.read,
            created_at=n.created_at.isoformat() if n.created_at else "",
        )
        for n in items
    ]


@router.post("/mark-read")
def mark_read(
    notification_ids: list[int],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db.query(Notification).filter(
        Notification.id.in_(notification_ids),
        Notification.user_id == current_user.id,
    ).update({"read": True}, synchronize_session=False)
    db.commit()
    return {"ok": True}


@router.post("/mark-all-read")
def mark_all_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.read == False,
    ).update({"read": True}, synchronize_session=False)
    db.commit()
    return {"ok": True}


@router.get("/unread-count")
def unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    count = db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.read == False,
    ).count()
    return {"unread": count}
