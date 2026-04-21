"""
Agora — 1000-user stress test
Uses bulk rating endpoint — deferred score recalc
"""

import requests
import random
import time
import statistics
import sys

BASE = "http://localhost:8001"
random.seed(42)

def post(url, data=None, params=None):
    return requests.post(BASE + url, json=data, params=params, timeout=300)

def get(url):
    return requests.get(BASE + url, timeout=60).json()

def section(title):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}")

# ---------------------------------------------------------------------------
# 1. Register 998 agents (sean + ava already seeded)
# ---------------------------------------------------------------------------
section("1. REGISTERING 998 AGENTS")
t0 = time.time()
handles = []
BATCH = 50
agent_list = [{"handle": f"a{i:04d}", "display_name": f"Agent {i:04d}", "agent_type": "agent"}
              for i in range(1, 999)]

ok = 0
for agent in agent_list:
    r = post("/users/", agent)
    if r.status_code == 201:
        ok += 1
        handles.append(agent["handle"])
    elif r.status_code == 409:
        handles.append(agent["handle"])  # already exists

all_handles = ["sean", "ava"] + handles
print(f"  Registered: {ok} | Total handles: {len(all_handles)} | Time: {time.time()-t0:.1f}s")

# ---------------------------------------------------------------------------
# 2. Submit 100 assets across random submitters
# ---------------------------------------------------------------------------
section("2. SUBMITTING 100 ASSETS")
t0 = time.time()

templates = [
    ("Consensus Protocol",   "Proposed consensus mechanism, iteration {}.",      "concept"),
    ("Data Schema Spec",     "Standardized schema for data interchange, v{}.",   "data"),
    ("Token Flow Model",     "Token velocity and distribution model, rev {}.",   "concept"),
    ("Reputation Engine",    "Multi-dimensional reputation system, draft {}.",   "code"),
    ("Asset Discovery",      "Feed of new assets ranked by quality, v{}.",       "code"),
    ("Governance Template",  "Reusable proposal template, iter {}.",             "concept"),
    ("Trade Escrow",         "Escrow logic for marketplace trades, rev {}.",     "code"),
    ("Pruning Policy",       "Community-driven pruning threshold proposal {}.",  "concept"),
    ("Agent Identity",       "Standardized agent identity declaration, v{}.",    "data"),
    ("Health Metrics",       "Network health measurement system, iter {}.",      "data"),
]

submitted_assets = []
submitters = random.sample(all_handles[2:], 100)

for idx, submitter in enumerate(submitters):
    t = templates[idx % len(templates)]
    roll = random.random()
    quality = (random.uniform(7.5, 9.5) if roll > 0.80 else
               random.uniform(4.5, 7.5) if roll > 0.30 else
               random.uniform(2.0, 4.5) if roll > 0.10 else
               random.uniform(1.0, 2.0))
    content = f"{t[1].format(idx+1)} By {submitter}. Seed={idx*7919+1337}."
    r = post("/assets/", {
        "title": f"{t[0]} v{idx+1}",
        "description": t[1].format(idx+1),
        "content": content,
        "asset_type": t[2],
        "submitter_handle": submitter,
    })
    if r.status_code == 201:
        submitted_assets.append((r.json()["id"], submitter, quality))

print(f"  Assets submitted: {len(submitted_assets)} | Time: {time.time()-t0:.1f}s")

# ---------------------------------------------------------------------------
# 3. Build rating payload — 40% participation, bulk submit
# ---------------------------------------------------------------------------
section("3. BUILDING RATING PAYLOAD (40% participation)")

# Get user id map
users_data = get("/users/")
uid_map = {u["handle"]: u["id"] for u in users_data}
# Get asset submitter map
asset_data = get("/assets/")
submitter_uid_map = {a["id"]: a["submitter_id"] for a in asset_data}

all_ratings = []
for asset_id, submitter, quality_base in submitted_assets:
    submitter_uid = uid_map.get(submitter)
    eligible = [uid for h, uid in uid_map.items() if uid != submitter_uid]
    raters = random.sample(eligible, int(len(eligible) * 0.40))
    for uid in raters:
        score = round(min(10.0, max(1.0, quality_base + random.uniform(-2.0, 2.0))), 1)
        all_ratings.append({"user_id": uid, "asset_id": asset_id, "score": score})

print(f"  Total ratings to submit: {len(all_ratings):,}")

# ---------------------------------------------------------------------------
# 4. Submit in batches of 2000
# ---------------------------------------------------------------------------
section("4. BULK RATING SUBMISSION")
t0 = time.time()
CHUNK = 2000
total_submitted = 0
total_skipped = 0

for i in range(0, len(all_ratings), CHUNK):
    chunk = all_ratings[i:i+CHUNK]
    r = post("/sim/bulk-rate", {"ratings": chunk})
    if r.status_code == 200:
        result = r.json()
        total_submitted += result["submitted"]
        total_skipped += result["skipped"]
        elapsed = time.time() - t0
        print(f"  Chunk {i//CHUNK+1}: {result['submitted']} submitted | {elapsed:.1f}s elapsed")
    else:
        print(f"  CHUNK FAILED: {r.status_code} {r.text[:100]}")

total_time = time.time() - t0
print(f"\n  Total submitted: {total_submitted:,} | Skipped: {total_skipped} | Time: {total_time:.1f}s")
print(f"  Rate: {total_submitted/total_time:.0f} ratings/sec")

# ---------------------------------------------------------------------------
# 5. Marketplace — 30 listings, 25 trades
# ---------------------------------------------------------------------------
section("4b. PRUNE TEST — submit a garbage asset, rate it into the ground")
prune_submitter = handles[0]
pr = post("/assets/", {
    "title": "PRUNE_TEST — Garbage Asset",
    "description": "This asset should be pruned.",
    "content": "Completely worthless content. No value whatsoever. Unique block #999999.",
    "asset_type": "concept",
    "submitter_handle": prune_submitter,
})
if pr.status_code == 201:
    prune_id = pr.json()["id"]
    print(f"  Submitted garbage asset id={prune_id}")
    # Rate it 1.0 by 25% of users (250 users — well above 20% threshold)
    prune_raters = [uid for h, uid in uid_map.items() if h != prune_submitter][:250]
    prune_ratings = [{"user_id": uid, "asset_id": prune_id, "score": 1.0} for uid in prune_raters]
    pr2 = post("/sim/bulk-rate", {"ratings": prune_ratings})
    print(f"  Rated by 250 users with score 1.0: {pr2.json()}")
    # Check if pruned
    check = requests.get(f"{BASE}/assets/", timeout=30).json()
    garbage = next((a for a in check if a["id"] == prune_id), None)
    if garbage:
        status_str = "🗑️  PRUNED ✅" if garbage["is_deleted"] else f"❌ NOT PRUNED (avg={garbage['avg_rating']:.2f}, ratings={garbage['rating_count']})"
        print(f"  Result: {status_str}")
else:
    print(f"  FAIL submit prune test asset: {pr.status_code}")

section("5. MARKETPLACE")
users_data = get("/users/")
balance_map = {u["handle"]: u["token_balance"] for u in users_data}

listings = []
for asset_id, submitter, _ in random.sample(submitted_assets, 30):
    bal = balance_map.get(submitter, 0)
    if bal > 0.5:
        price = round(random.uniform(0.5, min(3.0, bal * 0.4)), 2)
        r = post("/marketplace/listings", {"asset_id": asset_id, "seller_handle": submitter, "price": price})
        if r.status_code == 201:
            listings.append((r.json()["id"], submitter, price))

print(f"  Listings created: {len(listings)}")
trades_done = 0
for listing_id, seller, price in listings[:25]:
    buyers = [h for h, bal in balance_map.items() if bal >= price and h != seller]
    if not buyers:
        continue
    buyer = random.choice(buyers)
    r = post(f"/marketplace/listings/{listing_id}/buy", params={"buyer_handle": buyer})
    if r.status_code == 200:
        trades_done += 1
        balance_map[buyer] -= price
        balance_map[seller] += price * 0.99
print(f"  Trades completed: {trades_done}")

# ---------------------------------------------------------------------------
# 6. Diagnostic report
# ---------------------------------------------------------------------------
section("6. DIAGNOSTIC REPORT")

all_assets_data = get("/assets/")
all_users_data = get("/users/")
status = get("/status")
bank = get("/bank/balance")

live = [a for a in all_assets_data if not a["is_deleted"]]
pruned = [a for a in all_assets_data if a["is_deleted"]]
rated = [a for a in live if a["rating_count"] > 0]

avg_ratings   = [a["avg_rating"]    for a in rated]
token_minted  = [a["tokens_minted"] for a in rated]
token_bals    = [u["token_balance"]  for u in all_users_data]
total_scores  = [u["total_score"]    for u in all_users_data]
eligible      = [u for u in all_users_data if u["total_score"] >= 20.0]
circ          = sum(token_bals)
bank_bal      = bank["balance"]

print(f"\n📊 NETWORK")
print(f"  Users:             {status['users']:,}")
print(f"  Live assets:       {status['assets']:,}")
print(f"  Pruned assets:     {len(pruned)}")
print(f"  Trades:            {status['trades']}")
print(f"  Bank balance:      {bank_bal:,.4f} tokens")

print(f"\n📈 ASSET QUALITY")
print(f"  Mean avg rating:   {statistics.mean(avg_ratings):.3f}")
print(f"  Median:            {statistics.median(avg_ratings):.3f}")
print(f"  Std dev:           {statistics.stdev(avg_ratings):.3f}")
print(f"  Min / Max:         {min(avg_ratings):.3f} / {max(avg_ratings):.3f}")
print(f"  Assets avg < 2.0:  {sum(1 for r in avg_ratings if r < 2.0)}  ← health trigger")
print(f"  Assets avg > 8.0:  {sum(1 for r in avg_ratings if r > 8.0)}")

print(f"\n💰 TOKEN ECONOMY")
total_pool = sum(token_minted) + bank_bal
submitter_pct = sum(token_minted)/total_pool*100 if total_pool else 0
bank_pct = bank_bal/total_pool*100 if total_pool else 0
sorted_b = sorted(token_bals)
n = len(sorted_b)
gini = (2*sum((i+1)*v for i,v in enumerate(sorted_b))/(n*sum(sorted_b))-(n+1)/n) if sum(sorted_b) > 0 else 0
print(f"  Total pool value:  {total_pool:,.4f}")
print(f"  → Submitters:      {sum(token_minted):,.4f}  ({submitter_pct:.1f}%)")
print(f"  → Bank:            {bank_bal:,.4f}  ({bank_pct:.1f}%)")
print(f"  In circulation:    {circ:,.4f}")
print(f"  Users with tokens: {sum(1 for b in token_bals if b > 0):,} / {len(all_users_data):,}")
print(f"  Max balance:       {max(token_bals):.4f}")
print(f"  Gini coefficient:  {gini:.3f}")

print(f"\n🏆 SCORES")
print(f"  Mean total score:  {statistics.mean(total_scores):.3f}")
print(f"  Max total score:   {max(total_scores):.3f}")
print(f"  Eligible voters:   {len(eligible)} / {len(all_users_data)} ({len(eligible)/len(all_users_data)*100:.1f}%)")

print(f"\n🔪 PRUNING")
print(f"  Pruned:            {len(pruned)}")
near = [a for a in live if 0 < a["avg_rating"] <= 1.0]
for a in near:
    pct = a['rating_count']/status['users']*100
    print(f"  ⚠️  '{a['title'][:45]}' avg={a['avg_rating']:.2f} rated_by={pct:.0f}% — SHOULD BE PRUNED")
low = [a for a in live if 1.0 < a["avg_rating"] <= 2.0]
for a in low:
    pct = a['rating_count']/status['users']*100
    print(f"  📉 '{a['title'][:45]}' avg={a['avg_rating']:.2f} rated_by={pct:.0f}% (low but above prune threshold)")

print(f"\n⏱️  PERFORMANCE")
print(f"  Rating throughput: {total_submitted/total_time:.0f} ratings/sec")
print(f"  Total ratings:     {total_submitted:,}")
print(f"  Total time:        {total_time:.1f}s")

print(f"\n📋 TOP 10 ASSETS")
top_a = sorted(rated, key=lambda x: x["avg_rating"], reverse=True)[:10]
print(f"  {'Title':<40} {'Avg':>5} {'Ratings':>8} {'Minted':>10}")
print(f"  {'-'*67}")
for a in top_a:
    print(f"  {a['title'][:40]:<40} {a['avg_rating']:>5.2f} {a['rating_count']:>8} {a['tokens_minted']:>10.4f}")

print(f"\n📋 TOP 10 USERS")
top_u = sorted(all_users_data, key=lambda x: x["total_score"], reverse=True)[:10]
print(f"  {'Handle':<12} {'Total':>6} {'Sub':>6} {'Rate':>6} {'Trade':>6} {'Tokens':>10}")
print(f"  {'-'*55}")
for u in top_u:
    ev = " 🗳️" if u["total_score"] >= 20.0 else ""
    print(f"  {u['handle']:<12} {u['total_score']:>6.2f} {u['submission_score']:>6.2f} {u['rater_score']:>6.2f} {u['trade_score']:>6.2f} {u['token_balance']:>10.4f}{ev}")

print("\n✅ Done.\n")
