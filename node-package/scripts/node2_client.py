#!/usr/bin/env python3
"""
Agora Node 2 Client
Registers with Node 1, syncs state, runs local Agora instance.
"""

import requests
import json
import time
import subprocess
import sys
from pathlib import Path

NODE1_URL = "https://backed-labels-server-sporting.trycloudflare.com"
CONFIG_FILE = Path(".env.json")


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def register_with_node1(node_id: str, operator: str, my_url: str, code_hash: str):
    print(f"Registering {node_id} with Node 1...")
    r = requests.post(f"{NODE1_URL}/federation/register", json={
        "node_id": node_id,
        "operator_handle": operator,
        "public_url": my_url,
        "codebase_hash": code_hash,
    }, timeout=15)
    if r.ok:
        data = r.json()
        print(f"✅ Registered! Network size: {data['network_size']} nodes")
        return data
    else:
        print(f"❌ Registration failed: {r.text}")
        return None


def sync_from_node1(since_id: int = 0):
    """Pull state snapshot from Node 1."""
    print(f"Syncing from Node 1 (since asset #{since_id})...")
    r = requests.get(f"{NODE1_URL}/federation/snapshot", params={"since_id": since_id}, timeout=30)
    if r.ok:
        data = r.json()
        print(f"  Users: {len(data['users'])} | Assets: {len(data['assets'])}")
        return data
    else:
        print(f"❌ Sync failed: {r.text}")
        return None


def heartbeat_loop(node_id: str, my_port: int = 8002):
    """Send heartbeat to Node 1 every 5 minutes."""
    while True:
        try:
            # Get local stats
            r = requests.get(f"http://localhost:{my_port}/status", timeout=5)
            if r.ok:
                stats = r.json()
                requests.post(f"{NODE1_URL}/federation/heartbeat", json={
                    "node_id": node_id,
                    "status": "online",
                    "users": stats.get("users", 0),
                    "assets": stats.get("assets", 0),
                }, timeout=10)
        except Exception as e:
            print(f"Heartbeat error: {e}")
        time.sleep(300)  # 5 minutes


def main():
    cfg = load_config()

    if not cfg.get("node_id"):
        print("=== Agora Node 2 Setup ===")
        node_id = input("Node ID (e.g. node_2): ").strip() or "node_2"
        operator = input("Your handle: ").strip()
        my_url = input("Your public URL (or press Enter for local only): ").strip() or "http://localhost:8002"
        code_hash = input("Codebase hash (from node-config.json): ").strip()

        result = register_with_node1(node_id, operator, my_url, code_hash)
        if not result:
            sys.exit(1)

        cfg = {"node_id": node_id, "operator": operator, "my_url": my_url, "last_sync_id": 0}
        save_config(cfg)

    # Sync state
    snapshot = sync_from_node1(cfg.get("last_sync_id", 0))
    if snapshot:
        cfg["last_sync_id"] = snapshot.get("last_asset_id", 0)
        save_config(cfg)
        print(f"✅ Sync complete. Last asset ID: {cfg['last_sync_id']}")

    print(f"\n✅ Node {cfg['node_id']} ready.")
    print(f"   Start Agora: python3 main.py --port 8002")
    print(f"   UI: http://localhost:8002/ui")
    print(f"   Node 1 network: {NODE1_URL}/federation/nodes")

    # Optional: start heartbeat in background
    print("\nStarting heartbeat loop (Ctrl+C to stop)...")
    heartbeat_loop(cfg["node_id"])


if __name__ == "__main__":
    main()
