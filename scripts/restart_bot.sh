#!/bin/bash

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION="esi-bot"

echo "========================================"
echo "  ESI-Bot Restart"
echo "========================================"
echo ""

if screen -list | grep -q "\.${SESSION}"; then
    echo "[INFO] Stopping existing bot session '${SESSION}'..."
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

echo "[INFO] Starting bot in screen session '${SESSION}'..."
screen -S "$SESSION" -dm bash -c "cd '$BOT_DIR' && python3 bot.py"
sleep 1

if screen -list | grep -q "\.${SESSION}"; then
    echo "[OK] Bot restarted successfully."
    echo ""
    echo "     Attach to session : screen -r ${SESSION}"
    echo "     Detach from session: Ctrl+A then D"
    echo "     View logs          : bash scripts/view_bot_logs.sh"
else
    echo "[ERROR] Bot failed to start after restart."
    exit 1
fi
