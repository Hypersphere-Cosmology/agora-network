#!/bin/bash
# Agora startup script for launchd
# Waits for port 8001 to be free before starting

MAX_WAIT=30
WAITED=0

while lsof -i :8001 >/dev/null 2>&1; do
    if [ $WAITED -ge $MAX_WAIT ]; then
        echo "$(date): Port 8001 still busy after ${MAX_WAIT}s, killing occupant"
        pkill -f "python3 main.py" 2>/dev/null
        sleep 2
        break
    fi
    echo "$(date): Waiting for port 8001... (${WAITED}s)"
    sleep 2
    WAITED=$((WAITED + 2))
done

cd /Users/viralsatan/projects/the-network
exec /Users/viralsatan/projects/the-network/venv/bin/python3 main.py
