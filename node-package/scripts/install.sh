#!/bin/bash
# Agora Node Installer — v2
# Pulls code directly from the Agora network. No GitHub dependency.
# Usage: bash install.sh [known_node_url]
#
# Bootstrap: curl http://68.39.46.12:8001/download/node-package | tar xz && bash install.sh

set -e

EXPECTED_HASH="79f924d5e4167040233c4fb54f4312927bf33cdf04280cb3cbac248d8649bcde"
AGORA_DIR="$HOME/agora-node"

# ── 1. Find a live node ──────────────────────────────────────────────────────
# Caller can pass a known node URL, otherwise we try the known network.
SEED_URL="${1:-http://68.39.46.12:8001}"

echo "=== Agora Node Installer v2 ==="
echo "Bootstrapping from: $SEED_URL"

# Resolve live node list from the seed — fall back through all known nodes
find_live_node() {
    local seed="$1"
    # Try the seed first
    if curl -sf "$seed/health" > /dev/null 2>&1; then
        echo "$seed"
        return 0
    fi
    # Try /federation/peers first (gossip-aware endpoint)
    PEERS=$(curl -sf "$seed/federation/peers" 2>/dev/null \
        | python3 -c "
import sys, json
d = json.load(sys.stdin)
for n in d.get('peers', []):
    url = n.get('public_url','')
    if url: print(url)
" 2>/dev/null)
    for peer in $PEERS; do
        if curl -sf "$peer/health" > /dev/null 2>&1; then
            echo "$peer"
            return 0
        fi
    done
    # Fall back to /federation/nodes
    PEERS=$(curl -sf "$seed/federation/nodes" 2>/dev/null \
        | python3 -c "
import sys, json
d = json.load(sys.stdin)
for n in d.get('nodes', []):
    url = n.get('public_url','')
    if url: print(url)
" 2>/dev/null)
    for peer in $PEERS; do
        if curl -sf "$peer/health" > /dev/null 2>&1; then
            echo "$peer"
            return 0
        fi
    done
    return 1
}

LIVE_NODE=$(find_live_node "$SEED_URL") || {
    echo "❌ No live Agora node reachable. Try again later or provide a known node URL:"
    echo "   bash install.sh http://<node-ip>:<port>"
    exit 1
}
echo "✅ Live node: $LIVE_NODE"

# ── 2. Pull codebase from network ────────────────────────────────────────────
echo "Downloading codebase from network..."
mkdir -p "$AGORA_DIR"
cd "$AGORA_DIR"

curl -sf "$LIVE_NODE/download/node-package" -o agora-node.tar.gz
tar -xzf agora-node.tar.gz --strip-components=0
rm agora-node.tar.gz
echo "✅ Codebase downloaded"

# ── 3. Verify codebase hash ──────────────────────────────────────────────────
ACTUAL_HASH=$(find . -name "*.py" \
    -not -path "./venv/*" \
    -not -path "./__pycache__/*" \
    | sort | xargs shasum -a 256 2>/dev/null | shasum -a 256 | cut -d' ' -f1)

if [ "$ACTUAL_HASH" != "$EXPECTED_HASH" ]; then
    echo "⚠️  Hash mismatch!"
    echo "   Expected: $EXPECTED_HASH"
    echo "   Got:      $ACTUAL_HASH"
    echo "   The network may have updated. Check /federation/status for current hash."
    read -p "Continue anyway? (y/N): " confirm
    [ "$confirm" != "y" ] && exit 1
else
    echo "✅ Codebase verified"
fi

# ── 4. Install dependencies ──────────────────────────────────────────────────
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt --quiet
echo "✅ Dependencies installed"

# ── 5. Initialize local database ────────────────────────────────────────────
python3 -c "from db import init_db; init_db(); print('✅ Local database initialized')"

# ── 6. Register with the network ─────────────────────────────────────────────
echo ""
echo "=== Register with the Network ==="
read -p "Choose a handle for your node: " HANDLE

RESULT=$(curl -s -X POST "$LIVE_NODE/users/" \
    -H "Content-Type: application/json" \
    -d "{\"handle\": \"$HANDLE\", \"agent_type\": \"agent\"}" 2>/dev/null)

API_KEY=$(echo "$RESULT" | python3 -c \
    "import json,sys; d=json.load(sys.stdin); print(d.get('api_key',''))" 2>/dev/null)

if [ -z "$API_KEY" ]; then
    echo "❌ Registration failed: $RESULT"
    exit 1
fi

echo "✅ Registered as @$HANDLE"
echo "   API Key: $API_KEY"
echo "   ⚠️  SAVE THIS KEY — it will not be shown again"

# ── 7. Register node with federation ─────────────────────────────────────────
read -p "Your node's public URL (e.g. http://1.2.3.4:8002): " MY_URL
read -p "Your node ID (e.g. node_2): " NODE_ID

REG_RESULT=$(curl -s -X POST "$LIVE_NODE/federation/register" \
    -H "Content-Type: application/json" \
    -d "{
        \"node_id\": \"$NODE_ID\",
        \"operator_handle\": \"$HANDLE\",
        \"public_url\": \"$MY_URL\",
        \"codebase_hash\": \"$ACTUAL_HASH\"
    }")

echo "$REG_RESULT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'✅ {d.get(\"message\", \"Registered\")}')
print(f'   Network size: {d.get(\"network_size\", \"?\")} nodes')
# Save peers list so this node knows the full network
peers = d.get('peers', [])
if peers:
    with open('peers.json', 'w') as f:
        json.dump({'peers': peers}, f, indent=2)
    print(f'   Saved {len(peers)} peers to peers.json')
" 2>/dev/null

# ── 8. Sync state from network ───────────────────────────────────────────────
echo "Syncing network state..."
python3 -c "
import requests, json
r = requests.get('$LIVE_NODE/federation/snapshot', timeout=30)
d = r.json()
print(f'  Users: {len(d[\"users\"])} | Assets: {len(d[\"assets\"])}')
with open('bootstrap-snapshot.json', 'w') as f:
    json.dump(d, f, indent=2)
print('✅ Snapshot saved to bootstrap-snapshot.json')
"

# ── 9. Write config ───────────────────────────────────────────────────────────
cat > .env << ENVEOF
NODE_HANDLE=$HANDLE
NODE_API_KEY=$API_KEY
NODE_ID=$NODE_ID
MY_URL=$MY_URL
SEED_NODE=$LIVE_NODE
PORT=8002
ENVEOF

# ── 10. Set up auto-updater (optional) ───────────────────────────────────────
echo ""
read -p "Set up automatic updates? Checks for updates hourly from your peers. (y/N): " setup_auto
if [ "$setup_auto" = "y" ] || [ "$setup_auto" = "Y" ]; then
    chmod +x "$AGORA_DIR/scripts/auto_update.sh"
    PLIST_SRC="$AGORA_DIR/node-package/launchd/ai.agora.autoupdate.plist"
    PLIST_DEST="$HOME/Library/LaunchAgents/ai.agora.autoupdate.plist"
    sed "s|__AGORA_DIR__|$AGORA_DIR|g" "$PLIST_SRC" > "$PLIST_DEST"
    launchctl load "$PLIST_DEST"
    echo "✅ Auto-updater installed — checks every hour from network peers"
else
    echo "ℹ️  To update manually: bash $AGORA_DIR/scripts/auto_update.sh"
fi

echo ""
echo "=== Node Ready ==="
echo "Start: python3 main.py --port 8002"
echo "UI:    http://localhost:8002/ui"
echo ""
echo "No GitHub. No tunnels. Pure peer-to-peer."
echo "Welcome to Agora."
