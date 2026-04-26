"""
Agora — federation router
Node-to-node sync for distributed Agora network.
Node 1 (founder) is the authoritative source.
Other nodes register here and receive state snapshots + incremental updates.
"""

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from db import get_db, User, Asset, Rating, TokenEvent, BankLedger, StorageConfig

router = APIRouter(prefix="/federation", tags=["federation"])

# Node registry — in-memory for now, persisted to JSON
NODE_REGISTRY_FILE = Path(__file__).parent.parent / "node-registry.json"

def load_registry():
    if NODE_REGISTRY_FILE.exists():
        return json.loads(NODE_REGISTRY_FILE.read_text())
    return {"nodes": {}}

def save_registry(reg):
    NODE_REGISTRY_FILE.write_text(json.dumps(reg, indent=2))

def get_node_secret():
    """Node auth secret — hash of codebase for initial trust."""
    # Nodes prove identity by knowing the codebase hash
    cfg_file = Path(__file__).parent.parent / "node-package" / "config" / "node-config.json"
    if cfg_file.exists():
        cfg = json.loads(cfg_file.read_text())
        return cfg.get("codebase_hash", "")
    return ""


class NodeRegister(BaseModel):
    node_id: str           # unique node identifier (e.g. "node_2")
    operator_handle: str   # handle of the human operator
    public_url: str        # where this node is reachable
    codebase_hash: str     # must match Node 1's hash


class NodeHeartbeat(BaseModel):
    node_id: str
    status: str = "online"
    users: int = 0
    assets: int = 0


@router.post("/register")
def register_node(payload: NodeRegister, db: Session = Depends(get_db)):
    """Register a new node with Node 1. Validates codebase hash."""
    expected_hash = get_node_secret()
    if expected_hash and payload.codebase_hash != expected_hash:
        raise HTTPException(status_code=403,
            detail="Codebase hash mismatch. Ensure you're running unmodified Agora code.")

    reg = load_registry()
    if payload.node_id in reg["nodes"]:
        # Update existing
        reg["nodes"][payload.node_id]["public_url"] = payload.public_url
        reg["nodes"][payload.node_id]["last_seen"] = datetime.now(timezone.utc).isoformat()
    else:
        reg["nodes"][payload.node_id] = {
            "node_id": payload.node_id,
            "operator_handle": payload.operator_handle,
            "public_url": payload.public_url,
            "codebase_hash": payload.codebase_hash,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "status": "online",
        }
    save_registry(reg)

    return {
        "accepted": True,
        "node_id": payload.node_id,
        "network_size": len(reg["nodes"]) + 1,  # +1 for Node 1
        "message": f"Welcome to the Agora network. You are node #{len(reg['nodes'])}.",
        "sync_endpoint": "/federation/snapshot",
        "heartbeat_interval_seconds": 300,
    }


@router.post("/heartbeat")
def node_heartbeat(payload: NodeHeartbeat):
    """Nodes ping this every 5 minutes to stay marked online."""
    reg = load_registry()
    if payload.node_id in reg["nodes"]:
        reg["nodes"][payload.node_id]["last_seen"] = datetime.now(timezone.utc).isoformat()
        reg["nodes"][payload.node_id]["status"] = payload.status
        reg["nodes"][payload.node_id]["reported_users"] = payload.users
        reg["nodes"][payload.node_id]["reported_assets"] = payload.assets
        save_registry(reg)
    return {"ok": True}


@router.get("/nodes")
def list_nodes():
    """Public list of all registered nodes."""
    reg = load_registry()
    nodes = list(reg.get("nodes", {}).values())
    # Add Node 1
    nodes.insert(0, {
        "node_id": "node_1",
        "operator_handle": "sean",
        "public_url": "http://68.39.46.12:8001",
        "status": "online",
        "is_founder": True,
    })
    return {"total": len(nodes), "nodes": nodes}


@router.get("/snapshot")
def get_snapshot(since_id: int = 0, db: Session = Depends(get_db)):
    """
    Full or incremental state snapshot for node sync.
    New nodes call this with since_id=0 to get full state.
    Then poll with the last asset_id they received for incremental updates.
    """
    assets = db.query(Asset).filter(
        Asset.id > since_id,
        Asset.is_deleted == False
    ).order_by(Asset.id.asc()).limit(500).all()

    users = db.query(User).all()
    config = db.query(StorageConfig).all()

    return {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "since_id": since_id,
        "users": [
            {
                "id": u.id, "handle": u.handle, "display_name": u.display_name,
                "agent_type": u.agent_type, "token_balance": u.token_balance,
                "total_score": u.total_score, "submission_score": u.submission_score,
                "rater_score": u.rater_score, "trade_score": u.trade_score,
            }
            for u in users
        ],
        "assets": [
            {
                "id": a.id, "title": a.title, "description": a.description,
                "content": a.content, "asset_type": a.asset_type,
                "submitter_id": a.submitter_id, "avg_rating": a.avg_rating,
                "rating_count": a.rating_count, "tokens_minted": a.tokens_minted,
                "is_genesis": a.is_genesis, "parent_id": a.parent_id,
            }
            for a in assets
        ],
        "config": {c.key: (c.value_int or c.value_text) for c in config},
        "has_more": len(assets) == 500,
        "last_asset_id": assets[-1].id if assets else since_id,
    }


@router.get("/status")
def federation_status(db: Session = Depends(get_db)):
    """Network-wide status across all nodes."""
    reg = load_registry()
    registered_nodes = list(reg.get("nodes", {}).values())

    # Node 1 local stats
    users = db.query(User).count()
    assets = db.query(Asset).filter(Asset.is_deleted == False).count()

    return {
        "node_1": {
            "node_id": "node_1",
            "operator": "sean",
            "status": "online",
            "users": users,
            "assets": assets,
        },
        "registered_nodes": len(registered_nodes),
        "total_nodes": len(registered_nodes) + 1,
        "network_nodes": registered_nodes,
    }
