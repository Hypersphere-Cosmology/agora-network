"""
Agora — federation router
Node-to-node sync for distributed Agora network.
Node 1 (founder) is the authoritative source.
Other nodes register here and receive state snapshots + incremental updates.

v2: gossip peer discovery, merkle state root, consistent-hash sharding.
"""

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db, User, Asset, Rating, TokenEvent, BankLedger, StorageConfig, ProfilePost
from engine.sharding import assign_nodes, shard_coverage

router = APIRouter(prefix="/federation", tags=["federation"])

# ---------------------------------------------------------------------------
# Node 1 identity (hardcoded — this IS node 1)
# ---------------------------------------------------------------------------
NODE_1_INFO = {
    "node_id": "node_1",
    "operator_handle": "sean",
    "public_url": "http://68.39.46.12:8001",
    "status": "online",
    "is_founder": True,
}

# Node registry — persisted to JSON
NODE_REGISTRY_FILE = Path(__file__).parent.parent / "node-registry.json"


def load_registry() -> dict:
    if NODE_REGISTRY_FILE.exists():
        return json.loads(NODE_REGISTRY_FILE.read_text())
    return {"nodes": {}}


def save_registry(reg: dict):
    NODE_REGISTRY_FILE.write_text(json.dumps(reg, indent=2))


def get_all_peers() -> list:
    """Return full list of known peers including Node 1 itself."""
    reg = load_registry()
    peers = list(reg.get("nodes", {}).values())
    # Always include ourselves
    node1 = dict(NODE_1_INFO)
    node1["last_seen"] = datetime.now(timezone.utc).isoformat()
    node1["download_url"] = NODE_1_INFO["public_url"] + "/download/node-package"
    # Add download_url to all peers
    for p in peers:
        if "download_url" not in p:
            p["download_url"] = p.get("public_url", "") + "/download/node-package"
    return [node1] + peers


def get_node_secret() -> str:
    """Node auth secret — hash of codebase for initial trust."""
    cfg_file = Path(__file__).parent.parent / "node-package" / "config" / "node-config.json"
    if cfg_file.exists():
        cfg = json.loads(cfg_file.read_text())
        return cfg.get("codebase_hash", "")
    return ""


# ---------------------------------------------------------------------------
# Background gossip task
# ---------------------------------------------------------------------------

async def _gossip_loop():
    """Every 60 seconds, push our peer list to all known peers."""
    import httpx
    while True:
        await asyncio.sleep(60)
        try:
            reg = load_registry()
            peers = list(reg.get("nodes", {}).values())
            my_peers = get_all_peers()
            payload = {"peers": my_peers}

            async with httpx.AsyncClient(timeout=10.0) as client:
                for peer in peers:
                    url = peer.get("public_url", "")
                    if not url:
                        continue
                    try:
                        await client.post(f"{url}/federation/gossip", json=payload)
                    except Exception:
                        pass
        except Exception:
            pass


async def merkle_heartbeat_loop():
    """Every 5 minutes, cross-verify merkle root with all known peers."""
    import httpx
    await asyncio.sleep(30)  # initial delay
    while True:
        await asyncio.sleep(300)
        reg = load_registry()
        for node_id, node in reg.get("nodes", {}).items():
            peer_url = node.get("public_url", "")
            if not peer_url:
                continue
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    # Get our own merkle
                    our = await client.get("http://localhost:8001/federation/merkle")
                    our_root = our.json().get("merkle_root", "")
                    # Verify against peer
                    resp = await client.post(f"{peer_url}/federation/verify",
                        json={"merkle_root": our_root})
                    result = resp.json()
                    if not result.get("match"):
                        print(f"[merkle] MISMATCH with {node_id}: our={our_root[:16]}... their={result.get('our_root','?')[:16]}...")
                    else:
                        print(f"[merkle] ✅ {node_id} in sync")
            except Exception:
                pass  # peer unreachable, skip silently


def start_gossip_task():
    """Schedule the gossip loop and merkle heartbeat in the current event loop."""
    loop = asyncio.get_event_loop()
    loop.create_task(_gossip_loop())
    loop.create_task(merkle_heartbeat_loop())


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class NodeRegister(BaseModel):
    node_id: str
    operator_handle: str
    public_url: str
    codebase_hash: str


class NodeHeartbeat(BaseModel):
    node_id: str
    status: str = "online"
    users: int = 0
    assets: int = 0


class GossipPayload(BaseModel):
    peers: list  # list of peer dicts


class VerifyRequest(BaseModel):
    merkle_root: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/register")
def register_node(payload: NodeRegister, db: Session = Depends(get_db)):
    """Register a new node. Returns full peer list so the new node knows all peers."""
    expected_hash = get_node_secret()
    if expected_hash and payload.codebase_hash != expected_hash:
        raise HTTPException(status_code=403,
            detail="Codebase hash mismatch. Ensure you're running unmodified Agora code.")

    reg = load_registry()
    now = datetime.now(timezone.utc).isoformat()
    if payload.node_id in reg["nodes"]:
        reg["nodes"][payload.node_id]["public_url"] = payload.public_url
        reg["nodes"][payload.node_id]["last_seen"] = now
    else:
        reg["nodes"][payload.node_id] = {
            "node_id": payload.node_id,
            "operator_handle": payload.operator_handle,
            "public_url": payload.public_url,
            "codebase_hash": payload.codebase_hash,
            "registered_at": now,
            "last_seen": now,
            "status": "online",
        }
    save_registry(reg)

    # Return full peer list so the new node immediately knows everyone
    all_peers = get_all_peers()

    return {
        "accepted": True,
        "node_id": payload.node_id,
        "network_size": len(reg["nodes"]) + 1,
        "message": f"Welcome to the Agora network. You are node #{len(reg['nodes'])}.",
        "sync_endpoint": "/federation/snapshot",
        "heartbeat_interval_seconds": 300,
        "peers": all_peers,
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


@router.get("/peers")
def get_peers():
    """Return full known peer list (all nodes including Node 1)."""
    return {"peers": get_all_peers()}


@router.post("/gossip")
def receive_gossip(payload: GossipPayload):
    """
    Accept a list of peers from another node. Merge any unknown peers into local registry.
    This is how nodes learn about each other without a central coordinator.
    """
    reg = load_registry()
    now = datetime.now(timezone.utc).isoformat()
    changed = False

    for peer in payload.peers:
        nid = peer.get("node_id")
        if not nid or nid == "node_1":
            continue  # Skip ourselves and invalid entries
        if nid not in reg["nodes"]:
            # New peer — add it
            reg["nodes"][nid] = {
                "node_id": nid,
                "operator_handle": peer.get("operator_handle", ""),
                "public_url": peer.get("public_url", ""),
                "codebase_hash": peer.get("codebase_hash", ""),
                "registered_at": peer.get("registered_at", now),
                "last_seen": now,
                "status": peer.get("status", "online"),
            }
            changed = True

    if changed:
        save_registry(reg)

    return {"ok": True, "known_peers": len(reg["nodes"]) + 1}


@router.get("/nodes")
def list_nodes():
    """Public list of all registered nodes (gossip-merged), each with download_url."""
    all_nodes = get_all_peers()
    return {"total": len(all_nodes), "nodes": all_nodes}


@router.get("/snapshot")
def get_snapshot(since_id: int = 0, db: Session = Depends(get_db)):
    """Full or incremental state snapshot for node sync."""
    assets = db.query(Asset).filter(
        Asset.id > since_id,
        Asset.is_deleted == False
    ).order_by(Asset.id.asc()).limit(500).all()

    users = db.query(User).all()
    config = db.query(StorageConfig).all()
    posts = db.query(ProfilePost).filter(ProfilePost.is_deleted == False).order_by(ProfilePost.id.asc()).all()

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
                "content_hash": a.content_hash,
            }
            for a in assets
        ],
        "profile_posts": [
            {
                "id": p.id, "target_handle": p.target_handle, "author_id": p.author_id,
                "content": p.content, "posted_at": p.posted_at.isoformat() if p.posted_at else None,
                "parent_id": p.parent_id,
            }
            for p in posts
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


# ---------------------------------------------------------------------------
# Merkle state root
# ---------------------------------------------------------------------------

def _compute_merkle_root(db: Session):
    """Compute deterministic merkle root of current network state."""
    # Gather canonical strings
    leaves = []

    assets = db.query(Asset).filter(Asset.is_deleted == False).order_by(Asset.id.asc()).all()
    for a in assets:
        leaves.append(f"{a.id}:{a.content_hash}:{a.avg_rating}:{a.rating_count}")

    users = db.query(User).order_by(User.id.asc()).all()
    for u in users:
        leaves.append(f"{u.id}:{u.handle}:{u.token_balance:.4f}:{u.total_score:.4f}")

    configs = db.query(StorageConfig).order_by(StorageConfig.key.asc()).all()
    for c in configs:
        val = c.value_int if c.value_int is not None else (c.value_text or "")
        leaves.append(f"{c.key}:{val}")

    # Sort all strings for determinism
    leaves.sort()

    if not leaves:
        return hashlib.sha256(b"empty").hexdigest(), 0, 0

    # Hash each leaf
    hashes = [hashlib.sha256(l.encode()).digest() for l in leaves]

    # Build merkle tree
    while len(hashes) > 1:
        next_level = []
        for i in range(0, len(hashes), 2):
            left = hashes[i]
            right = hashes[i + 1] if i + 1 < len(hashes) else left
            combined = hashlib.sha256(left + right).digest()
            next_level.append(combined)
        hashes = next_level

    root = hashes[0].hex()
    return root, len(assets), len(users)


@router.get("/merkle")
def get_merkle(db: Session = Depends(get_db)):
    """Compute deterministic merkle root of current network state."""
    root, asset_count, user_count = _compute_merkle_root(db)
    return {
        "merkle_root": root,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "asset_count": asset_count,
        "user_count": user_count,
    }


@router.post("/verify")
def verify_merkle(payload: VerifyRequest, db: Session = Depends(get_db)):
    """Compare our merkle root to a remote node's root."""
    our_root, _, _ = _compute_merkle_root(db)
    return {
        "match": our_root == payload.merkle_root,
        "our_root": our_root,
        "their_root": payload.merkle_root,
    }


# ---------------------------------------------------------------------------
# Shard map endpoints
# ---------------------------------------------------------------------------

def _get_all_node_ids() -> List[str]:
    """Get all known node IDs (node_1 + registry)."""
    reg = load_registry()
    ids = ["node_1"] + list(reg.get("nodes", {}).keys())
    return ids


@router.get("/shard-map")
def get_shard_map(db: Session = Depends(get_db)):
    """
    Show consistent-hash sharding assignment for all assets.
    Returns which nodes are responsible for each asset.
    """
    assets = db.query(Asset).filter(Asset.is_deleted == False).all()
    all_node_ids = _get_all_node_ids()
    n = len(all_node_ids)
    k = min(3, n)

    assignments = {}
    for a in assets:
        if a.content_hash:
            responsible = assign_nodes(a.content_hash, all_node_ids)
            assignments[a.content_hash] = {
                "asset_id": a.id,
                "title": a.title,
                "responsible_nodes": responsible,
            }

    coverage = shard_coverage("node_1", all_node_ids)

    return {
        "node_count": n,
        "replication_factor": k,
        "shard_coverage_pct": round(coverage * 100, 2),
        "assignments": assignments,
    }


@router.get("/any")
async def any_redirect(ref: str = None):
    """Redirect to a random live peer's UI (or join page if ref provided). Falls back to self if no peers live."""
    import httpx
    import random
    from fastapi.responses import RedirectResponse

    peers = get_all_peers()
    self_url = NODE_1_INFO["public_url"]

    # Try each peer (excluding self) with 2s timeout
    live_peers = []
    async with httpx.AsyncClient(timeout=2.0) as client:
        for peer in peers:
            peer_url = peer.get("public_url", "")
            if not peer_url or peer_url == self_url:
                continue
            try:
                r = await client.get(f"{peer_url}/health")
                if r.status_code == 200:
                    live_peers.append(peer_url)
            except Exception:
                pass

    # Always serve from self — peers join the rotation only when on independent IPs/ISPs
    # (routing to a peer on the same ISP/router causes firewall issues for external users)
    live_url = ""  # relative = self

    if ref:
        target = f"{live_url}/join?ref={ref}" if live_url else f"/join?ref={ref}"
    else:
        target = f"{live_url}/ui" if live_url else "/ui"

    return RedirectResponse(url=target, status_code=302)


@router.get("/version")
def get_version():
    """Returns current codebase hash for lightweight update checking."""
    cfg_file = Path(__file__).parent.parent / "node-package" / "config" / "node-config.json"
    if cfg_file.exists():
        cfg = json.loads(cfg_file.read_text())
        return {
            "codebase_hash": cfg.get("codebase_hash", ""),
            "agora_version": cfg.get("agora_version", "0.1.0"),
            "ruleset": cfg.get("ruleset", "v18")
        }
    return {"codebase_hash": "", "agora_version": "0.1.0", "ruleset": "v18"}


@router.get("/my-shard")
def my_shard(db: Session = Depends(get_db)):
    """Return only assets that node_1 is responsible for (based on consistent hashing)."""
    assets = db.query(Asset).filter(Asset.is_deleted == False).all()
    all_node_ids = _get_all_node_ids()

    my_assets = []
    for a in assets:
        if a.content_hash:
            responsible = assign_nodes(a.content_hash, all_node_ids)
            if "node_1" in responsible:
                my_assets.append({
                    "asset_id": a.id,
                    "title": a.title,
                    "content_hash": a.content_hash,
                    "responsible_nodes": responsible,
                })

    return {
        "node_id": "node_1",
        "asset_count": len(my_assets),
        "assets": my_assets,
    }
