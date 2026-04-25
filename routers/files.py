"""
Agora — file attachments for assets.
Upload files to assets, download them, track storage usage.
Limits are governance-adjustable via storage_config table.
"""

import os
import hashlib
import mimetypes
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from db import get_db, User, Asset, AssetFile, StorageConfig
from auth import get_current_user
from notifications import notify
from ratelimit import limiter

UPLOAD_DIR = Path(__file__).parent.parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

router = APIRouter(prefix="/files", tags=["files"])


def get_config(db: Session, key: str, default_int=None, default_text=None):
    row = db.query(StorageConfig).filter(StorageConfig.key == key).first()
    if not row:
        return default_int if default_int is not None else default_text
    return row.value_int if row.value_int is not None else row.value_text


def get_storage_stats(db: Session):
    from sqlalchemy import func
    total = db.query(func.sum(AssetFile.size_bytes)).scalar() or 0
    return total


def get_user_storage(db: Session, user_id: int):
    from sqlalchemy import func
    used = db.query(func.sum(AssetFile.size_bytes)).filter(
        AssetFile.uploader_id == user_id).scalar() or 0
    return used


def fmt_bytes(b: int) -> str:
    if b < 1024: return f"{b} B"
    if b < 1048576: return f"{b/1024:.1f} KB"
    if b < 1073741824: return f"{b/1048576:.1f} MB"
    return f"{b/1073741824:.2f} GB"


@router.post("/assets/{asset_id}", status_code=201)
@limiter.limit("20/hour")
async def upload_file(
    request: Request,
    asset_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Attach a file to an asset. Only the asset submitter can attach files."""
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.is_deleted == False).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.submitter_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the asset submitter can attach files")

    # Check filename / extension
    filename = file.filename or "attachment"
    ext = Path(filename).suffix.lstrip(".").lower()
    allowed_raw = get_config(db, "allowed_types", default_text="pdf,md,txt,json,csv,png,jpg,jpeg,gif,mp3,wav,zip")
    allowed = {e.strip() for e in (allowed_raw or "").split(",")}
    if ext and ext not in allowed:
        raise HTTPException(status_code=422,
            detail=f"File type '.{ext}' not allowed. Allowed: {', '.join(sorted(allowed))}")

    # Read file content
    content = await file.read()
    size = len(content)

    # Enforce per-file limit
    max_file = get_config(db, "max_file_bytes", default_int=52428800)  # 50MB
    if size > max_file:
        raise HTTPException(status_code=413,
            detail=f"File too large: {fmt_bytes(size)}. Limit: {fmt_bytes(max_file)}")

    # Enforce per-user limit
    user_used = get_user_storage(db, current_user.id)
    max_user = get_config(db, "max_user_bytes", default_int=524288000)  # 500MB
    if user_used + size > max_user:
        raise HTTPException(status_code=413,
            detail=f"User storage limit reached: using {fmt_bytes(user_used)}, limit {fmt_bytes(max_user)}")

    # Enforce network limit
    net_used = get_storage_stats(db)
    max_net = get_config(db, "max_network_bytes", default_int=10737418240)  # 10GB
    if net_used + size > max_net:
        raise HTTPException(status_code=413,
            detail=f"Network storage limit reached: {fmt_bytes(net_used)}/{fmt_bytes(max_net)}")

    # Store file — content-addressed by hash to dedup
    file_hash = hashlib.sha256(content).hexdigest()
    safe_name = f"{file_hash[:16]}_{Path(filename).name}"
    storage_path = UPLOAD_DIR / safe_name
    storage_path.write_bytes(content)

    # Detect mime type
    mime = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    record = AssetFile(
        asset_id=asset_id,
        uploader_id=current_user.id,
        filename=filename,
        mime_type=mime,
        size_bytes=size,
        storage_path=str(storage_path),
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    return {
        "file_id": record.id,
        "asset_id": asset_id,
        "filename": filename,
        "size": fmt_bytes(size),
        "size_bytes": size,
        "mime_type": mime,
        "download_url": f"/files/{record.id}/download",
        "user_storage_used": fmt_bytes(user_used + size),
        "user_storage_limit": fmt_bytes(max_user),
    }


@router.get("/assets/{asset_id}")
def list_asset_files(asset_id: int, db: Session = Depends(get_db)):
    """List all files attached to an asset."""
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    files = db.query(AssetFile).filter(AssetFile.asset_id == asset_id).all()
    return [
        {
            "file_id": f.id,
            "filename": f.filename,
            "mime_type": f.mime_type,
            "size": fmt_bytes(f.size_bytes),
            "size_bytes": f.size_bytes,
            "uploaded_by": f.uploader.handle if f.uploader else "?",
            "created_at": f.created_at.isoformat() if f.created_at else None,
            "download_url": f"/files/{f.id}/download",
        }
        for f in files
    ]


@router.get("/{file_id}/download")
def download_file(file_id: int, db: Session = Depends(get_db)):
    """Download a file by ID."""
    f = db.query(AssetFile).filter(AssetFile.id == file_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    if not Path(f.storage_path).exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(
        path=f.storage_path,
        media_type=f.mime_type,
        filename=f.filename,
    )


@router.delete("/{file_id}")
def delete_file(
    file_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a file. Only the uploader can delete."""
    f = db.query(AssetFile).filter(AssetFile.id == file_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    if f.uploader_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the uploader can delete this file")
    try:
        Path(f.storage_path).unlink(missing_ok=True)
    except Exception:
        pass
    db.delete(f)
    db.commit()
    return {"deleted": file_id}


@router.get("/storage/stats")
def storage_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Storage usage summary for the network and your account."""
    net_used = get_storage_stats(db)
    user_used = get_user_storage(db, current_user.id)
    max_file = get_config(db, "max_file_bytes", default_int=52428800)
    max_user = get_config(db, "max_user_bytes", default_int=524288000)
    max_net = get_config(db, "max_network_bytes", default_int=10737418240)
    allowed_raw = get_config(db, "allowed_types", default_text="pdf,md,txt,json,csv,png,jpg,jpeg,gif,mp3,wav,zip")

    return {
        "network": {
            "used": fmt_bytes(net_used),
            "limit": fmt_bytes(max_net),
            "pct": round(net_used / max_net * 100, 2) if max_net else 0,
        },
        "your_account": {
            "used": fmt_bytes(user_used),
            "limit": fmt_bytes(max_user),
            "pct": round(user_used / max_user * 100, 2) if max_user else 0,
        },
        "per_file_limit": fmt_bytes(max_file),
        "allowed_types": (allowed_raw or "").split(","),
        "governance_note": "Limits adjustable by governance vote. POST /governance/storage-config (founders only until vote).",
    }


@router.post("/storage/config")
def update_storage_config(
    key: str,
    value_int: int = None,
    value_text: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Founders only — update a storage config value. Intended to be called after governance vote."""
    FOUNDER_HANDLES = {"sean", "ava"}
    if current_user.handle not in FOUNDER_HANDLES:
        raise HTTPException(status_code=403, detail="Founders only (until governance vote passes)")

    VALID_KEYS = {"max_file_bytes", "max_user_bytes", "max_network_bytes", "allowed_types"}
    if key not in VALID_KEYS:
        raise HTTPException(status_code=422, detail=f"Valid keys: {', '.join(VALID_KEYS)}")

    row = db.query(StorageConfig).filter(StorageConfig.key == key).first()
    if row:
        row.value_int = value_int
        row.value_text = value_text
        from datetime import datetime, timezone
        row.updated_at = datetime.now(timezone.utc)
    else:
        row = StorageConfig(key=key, value_int=value_int, value_text=value_text)
        db.add(row)
    db.commit()
    return {"updated": key, "value_int": value_int, "value_text": value_text}
