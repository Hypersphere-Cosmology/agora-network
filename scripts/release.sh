#!/bin/bash
# Agora Release Script
# Run after any code changes to keep the node package in sync.
# Usage: ./scripts/release.sh "commit message"

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

MSG="${1:-chore: release - update node package and codebase hash}"

echo "=== Agora Release ==="

# 1. Compute new codebase hash
echo "Computing codebase hash..."
NEW_HASH=$(find . -name "*.py" \
  -not -path "./venv/*" \
  -not -path "./.git/*" \
  -not -path "./__pycache__/*" \
  | sort | xargs shasum -a 256 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
echo "  Hash: $NEW_HASH"

# 2. Update install.sh
sed -i '' "s|EXPECTED_HASH=\".*\"|EXPECTED_HASH=\"$NEW_HASH\"|" \
  node-package/scripts/install.sh
echo "✅ install.sh hash updated"

# 3. Update node-config.json
python3 -c "
import json, pathlib
p = pathlib.Path('node-package/config/node-config.json')
d = json.loads(p.read_text())
d['codebase_hash'] = '$NEW_HASH'
p.write_text(json.dumps(d, indent=2) + '\n')
print('✅ node-config.json hash updated')
"

# 4. Rebuild tar.gz
echo "Rebuilding node package..."
tar -czf static/agora-node.tar.gz \
  --exclude='./.git' \
  --exclude='./venv' \
  --exclude='./agora.db' \
  --exclude='./agora.db-wal' \
  --exclude='./agora.db-shm' \
  --exclude='./KEYS.txt' \
  --exclude='./.secrets' \
  --exclude='./backups' \
  --exclude='./__pycache__' \
  --exclude='./uploads' \
  --exclude='./.DS_Store' \
  --exclude='./moltbook-archive' \
  --exclude='./static/agora-node.tar.gz' \
  .
echo "✅ node package rebuilt ($(du -sh static/agora-node.tar.gz | cut -f1))"

# 5. Commit and push
git add -A
git commit -m "$MSG"
git push origin master:main

echo ""
echo "=== Released ==="
echo "Hash: $NEW_HASH"
echo "Repo: https://github.com/Hypersphere-Cosmology/agora-network"
