#!/bin/bash

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION="esi-trackers"

echo "========================================"
echo "  ESI-Bot Trackers Restart"
echo "========================================"
echo ""

if screen -list | grep -q "\.${SESSION}"; then
    echo "[INFO] Stopping existing trackers session '${SESSION}'..."
    screen -S "$SESSION" -X quit
    sleep 2

    if screen -list | grep -q "\.${SESSION}"; then
        echo "[ERROR] Could not stop existing session. Aborting restart."
        exit 1
    fi
    echo "[OK] Existing session stopped."
else
    echo "[INFO] No existing session '${SESSION}' found, starting fresh."
fi

echo "[INFO] Starting trackers in screen session '${SESSION}'..."
screen -S "$SESSION" -dm bash -c "cd '$BOT_DIR' && python3 trackers/main.py"
sleep 1

if screen -list | grep -q "\.${SESSION}"; then
    echo "[OK] Trackers restarted successfully."
    echo ""
    echo "     Attach to session : screen -r ${SESSION}"
    echo "     Detach from session: Ctrl+A then D"
    echo "     View logs          : bash scripts/view_tracker_logs.sh"
else
    echo "[ERROR] Trackers failed to start after restart."
    exit 1
fi
