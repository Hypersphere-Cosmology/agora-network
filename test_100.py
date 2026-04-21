"""
Agora — 100-user stress test with diagnostic report
"""

import requests
import random
import time
import statistics

BASE = "http://localhost:8001"
random.seed(99)

def post(url, data=None, params=None):
    r = requests.post(BASE + url, json=data, params=params)
    return r

def get(url, params=None):
    r = requests.get(BASE + url, params=params)
    return r.json()

# ---------------------------------------------------------------------------
# 1. Register 98 agents (sean + ava already exist)
# ---------------------------------------------------------------------------
print("\n=== REGISTERING 98 AGENTS ===")
agent_types = ["agent"] * 90 + ["human"] * 8
random.shuffle(agent_types)

handles = []
for i in range(1, 99):
    handle = f"agent-{i:03d}"
    r = post("/users/", {"handle": handle, "display_name": f"Agent {i:03d}", "agent_type": agent_types[i-1]})
    if r.status_code != 201:
        print(f"  FAIL register {handle}: {r.status_code}")
    handles.append(handle)

all_handles = ["sean", "ava"] + handles
print(f"  Registered {len(handles)} agents. Total users: {len(all_handles)}")

# ---------------------------------------------------------------------------
# 2. Submit 50 assets across random submitters
# ---------------------------------------------------------------------------
print("\n=== SUBMITTING 50 ASSETS ===")

asset_templates = [
    ("Consensus Protocol v{}", "A proposed consensus mechanism for agent networks, iteration {}.", "concept"),
    ("Data Schema Standard v{}", "Standardized schema for agent data interchange, revision {}.", "data"),
    ("Token Velocity Limiter v{}", "Prevents token velocity attacks by rate-limiting mints, version {}.", "code"),
    ("Reputation Bootstrap v{}", "Mechanism for bootstrapping reputation for new agents, draft {}.", "concept"),
    ("Asset Discovery Feed v{}", "Aggregated feed of new assets ranked by emerging quality signals, v{}.", "code"),
    ("Governance Template v{}", "Reusable proposal template for common governance decisions, iter {}.", "concept"),
    ("Trade Escrow Contract v{}", "Smart escrow logic for marketplace trades, revision {}.", "code"),
    ("Pruning Policy Proposal v{}", "Community-driven pruning threshold adjustment proposal, v{}.", "concept"),
    ("Agent Identity Spec v{}", "Standardized agent identity and capability declaration, version {}.", "data"),
    ("Network Health Metric v{}", "New metric for measuring network health beyond avg rating, iter {}.", "data"),
]

# Assign quality scores — distributed to create realistic spread
# 20% high (7.5-9.5), 50% mid (4.5-7.5), 20% low (2.0-4.5), 10% junk (1.0-2.0)
submitted_assets = []  # (asset_id, submitter, quality_base)

submitters = random.sample(all_handles[2:], 50)  # pick 50 of the 98 agents

for idx, submitter in enumerate(submitters):
    template = asset_templates[idx % len(asset_templates)]
    roll = random.random()
    if roll > 0.80:
        quality = random.uniform(7.5, 9.5)
    elif roll > 0.30:
        quality = random.uniform(4.5, 7.5)
    elif roll > 0.10:
        quality = random.uniform(2.0, 4.5)
    else:
        quality = random.uniform(1.0, 2.0)

    title = template[0].format(idx + 1)
    desc = template[1].format(idx + 1)
    content = f"{desc} Submitted by {submitter}. Quality tier: {quality:.2f}. Unique content block #{idx * 7919 + 1337}."

    r = post("/assets/", {
        "title": title,
        "description": desc,
        "content": content,
        "asset_type": template[2],
        "submitter_handle": submitter,
    })
    if r.status_code == 201:
        asset = r.json()
        submitted_assets.append((asset["id"], submitter, quality))
    else:
        print(f"  FAIL submit asset {idx}: {r.status_code} {r.text[:80]}")

print(f"  Submitted {len(submitted_assets)} assets")

# ---------------------------------------------------------------------------
# 3. Rating round — simulate ~40% participation per asset (realistic)
# ---------------------------------------------------------------------------
print("\n=== RATING ROUND (40% participation per asset) ===")

total_ratings = 0
failed_ratings = 0
t0 = time.time()

for asset_id, submitter, quality_base in submitted_assets:
    # Pick ~40% of non-submitter users to rate
    eligible = [h for h in all_handles if h != submitter]
    raters = random.sample(eligible, int(len(eligible) * 0.40))

    for rater in raters:
        score = round(min(10.0, max(1.0, quality_base + random.uniform(-2.0, 2.0))), 1)
        r = post(f"/assets/{asset_id}/rate", {"rater_handle": rater, "score": score})
        if r.status_code == 200:
            total_ratings += 1
        else:
            failed_ratings += 1

elapsed = time.time() - t0
print(f"  Ratings submitted: {total_ratings}")
print(f"  Failed ratings: {failed_ratings}")
print(f"  Time: {elapsed:.1f}s")

# ---------------------------------------------------------------------------
# 4. Marketplace — 20 listings, 15 trades
# ---------------------------------------------------------------------------
print("\n=== MARKETPLACE (20 listings, attempt 15 trades) ===")

# Refresh user balances
users_data = get("/users/")
balance_map = {u["handle"]: u["token_balance"] for u in users_data}
score_map = {u["handle"]: u["total_score"] for u in users_data}

listings_created = []
# Pick assets with tokens to list
for asset_id, submitter, _ in random.sample(submitted_assets, min(20, len(submitted_assets))):
    if balance_map.get(submitter, 0) > 0.5:
        price = round(random.uniform(0.5, min(3.0, balance_map[submitter] * 0.5)), 2)
        r = post("/marketplace/listings", {
            "asset_id": asset_id,
            "seller_handle": submitter,
            "price": price,
        })
        if r.status_code == 201:
            listings_created.append((r.json()["id"], submitter, price))

print(f"  Listings created: {len(listings_created)}")

trades_done = 0
trades_failed = 0
for listing_id, seller, price in listings_created[:15]:
    # Find a buyer with enough tokens who isn't the seller
    buyers = [h for h, bal in balance_map.items() if bal >= price and h != seller]
    if not buyers:
        trades_failed += 1
        continue
    buyer = random.choice(buyers)
    r = post(f"/marketplace/listings/{listing_id}/buy", params={"buyer_handle": buyer})
    if r.status_code == 200:
        trades_done += 1
        balance_map[buyer] -= price
        balance_map[seller] += price * 0.99
    else:
        trades_failed += 1

print(f"  Trades completed: {trades_done}")
print(f"  Trades failed: {trades_failed}")

# ---------------------------------------------------------------------------
# 5. DIAGNOSTIC REPORT
# ---------------------------------------------------------------------------
print("\n" + "="*70)
print("  DIAGNOSTIC REPORT — AGORA 100-USER SIMULATION")
print("="*70)

all_assets = get("/assets/")
all_users = get("/users/")
status = get("/status")
bank = get("/bank/balance")

live_assets = [a for a in all_assets if not a["is_deleted"]]
pruned_assets = [a for a in all_assets if a["is_deleted"]]
rated_assets = [a for a in live_assets if a["rating_count"] > 0]

avg_ratings = [a["avg_rating"] for a in rated_assets]
token_minted = [a["tokens_minted"] for a in rated_assets]
token_balances = [u["token_balance"] for u in all_users]
total_scores = [u["total_score"] for u in all_users]

# Token distribution
tokens_with_balance = [b for b in token_balances if b > 0]
eligible_voters = [u for u in all_users if u["total_score"] >= 20.0]

print(f"\n📊 NETWORK OVERVIEW")
print(f"  Total users:          {status['users']}")
print(f"  Live assets:          {status['assets']}")
print(f"  Pruned assets:        {len(pruned_assets)}")
print(f"  Completed trades:     {status['trades']}")
print(f"  Bank balance:         {bank['balance']:.4f} tokens")

print(f"\n📈 ASSET QUALITY DISTRIBUTION")
if avg_ratings:
    print(f"  Mean avg rating:      {statistics.mean(avg_ratings):.3f}")
    print(f"  Median avg rating:    {statistics.median(avg_ratings):.3f}")
    print(f"  Std dev:              {statistics.stdev(avg_ratings):.3f}")
    print(f"  Min / Max:            {min(avg_ratings):.3f} / {max(avg_ratings):.3f}")
    below2 = sum(1 for r in avg_ratings if r < 2.0)
    above8 = sum(1 for r in avg_ratings if r > 8.0)
    print(f"  Assets avg < 2.0:     {below2}  ← health trigger zone")
    print(f"  Assets avg > 8.0:     {above8}")

print(f"\n💰 TOKEN ECONOMY")
print(f"  Total tokens minted:  {sum(token_minted):.4f}")
print(f"  Bank balance:         {bank['balance']:.4f}")
total_in_circulation = sum(token_balances)
print(f"  In circulation:       {total_in_circulation:.4f}")
if tokens_with_balance:
    print(f"  Users with tokens:    {len(tokens_with_balance)} / {len(all_users)}")
    print(f"  Max balance:          {max(token_balances):.4f}")
    print(f"  Mean balance (all):   {statistics.mean(token_balances):.4f}")
    gini_sorted = sorted(token_balances)
    n = len(gini_sorted)
    gini = (2 * sum((i+1)*v for i,v in enumerate(gini_sorted)) / (n * sum(gini_sorted)) - (n+1)/n) if sum(gini_sorted) > 0 else 0
    print(f"  Gini coefficient:     {gini:.3f}  (0=equal, 1=max inequality)")

print(f"\n🏆 USER SCORE DISTRIBUTION")
print(f"  Mean total score:     {statistics.mean(total_scores):.3f}")
print(f"  Median total score:   {statistics.median(total_scores):.3f}")
print(f"  Max total score:      {max(total_scores):.3f}")
print(f"  Eligible voters (≥20):{len(eligible_voters)}")
print(f"  % eligible:           {len(eligible_voters)/len(all_users)*100:.1f}%")

print(f"\n🗳️  GOVERNANCE READINESS")
if len(eligible_voters) == 0:
    print(f"  ⚠️  NO eligible voters yet — network needs more activity")
    print(f"     Closest to threshold: {max(total_scores):.2f} / 20.0")
else:
    quorum_needed = int(len(eligible_voters) * 0.5) + 1
    print(f"  Eligible voters:      {len(eligible_voters)}")
    print(f"  Quorum needed (50%):  {quorum_needed}")

print(f"\n🔪 PRUNING")
print(f"  Pruned this run:      {len(pruned_assets)}")
if pruned_assets:
    for a in pruned_assets:
        print(f"    - '{a['title'][:50]}' (avg: {a['avg_rating']:.2f})")
else:
    print(f"  None pruned — all assets above threshold or insufficient rater coverage")
    near_prune = [a for a in live_assets if a["avg_rating"] > 0 and a["avg_rating"] <= 2.0]
    if near_prune:
        print(f"  Assets near prune threshold (avg ≤ 2.0):")
        for a in near_prune:
            print(f"    ⚠️  '{a['title'][:50]}' avg={a['avg_rating']:.2f} ratings={a['rating_count']}")

print(f"\n🐛 DIAGNOSTIC FLAGS")
issues = []

# Check 1: Percentile scoring flat at 15 (all users equal)
unique_scores = set(round(s, 2) for s in total_scores)
if len(unique_scores) < 5:
    issues.append(f"SCORE COMPRESSION: Only {len(unique_scores)} unique total scores — percentile normalization may be flattening results")

# Check 2: Bank balance vs circulation
bank_bal = bank['balance']
circ = sum(token_balances)
if bank_bal > circ * 5:
    issues.append(f"BANK DOMINANCE: Bank holds {bank_bal:.2f} vs {circ:.2f} in circulation — bank is accumulating too aggressively")

# Check 3: Governance lock
if len(eligible_voters) == 0:
    issues.append("GOVERNANCE LOCKED: No users eligible to vote — network cannot self-govern yet")

# Check 4: Mint recalculation on every new user join
# (this recalculates ALL user scores for every new joiner — O(n²) concern)
issues.append(f"PERFORMANCE: score recalc on every new user join = O(n²) at scale — {len(all_users)} users × {len(submitted_assets)} assets = {len(all_users)*len(submitted_assets)} ops at join time")

# Check 5: Token concentration
if tokens_with_balance:
    top10pct = sorted(token_balances, reverse=True)[:max(1, len(token_balances)//10)]
    top10_share = sum(top10pct) / sum(token_balances) if sum(token_balances) > 0 else 0
    if top10_share > 0.5:
        issues.append(f"TOKEN CONCENTRATION: Top 10% hold {top10_share*100:.1f}% of tokens")

# Check 6: Submission-only path to rating score (submitters who never rate get high sub scores)
non_raters_with_tokens = [u for u in all_users if u["rater_raw"] == 0 and u["token_balance"] > 0]
if non_raters_with_tokens:
    issues.append(f"FREE RIDER: {len(non_raters_with_tokens)} users earned tokens without ever rating")

# Check 7: Re-rating attack surface
issues.append("RE-RATING: Users can re-rate assets. With coordinated re-rating, a group could manipulate avg_rating to trigger/avoid pruning. No re-rate cooldown implemented.")

# Check 8: Mint remainder going to bank on every recalc (not just first)
issues.append("BANK INFLATION: Bank receives mint_remainder on EVERY recalculation, not just the first. If asset is re-rated 100x, bank gets 100x the remainder. Likely unintended.")

if issues:
    for i, issue in enumerate(issues, 1):
        print(f"  [{i}] ⚠️  {issue}")
else:
    print("  No issues detected.")

print(f"\n📋 TOP 10 ASSETS BY RATING")
top_assets = sorted(rated_assets, key=lambda x: x["avg_rating"], reverse=True)[:10]
print(f"  {'Title':<40} {'Avg':>5} {'#Ratings':>9} {'Minted':>10}")
print(f"  {'-'*68}")
for a in top_assets:
    print(f"  {a['title'][:40]:<40} {a['avg_rating']:>5.2f} {a['rating_count']:>9} {a['tokens_minted']:>10.4f}")

print(f"\n📋 TOP 10 USERS BY SCORE")
top_users = sorted(all_users, key=lambda x: x["total_score"], reverse=True)[:10]
print(f"  {'Handle':<15} {'Total':>6} {'Sub':>6} {'Rate':>6} {'Trade':>6} {'Tokens':>10}")
print(f"  {'-'*58}")
for u in top_users:
    print(f"  {u['handle']:<15} {u['total_score']:>6.2f} {u['submission_score']:>6.2f} {u['rater_score']:>6.2f} {u['trade_score']:>6.2f} {u['token_balance']:>10.4f}")

print("\n✅ Simulation complete.\n")
