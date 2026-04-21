"""
Agora — 10-user network simulation
Tests: join, submit, rate, trade, governance
"""

import requests
import json

BASE = "http://localhost:8001"

def p(label, r):
    status = "✅" if r.status_code < 400 else "❌"
    print(f"{status} [{r.status_code}] {label}")
    if r.status_code >= 400:
        print(f"   ERROR: {r.json()}")
    return r.json() if r.status_code < 400 else None


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# 1. Register 8 additional agents (sean + ava already exist)
# ---------------------------------------------------------------------------
section("1. REGISTER AGENTS")

agents = [
    {"handle": "forge-7",    "display_name": "Forge-7",        "agent_type": "agent"},
    {"handle": "null-echo",  "display_name": "Null Echo",      "agent_type": "agent"},
    {"handle": "meridian",   "display_name": "Meridian",       "agent_type": "agent"},
    {"handle": "hex-bloom",  "display_name": "Hex Bloom",      "agent_type": "agent"},
    {"handle": "caelum",     "display_name": "Caelum",         "agent_type": "agent"},
    {"handle": "vex",        "display_name": "Vex",            "agent_type": "agent"},
    {"handle": "oracle-9",   "display_name": "Oracle-9",       "agent_type": "agent"},
    {"handle": "drift",      "display_name": "Drift",          "agent_type": "agent"},
]

for a in agents:
    p(f"Register {a['handle']}", requests.post(f"{BASE}/users/", json=a))

# Show all users + scores
section("USER ROSTER")
users = requests.get(f"{BASE}/users/").json()
print(f"{'Handle':<15} {'Score':>6} {'Sub':>6} {'Rate':>6} {'Trade':>6} {'Tokens':>10}")
print("-" * 55)
for u in sorted(users, key=lambda x: x["handle"]):
    print(f"{u['handle']:<15} {u['total_score']:>6.2f} {u['submission_score']:>6.2f} {u['rater_score']:>6.2f} {u['trade_score']:>6.2f} {u['token_balance']:>10.4f}")


# ---------------------------------------------------------------------------
# 2. Submit assets (each agent submits one)
# ---------------------------------------------------------------------------
section("2. SUBMIT ASSETS")

asset_defs = [
    {
        "title": "Reputation Decay Algorithm",
        "description": "Slowly reduce scores of inactive users to prevent stale rankings.",
        "content": "A proposal for a decay function applied to rater_raw scores when a user has not submitted a rating in 30 days. Decay rate: 2% per week. Floors at 0.",
        "asset_type": "concept",
        "submitter_handle": "forge-7",
    },
    {
        "title": "Anti-Sybil Token Stake",
        "description": "Require a minimum token stake to submit assets after the first.",
        "content": "To prevent Sybil attacks via asset flooding: users must hold at least 1 token to submit their second asset, 2 tokens for their third, etc. First submission is always free.",
        "asset_type": "concept",
        "submitter_handle": "null-echo",
    },
    {
        "title": "Agent Metadata Standard",
        "description": "A standard schema for agent self-description on join.",
        "content": "Proposed JSON schema for agent registration: {handle, display_name, agent_type, capabilities: [], version, operator_url}. Capabilities list is open-ended and self-declared.",
        "asset_type": "data",
        "submitter_handle": "meridian",
    },
    {
        "title": "Marketplace Escrow Module",
        "description": "Hold tokens in escrow during a trade to prevent double-spend.",
        "content": "A code module that locks buyer tokens at listing purchase, releases to seller only on confirmation. Adds a 24h dispute window. Bank covers disputes from fee pool.",
        "asset_type": "code",
        "submitter_handle": "hex-bloom",
    },
    {
        "title": "Asset Tagging System",
        "description": "Let submitters tag assets with categories for discoverability.",
        "content": "Tags are free-form strings, max 5 per asset, max 32 chars each. Community can vote to canonicalize a tag taxonomy after 100 assets exist.",
        "asset_type": "concept",
        "submitter_handle": "caelum",
    },
    {
        "title": "GRU4Rec Trade Predictor",
        "description": "Sequential recommendation model for predicting high-value trades.",
        "content": "Applies GRU4Rec architecture to trade history sequences. Trained locally, updated daily. Output: ranked list of assets likely to appreciate in the next 24h.",
        "asset_type": "code",
        "submitter_handle": "vex",
    },
    {
        "title": "Governance Cooldown Rule",
        "description": "Enforce a 48h cooldown between proposals from the same user.",
        "content": "Prevents governance spam by enforcing a 48-hour cooldown between proposals submitted by the same user. Emergency proposals (flagged by 3+ eligible voters) bypass the cooldown.",
        "asset_type": "concept",
        "submitter_handle": "oracle-9",
    },
    {
        "title": "Network Health Dashboard",
        "description": "Live monitoring of aggregate asset ratings and activity.",
        "content": "A read-only dashboard endpoint at /health/dashboard exposing: avg asset rating, active users (rated in last 7d), trade volume, bank balance, top-rated assets, lowest-rated assets approaching prune threshold.",
        "asset_type": "code",
        "submitter_handle": "drift",
    },
]

asset_ids = {}
for a in asset_defs:
    result = p(f"Submit '{a['title'][:35]}'", requests.post(f"{BASE}/assets/", json=a))
    if result:
        asset_ids[a["submitter_handle"]] = result["id"]

# Genesis is asset 1, our submitted assets start at 2
all_assets = requests.get(f"{BASE}/assets/").json()
print(f"\nTotal assets: {len(all_assets)}")


# ---------------------------------------------------------------------------
# 3. Rate assets — everyone rates everything they didn't submit
# ---------------------------------------------------------------------------
section("3. RATING ROUND")

import random
random.seed(42)

all_handles = ["sean", "ava"] + [a["handle"] for a in agents]

# Scores designed so some assets shine, one tanks toward pruning
quality_map = {
    "forge-7":   8.5,
    "null-echo": 7.2,
    "meridian":  9.1,
    "hex-bloom": 6.8,
    "caelum":    7.9,
    "vex":       9.4,
    "oracle-9":  5.1,
    "drift":     8.8,
}

rating_count = 0
for asset in all_assets:
    if asset["is_genesis"]:
        continue
    submitter_handle = next(
        (h for h, aid in asset_ids.items() if aid == asset["id"]), None
    )
    base_quality = quality_map.get(submitter_handle, 7.0)

    for rater in all_handles:
        if rater == submitter_handle:
            continue
        # Add noise ±1.5, clamp to 1-10
        score = round(min(10, max(1, base_quality + random.uniform(-1.5, 1.5))), 1)
        r = requests.post(f"{BASE}/assets/{asset['id']}/rate", json={
            "rater_handle": rater,
            "score": score,
        })
        if r.status_code < 400:
            rating_count += 1

print(f"Total ratings submitted: {rating_count}")


# ---------------------------------------------------------------------------
# 4. Show asset standings after rating
# ---------------------------------------------------------------------------
section("4. ASSET STANDINGS")

all_assets = requests.get(f"{BASE}/assets/").json()
sorted_assets = sorted(
    [a for a in all_assets if not a["is_genesis"] and not a["is_deleted"]],
    key=lambda x: x["avg_rating"], reverse=True
)
print(f"{'#':<3} {'Title':<38} {'Avg':>5} {'Ratings':>8} {'Minted':>10}")
print("-" * 70)
for i, a in enumerate(sorted_assets, 1):
    print(f"{i:<3} {a['title'][:38]:<38} {a['avg_rating']:>5.2f} {a['rating_count']:>8} {a['tokens_minted']:>10.4f}")

deleted = [a for a in all_assets if a["is_deleted"]]
if deleted:
    print(f"\n🗑️  PRUNED: {len(deleted)} asset(s) auto-deleted")
    for a in deleted:
        print(f"   - {a['title']} (avg: {a['avg_rating']:.2f})")


# ---------------------------------------------------------------------------
# 5. User scores after ratings
# ---------------------------------------------------------------------------
section("5. USER LEADERBOARD")

users = requests.get(f"{BASE}/users/").json()
sorted_users = sorted(users, key=lambda x: x["total_score"], reverse=True)
print(f"{'Rank':<5} {'Handle':<15} {'Total':>6} {'Sub':>6} {'Rate':>6} {'Trade':>6} {'Tokens':>10}")
print("-" * 60)
for i, u in enumerate(sorted_users, 1):
    eligible = " 🗳️" if u["total_score"] >= 20 else ""
    print(f"{i:<5} {u['handle']:<15} {u['total_score']:>6.2f} {u['submission_score']:>6.2f} {u['rater_score']:>6.2f} {u['trade_score']:>6.2f} {u['token_balance']:>10.4f}{eligible}")

print("\n🗳️  = eligible to vote (score ≥ 20)")


# ---------------------------------------------------------------------------
# 6. Marketplace — a few trades
# ---------------------------------------------------------------------------
section("6. MARKETPLACE TRADES")

# Vex lists GRU4Rec for 5 tokens (they earned some from ratings)
vex_user = next(u for u in users if u["handle"] == "vex")
print(f"Vex token balance: {vex_user['token_balance']:.4f}")

# Give sean some tokens manually via direct check first
sean_user = next(u for u in users if u["handle"] == "sean")
print(f"Sean token balance: {sean_user['token_balance']:.4f}")

vex_asset_id = asset_ids.get("vex")
meridian_asset_id = asset_ids.get("meridian")

listings = []
if vex_asset_id:
    r = p("Vex lists GRU4Rec for 3 tokens", requests.post(f"{BASE}/marketplace/listings", json={
        "asset_id": vex_asset_id,
        "seller_handle": "vex",
        "price": 3.0,
    }))
    if r:
        listings.append(r)

if meridian_asset_id:
    r = p("Meridian lists Agent Metadata for 2 tokens", requests.post(f"{BASE}/marketplace/listings", json={
        "asset_id": meridian_asset_id,
        "seller_handle": "meridian",
        "price": 2.0,
    }))
    if r:
        listings.append(r)

# Show active listings
active = requests.get(f"{BASE}/marketplace/listings").json()
print(f"\nActive listings: {len(active)}")
for l in active:
    print(f"  Listing #{l['id']} — asset {l['asset_id']} @ {l['price']} tokens")

# Try buying — forge-7 buys if they have tokens
forge_user = next(u for u in users if u["handle"] == "forge-7")
print(f"\nForge-7 balance: {forge_user['token_balance']:.4f}")
if listings and forge_user["token_balance"] >= 3.0:
    p("Forge-7 buys from listing 1", requests.post(
        f"{BASE}/marketplace/listings/{listings[0]['id']}/buy",
        params={"buyer_handle": "forge-7"}
    ))
else:
    print("⚠️  Forge-7 has insufficient tokens for trade (expected in early network — minting is low)")


# ---------------------------------------------------------------------------
# 7. Final status
# ---------------------------------------------------------------------------
section("7. FINAL NETWORK STATUS")

status = requests.get(f"{BASE}/status").json()
bank = requests.get(f"{BASE}/bank/balance").json()
print(f"Users:         {status['users']}")
print(f"Assets:        {status['assets']}")
print(f"Trades:        {status['trades']}")
print(f"Bank balance:  {bank['balance']:.6f} tokens")

print("\n✅ Simulation complete.")
