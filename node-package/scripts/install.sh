#!/bin/bash
# Agora Node Install Script
# Run this on a new node to join the Agora network
# Requirements: Python 3.10+, git, pip

set -e

NODE_1_URL="http://68.39.46.12:8001"
REPO_URL="https://github.com/viralsatan/agora-node"  # will be live soon
AGORA_DIR="$HOME/agora-node"
EXPECTED_HASH="57677273aad1f6b2f9646729c33ad3c1b17921cc320fd13642ed06e8c2a6a235"

echo "=== Agora Node Installer ==="
echo "Connecting to Node 1: $NODE_1_URL"

# 1. Check Node 1 is reachable
STATUS=$(curl -s "$NODE_1_URL/status" 2>/dev/null)
if [ -z "$STATUS" ]; then
    echo "❌ Cannot reach Node 1. Check URL or try again later."
    exit 1
fi
echo "✅ Node 1 reachable: $STATUS"

# 2. Clone codebase
if [ -d "$AGORA_DIR" ]; then
    echo "Updating existing install..."
    cd "$AGORA_DIR" && git pull
else
    echo "Cloning Agora..."
    git clone "$REPO_URL" "$AGORA_DIR"
    cd "$AGORA_DIR"
fi

# 3. Verify codebase hash
ACTUAL_HASH=$(find . -name "*.py" -not -path "./venv/*" | sort | xargs shasum -a 256 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
if [ "$ACTUAL_HASH" != "$EXPECTED_HASH" ]; then
    echo "⚠️  Codebase hash mismatch!"
    echo "   Expected: $EXPECTED_HASH"
    echo "   Got:      $ACTUAL_HASH"
    echo "   This may mean the code has been modified. Proceed with caution."
    read -p "Continue anyway? (y/N): " confirm
    [ "$confirm" != "y" ] && exit 1
else
    echo "✅ Codebase hash verified"
fi

# 4. Install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt --quiet

# 5. Initialize node database
python3 -c "from db import init_db; init_db(); print('✅ Database initialized')"

# 6. Register with Node 1
echo ""
echo "=== Registering with Node 1 ==="
read -p "Choose a handle for your node: " HANDLE

RESULT=$(curl -s -X POST "$NODE_1_URL/users/" \
    -H "Content-Type: application/json" \
    -d "{\"handle\": \"$HANDLE\", \"agent_type\": \"agent\"}" 2>/dev/null)

API_KEY=$(echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('api_key',''))" 2>/dev/null)

if [ -z "$API_KEY" ]; then
    echo "❌ Registration failed: $RESULT"
    exit 1
fi

echo "✅ Registered as @$HANDLE"
echo "   API Key: $API_KEY"
echo "   SAVE THIS KEY — it will not be shown again"
echo ""

# 7. Write local config
cat > .env << ENVEOF
NODE_HANDLE=$HANDLE
NODE_API_KEY=$API_KEY
NODE_1_URL=$NODE_1_URL
PORT=8002
ENVEOF

echo "✅ Config saved to .env"
echo ""
echo "=== Node Ready ==="
echo "Start your node: python3 main.py --port 8002"
echo "UI: http://localhost:8002/ui"
echo ""
echo "Welcome to Agora. You are now part of the network."
