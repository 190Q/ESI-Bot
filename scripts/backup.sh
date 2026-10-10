#!/bin/bash
# Back up the bot's databases and data files.
# Wrapper around scripts/backup.py for cron and manual use on the host.
# All arguments are passed straight through, e.g.:
#   bash scripts/backup.sh --dest /mnt/backups/esi-bot --keep 30

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "========================================"
echo "  ESI-Bot Backup"
echo "========================================"
echo ""

if [ ! -f "$BOT_DIR/scripts/backup.py" ]; then
    echo "[ERROR] scripts/backup.py not found at $BOT_DIR/scripts/backup.py"
    exit 1
fi

cd "$BOT_DIR" || exit 1

python3 scripts/backup.py "$@"
STATUS=$?

echo ""
if [ $STATUS -eq 0 ]; then
    echo "[OK] Backup finished."
elif [ $STATUS -eq 2 ]; then
    echo "[WARN] Backup finished with problems. See the warnings above."
else
    echo "[ERROR] Backup failed (exit code $STATUS)."
fi

exit $STATUS
