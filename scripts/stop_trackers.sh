#!/bin/bash

SESSION="esi-trackers"

echo "========================================"
echo "  ESI-Bot Trackers Stop"
echo "========================================"
echo ""

if ! screen -list | grep -q "\.${SESSION}"; then
    echo "[INFO] No running screen session '${SESSION}' found. Nothing to stop."
    exit 0
fi

echo "[INFO] Stopping trackers (screen session '${SESSION}')..."
screen -S "$SESSION" -X quit

sleep 1

if screen -list | grep -q "\.${SESSION}"; then
    echo "[ERROR] Failed to stop the trackers. You may need to kill them manually:"
    echo "        screen -S ${SESSION} -X kill"
    exit 1
fi

echo "[OK] Trackers stopped."
