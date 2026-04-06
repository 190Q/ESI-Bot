"""
Project directory constants.

All paths are absolute, derived from this file's location.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_DIR = PROJECT_ROOT / "config"
DB_DIR = PROJECT_ROOT / "databases"
