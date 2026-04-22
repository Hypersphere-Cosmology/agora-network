#!/bin/bash
# Kill any existing ngrok before starting (prevents ERR_NGROK_334)
pkill -f "ngrok http" 2>/dev/null
sleep 2
exec /opt/homebrew/bin/ngrok http 8001 --domain=garrison-declinatory-biannually.ngrok-free.dev
