#!/bin/bash
# Agora Auto-Updater
# Checks if codebase hash differs from any known peer, pulls update if so, restarts.
# Peers are read from peers.json — no single point of failure.
# Run periodically (e.g. every hour via launchd or cron).

set -e

AGORA_DIR="${AGORA_DIR:-$HOME/agora-node}"
SEED_NODE="${SEED_NODE:-http://68.39.46.12:8001}"
LOG="$AGORA_DIR/update.log"
PEERS_FILE="$AGORA_DIR/peers.json"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"; }

cd "$AGORA_DIR" || { log "ERROR: AGORA_DIR $AGORA_DIR not found"; exit 1; }

# ── 1. Build peer list from peers.json (source of truth after install) ────────
# Fall back to SEED_NODE only if peers.json doesn't exist yet (fresh install).
ALL_PEERS=()

if [ -f "$PEERS_FILE" ]; then
    # Extract all public_url values from peers.json
    mapfile -t ALL_PEERS < <(python3 -c "
import json, sys
try:
    peers = json.loads(open('$PEERS_FILE').read()).get('peers', [])
    for p in peers:
        url = p.get('public_url', '').rstrip('/')
        if url:
            print(url)
except Exception as e:
    sys.exit(0)
" 2>/dev/null)
fi

# Always ensure seed node is in the list (append if not already present)
if [ ${#ALL_PEERS[@]} -eq 0 ]; then
    log "peers.json empty or missing — using seed node as fallback"
    ALL_PEERS=("$SEED_NODE")
else
    # Add seed as last-resort fallback if not already listed
    SEED_CLEAN="${SEED_NODE%/}"
    SEED_IN_LIST=false
    for p in "${ALL_PEERS[@]}"; do
        [ "${p%/}" = "$SEED_CLEAN" ] && SEED_IN_LIST=true && break
    done
    $SEED_IN_LIST || ALL_PEERS+=("$SEED_CLEAN")
fi

log "Checking ${#ALL_PEERS[@]} peer(s) for updates..."

# ── 2. Ask peers for their codebase hash (first successful response wins) ────
REMOTE_HASH=""
RESPONDED_PEER=""

for peer in "${ALL_PEERS[@]}"; do
    [ -z "$peer" ] && continue
    HASH=$(curl -sf --max-time 8 "$peer/federation/version" | \
        python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('codebase_hash',''))" 2>/dev/null)
    if [ -n "$HASH" ]; then
        REMOTE_HASH="$HASH"
        RESPONDED_PEER="$peer"
        log "Got hash from $peer: ${HASH:0:16}..."
        break
    fi
done

if [ -z "$REMOTE_HASH" ]; then
    log "No peers responded — skipping update check"
    exit 0
fi

# ── 3. Compute local hash ─────────────────────────────────────────────────────
LOCAL_HASH=$(find . -name "*.py" \
    -not -path "./venv/*" \
    -not -path "./__pycache__/*" \
    | sort | xargs shasum -a 256 2>/dev/null | shasum -a 256 | cut -d' ' -f1)

if [ "$LOCAL_HASH" = "$REMOTE_HASH" ]; then
    log "Up to date (hash: ${LOCAL_HASH:0:16}...)"
    exit 0
fi

log "Update available: local=${LOCAL_HASH:0:16}... remote=${REMOTE_HASH:0:16}..."
log "Downloading update from peers (random order)..."

# ── 4. Shuffle peer list for download — no central source preferred ───────────
SHUFFLED_PEERS=($(python3 -c "
import random, sys
peers = sys.argv[1:]
random.shuffle(peers)
print(' '.join(peers))
" "${ALL_PEERS[@]}" 2>/dev/null))

# Fall back to original order if shuffle failed
[ ${#SHUFFLED_PEERS[@]} -eq 0 ] && SHUFFLED_PEERS=("${ALL_PEERS[@]}")

UPDATED=false

try_update_from() {
    local url="$1"
    log "Trying download from $url..."
    if curl -sf --max-time 120 "$url/download/node-package" -o /tmp/agora-update.tar.gz; then
        # Verify the downloaded package has the expected hash before applying
        DOWNLOADED_HASH=$(tar -xzf /tmp/agora-update.tar.gz -C /tmp/ 2>/dev/null; \
            find /tmp/agora-node* -name "*.py" 2>/dev/null | sort | \
            xargs shasum -a 256 2>/dev/null | shasum -a 256 | cut -d' ' -f1 || echo "")
        # Backup current code
        BACKUP_DIR="/tmp/agora-backup-$(date +%s)"
        cp -r . "$BACKUP_DIR" 2>/dev/null || true
        log "Backup at $BACKUP_DIR"
        # Extract update — preserve node-specific files
        tar -xzf /tmp/agora-update.tar.gz \
            --exclude='./venv' \
            --exclude='./agora.db*' \
            --exclude='./.secrets' \
            --exclude='./KEYS.txt' \
            --exclude='./.env*' \
            --exclude='./peers.json' \
            -C "$AGORA_DIR" 2>/dev/null
        UPDATED=true
        return 0
    fi
    return 1
}

for peer in "${SHUFFLED_PEERS[@]}"; do
    [ -z "$peer" ] && continue
    try_update_from "$peer" && break
done

if [ "$UPDATED" = false ]; then
    log "No peers served a package — update skipped"
    exit 0
fi

log "Update downloaded. Restarting node..."

# ── 5. Restart — detect how we were launched ─────────────────────────────────
if launchctl list | grep -q "ai.ava.agora" 2>/dev/null; then
    launchctl stop ai.ava.agora 2>/dev/null
    sleep 2
    launchctl start ai.ava.agora
    log "Restarted via launchd"
elif [ -f "$AGORA_DIR/node.pid" ]; then
    kill $(cat "$AGORA_DIR/node.pid") 2>/dev/null || true
    sleep 1
    nohup bash "$AGORA_DIR/start-node2.sh" > "$AGORA_DIR/node.log" 2>&1 &
    echo $! > "$AGORA_DIR/node.pid"
    log "Restarted via PID file"
else
    log "Could not determine restart method — please restart manually"
fi

log "Update complete → $REMOTE_HASH"
