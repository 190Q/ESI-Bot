#!/bin/bash

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "========================================"
echo "  ESI-Bot Dependency Installer"
echo "========================================"
echo ""

if ! command -v python3 &> /dev/null; then
    echo "[ERROR] python3 is not installed or not in PATH."
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "[INFO] Python version: $PYTHON_VERSION"

if ! python3 -m pip --version &> /dev/null; then
    echo "[INFO] pip not found, installing..."
    sudo apt-get update -qq
    sudo apt-get install -y python3-pip
    if [ $? -ne 0 ]; then
        echo "[ERROR] Failed to install pip."
        exit 1
    fi
fi

echo "[INFO] Upgrading pip..."
python3 -m pip install --upgrade pip --quiet

if [ ! -f "$BOT_DIR/requirements.txt" ]; then
    echo "[ERROR] requirements.txt not found at $BOT_DIR/requirements.txt"
    exit 1
fi

echo "[INFO] Installing dependencies from requirements.txt..."
python3 -m pip install -r "$BOT_DIR/requirements.txt"

if [ $? -ne 0 ]; then
    echo ""
    echo "[ERROR] Failed to install one or more dependencies."
    exit 1
fi

echo ""
echo "[OK] All dependencies installed successfully."
echo "     You can now start the bot with: bash scripts/start_bot.sh"
echo "     You can start the trackers with: bash scripts/start_trackers.sh"
