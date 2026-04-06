#!/bin/bash

SESSION="esi-bot"

echo "========================================"
echo "  ESI-Bot Stop"
echo "========================================"
echo ""

if ! screen -list | grep -q "\.${SESSION}"; then
    echo "[INFO] No running screen session '${SESSION}' found. Nothing to stop."
    exit 0
fi

echo "[INFO] Stopping bot (screen session '${SESSION}')..."
screen -S "$SESSION" -X quit

sleep 1

if screen -list | grep -q "\.${SESSION}"; then
    echo "[ERROR] Failed to stop the bot. You may need to kill it manually:"
    echo "        screen -S ${SESSION} -X kill"
    exit 1
fi

echo "[OK] Bot stopped."
