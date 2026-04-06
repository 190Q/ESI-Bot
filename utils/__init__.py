"""
ESI-Bot shared utilities.

Submodules:
    paths       – Project directory constants (PROJECT_ROOT, DATA_DIR, etc.)
    permissions – Role / permission helpers (has_roles)
    bans        – Command ban system (is_user_banned, check_user_ban, etc.)
"""

from utils.paths import PROJECT_ROOT, DATA_DIR, CONFIG_DIR, DB_DIR
from utils.permissions import has_roles
