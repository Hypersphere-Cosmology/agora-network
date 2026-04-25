#!/bin/bash
# Agora backup — local + external drive
# Keeps last 10 local backups, unlimited on external

BACKUP_BASE=~/projects/the-network/backups
EXTERNAL=/Volumes/Ava/ava-backup/agora
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

backup_to() {
    local DEST=$1/$TIMESTAMP
    mkdir -p $DEST

    cp ~/projects/the-network/agora.db $DEST/agora.db || { echo "❌ DB copy failed to $1"; return 1; }

    [ -d ~/projects/the-network/uploads ] && cp -r ~/projects/the-network/uploads $DEST/uploads
    [ -d ~/projects/the-network/.secrets ] && cp -r ~/projects/the-network/.secrets $DEST/.secrets

    # Verify byte-for-byte
    ORIG=$(python3 -c "import os; print(os.path.getsize(os.path.expanduser('~/projects/the-network/agora.db')))")
    BACK=$(python3 -c "import os; print(os.path.getsize('$DEST/agora.db'))")
    if [ "$ORIG" != "$BACK" ]; then
        echo "⚠️  Byte mismatch at $DEST ($ORIG vs $BACK)"
        return 1
    fi
    echo "✅ Backup OK: $DEST ($(du -sh $DEST | cut -f1))"
}

# Local backup
backup_to $BACKUP_BASE

# External drive backup (silent if not mounted)
if [ -d /Volumes/Ava/ava-backup ]; then
    mkdir -p $EXTERNAL
    backup_to $EXTERNAL
else
    echo "ℹ️  External drive not mounted — local backup only"
fi

# Rotate local: keep last 10
cd $BACKUP_BASE 2>/dev/null
ls -dt */ 2>/dev/null | tail -n +11 | xargs rm -rf 2>/dev/null

echo "Local backups retained: $(ls $BACKUP_BASE 2>/dev/null | wc -l | tr -d ' ')"
[ -d $EXTERNAL ] && echo "External backups retained: $(ls $EXTERNAL 2>/dev/null | wc -l | tr -d ' ')"
