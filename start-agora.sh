#!/bin/bash
# Agora startup script for launchd
# Uses python to check port (lsof not available in launchd PATH)
MAX_WAIT=30
WAITED=0

PYTHON=/Users/viralsatan/projects/the-network/venv/bin/python3

port_in_use() {
    $PYTHON -c "import socket; s=socket.socket(); r=s.connect_ex(('127.0.0.1',8001)); s.close(); exit(0 if r==0 else 1)" 2>/dev/null
}

while port_in_use; do
    if [ $WAITED -ge $MAX_WAIT ]; then
        pkill -f "python3 main.py" 2>/dev/null
        sleep 2
        break
    fi
    sleep 2
    WAITED=$((WAITED + 2))
done

cd /Users/viralsatan/projects/the-network
$PYTHON main.py
