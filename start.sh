#!/bin/bash
pkill -f "ngrok http" 2>/dev/null
pkill -f "python3 main.py" 2>/dev/null
sleep 1

cd ~/projects/the-network
source venv/bin/activate
python3 main.py > /tmp/agora.log 2>&1 &
sleep 2

ngrok http 8001 --domain=garrison-declinatory-biannually.ngrok-free.dev
