#!/bin/bash
# Agora backup script — run manually or via cron
# Keeps last 10 backups, removes older ones

BACKUP_BASE=~/projects/the-network/backups
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR=$BACKUP_BASE/$TIMESTAMP

mkdir -p $BACKUP_DIR

# Database
cp ~/projects/the-network/agora.db $BACKUP_DIR/agora.db

# File uploads (asset attachments)
if [ -d ~/projects/the-network/uploads ]; then
    cp -r ~/projects/the-network/uploads $BACKUP_DIR/uploads
fi

# Treasury private key (if exists)
if [ -d ~/projects/the-network/.secrets ]; then
    cp -r ~/projects/the-network/.secrets $BACKUP_DIR/.secrets
fi

# Verify backup integrity
ORIG_SIZE=$(du -s ~/projects/the-network/agora.db | cut -f1)
BACK_SIZE=$(du -s $BACKUP_DIR/agora.db | cut -f1)

if [ "$ORIG_SIZE" != "$BACK_SIZE" ]; then
    echo "⚠️  WARNING: Backup size mismatch! Original: ${ORIG_SIZE}k, Backup: ${BACK_SIZE}k"
    exit 1
fi

echo "✅ Backup created: $BACKUP_DIR ($(du -sh $BACKUP_DIR | cut -f1))"

# Rotate: keep last 10, delete older ones
cd $BACKUP_BASE
ls -dt */ | tail -n +11 | xargs rm -rf 2>/dev/null && echo "Old backups pruned" || true

echo "Backups retained: $(ls $BACKUP_BASE | wc -l | tr -d ' ')"
