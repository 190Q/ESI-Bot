#!/bin/bash

SESSION="esi-bot"

echo "========================================"
echo "  ESI-Bot Logs"
echo "========================================"
echo ""

if ! screen -list | grep -q "\.${SESSION}"; then
    echo "[ERROR] No running screen session '${SESSION}' found."
    echo "        Start the bot first with: bash scripts/start_bot.sh"
    exit 1
fi

echo "[INFO] Attaching to screen session '${SESSION}'..."
echo "       Detach with: Ctrl+A then D"
echo ""
sleep 1

screen -r "$SESSION"
