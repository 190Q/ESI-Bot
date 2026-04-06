#!/bin/bash

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION="esi-bot"

echo "========================================"
echo "  ESI-Bot Start"
echo "========================================"
echo ""

if ! command -v screen &> /dev/null; then
    echo "[ERROR] 'screen' is not installed. Run: sudo apt-get install screen"
    exit 1
fi

if screen -list | grep -q "\.${SESSION}"; then
    echo "[ERROR] Bot is already running in screen session '${SESSION}'."
    echo "        Use 'bash scripts/restart_bot.sh' to restart it."
    exit 1
fi

if [ ! -f "$BOT_DIR/bot.py" ]; then
    echo "[ERROR] bot.py not found at $BOT_DIR/bot.py"
    exit 1
fi

if [ ! -f "$BOT_DIR/.env" ]; then
    echo "[ERROR] .env file not found at $BOT_DIR/.env"
    exit 1
fi

echo "[INFO] Starting bot in screen session '${SESSION}'..."
screen -S "$SESSION" -dm bash -c "cd '$BOT_DIR' && python3 bot.py"
sleep 1

if screen -list | grep -q "\.${SESSION}"; then
    echo "[OK] Bot started successfully."
    echo ""
    echo "     Attach to session : screen -r ${SESSION}"
    echo "     Detach from session: Ctrl+A then D"
    echo "     View logs          : bash scripts/view_bot_logs.sh"
else
    echo "[ERROR] Bot failed to start. Check your .env configuration."
    exit 1
fi
