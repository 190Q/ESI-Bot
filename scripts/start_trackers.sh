#!/bin/bash

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION="esi-trackers"

echo "========================================"
echo "  ESI-Bot Trackers Start"
echo "========================================"
echo ""

if ! command -v screen &> /dev/null; then
    echo "[ERROR] 'screen' is not installed. Run: sudo apt-get install screen"
    exit 1
fi

if screen -list | grep -q "\.${SESSION}"; then
    echo "[ERROR] Trackers are already running in screen session '${SESSION}'."
    echo "        Use 'bash scripts/restart_trackers.sh' to restart them."
    exit 1
fi

if [ ! -f "$BOT_DIR/trackers/main.py" ]; then
    echo "[ERROR] trackers/main.py not found at $BOT_DIR/trackers/main.py"
    exit 1
fi

if [ ! -f "$BOT_DIR/.env" ]; then
    echo "[ERROR] .env file not found at $BOT_DIR/.env"
    exit 1
fi

echo "[INFO] Starting trackers in screen session '${SESSION}'..."
screen -S "$SESSION" -dm bash -c "cd '$BOT_DIR' && python3 trackers/main.py"
sleep 1

if screen -list | grep -q "\.${SESSION}"; then
    echo "[OK] Trackers started successfully."
    echo ""
    echo "     Attach to session : screen -r ${SESSION}"
    echo "     Detach from session: Ctrl+A then D"
    echo "     View logs          : bash scripts/view_tracker_logs.sh"
else
    echo "[ERROR] Trackers failed to start. Check your .env configuration."
    exit 1
fi
