"""
Agora — notification helper
Push events into the notifications table.
"""

from sqlalchemy.orm import Session
from db import Notification


def notify(db: Session, user_id: int, event_type: str, message: str):
    """Create a notification for a user. Silent — never raises."""
    try:
        n = Notification(user_id=user_id, event_type=event_type, message=message)
        db.add(n)
        db.flush()
    except Exception:
        pass  # notifications are best-effort
