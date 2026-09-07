import os
import sqlite3
import json
import re
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = BOT_ROOT / "data"
DB_DIR = BOT_ROOT / "databases"
API_TRACKING_DIR = DB_DIR / "api_tracking"
POINTS_DB = DB_DIR / "esi_points.db"
USERNAME_MATCHES = DATA_DIR / "username_matches.json"
TRACKED_GUILD = DATA_DIR / "tracked_guild.json"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

def _get_latest_api_db() -> Path | None:
    if not API_TRACKING_DIR.exists():
        return None
    from utils.coverage_utils import is_coverage_db
    all_dbs = [p for p in API_TRACKING_DIR.rglob("*.db") if not is_coverage_db(p) and p.is_file()]
    if not all_dbs:
        return None
    return max(all_dbs, key=lambda p: p.stat().st_mtime)

def _fetch_uuid_online(username: str) -> tuple[str | None, str | None]:
    """Fetch UUID and canonical username from Wynncraft API or Mojang API."""
    import urllib.request
    import urllib.error
    # 1. Try Wynncraft API
    try:
        url = f"https://api.wynncraft.com/v3/player/{username}"
        req = urllib.request.Request(url, headers={"User-Agent": "ESI-Bot/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("uuid"), data.get("username")
    except Exception:
        pass

    # 2. Try Mojang API
    try:
        url = f"https://api.mojang.com/users/profiles/minecraft/{username}"
        req = urllib.request.Request(url, headers={"User-Agent": "ESI-Bot/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                raw_id = data.get("id")
                if raw_id and len(raw_id) == 32:
                    formatted_uuid = f"{raw_id[:8]}-{raw_id[8:12]}-{raw_id[12:16]}-{raw_id[16:20]}-{raw_id[20:]}"
                    return formatted_uuid, data.get("name")
    except Exception:
        pass

    return None, None

def resolve_player_identity(username_or_uuid: str) -> tuple[str | None, str, list[str]]:
    """
    Resolve any username (current or past) or UUID to:
    (canonical_uuid, current_username, all_known_aliases_lower)
    """
    raw = (username_or_uuid or "").strip()
    if not raw:
        return None, "", []

    raw_lower = raw.lower()
    is_uuid = bool(_UUID_RE.match(raw))
    resolved_uuid = raw if is_uuid else None
    resolved_name = raw
    aliases = {raw_lower}

    # 1. Check latest api_tracking DB, and recent historical snapshots if not found
    latest_db = _get_latest_api_db()
    if latest_db:
        try:
            conn = sqlite3.connect(latest_db)
            if is_uuid:
                row = conn.execute("SELECT uuid, username FROM player_stats WHERE LOWER(uuid) = LOWER(?) LIMIT 1", (raw,)).fetchone()
            else:
                row = conn.execute("SELECT uuid, username FROM player_stats WHERE LOWER(username) = LOWER(?) LIMIT 1", (raw,)).fetchone()
            conn.close()
            if row:
                if row[0]:
                    resolved_uuid = row[0]
                if row[1]:
                    resolved_name = row[1]
                    aliases.add(row[1].lower())
        except Exception:
            pass

    if not resolved_uuid and not is_uuid and API_TRACKING_DIR.exists():
        try:
            from utils.coverage_utils import is_coverage_db
            all_dbs = sorted([p for p in API_TRACKING_DIR.rglob("*.db") if not is_coverage_db(p) and p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True)
            for db_path in all_dbs[:50]:
                conn = sqlite3.connect(db_path)
                row = conn.execute("SELECT uuid, username FROM player_stats WHERE LOWER(username) = LOWER(?) LIMIT 1", (raw,)).fetchone()
                conn.close()
                if row and row[0]:
                    resolved_uuid = row[0]
                    break
        except Exception:
            pass

    # 2. Check tracked_guild.json
    if TRACKED_GUILD.exists():
        try:
            with open(TRACKED_GUILD, "r", encoding="utf-8") as f:
                tg = json.load(f)
            mh = tg.get("member_history", {})
            if resolved_uuid and resolved_uuid.lower() in mh:
                entry = mh[resolved_uuid.lower()]
                if entry.get("username"):
                    aliases.add(entry["username"].lower())
            elif not resolved_uuid:
                for uid_key, entry in mh.items():
                    if (entry.get("username") or "").lower() == raw_lower:
                        resolved_uuid = entry.get("uuid") or uid_key
                        if entry.get("username"):
                            aliases.add(entry["username"].lower())
                        break
        except Exception:
            pass

    # 3. Check username_matches.json
    if USERNAME_MATCHES.exists():
        try:
            with open(USERNAME_MATCHES, "r", encoding="utf-8") as f:
                matches = json.load(f)
            for did, entry in matches.items():
                if isinstance(entry, dict):
                    m_uuid = entry.get("uuid")
                    m_uname = entry.get("username")
                    if resolved_uuid and m_uuid and m_uuid.lower() == resolved_uuid.lower():
                        if m_uname:
                            aliases.add(m_uname.lower())
                    elif not resolved_uuid and m_uname and m_uname.lower() == raw_lower:
                        if m_uuid:
                            resolved_uuid = m_uuid
                        aliases.add(m_uname.lower())
        except Exception:
            pass

    # 4. Check esi_points.db
    if POINTS_DB.exists():
        try:
            conn = sqlite3.connect(POINTS_DB)
            if not resolved_uuid:
                row = conn.execute("SELECT uuid, username FROM esi_points WHERE LOWER(username) = LOWER(?) ORDER BY cycle_id DESC LIMIT 1", (raw,)).fetchone()
                if row:
                    resolved_uuid = row[0]
                    if row[1]:
                        aliases.add(row[1].lower())
            conn.close()
        except Exception:
            pass

    # 5. If UUID still not resolved, query online API (handles username changes)
    if not resolved_uuid and not is_uuid:
        online_uuid, online_name = _fetch_uuid_online(raw)
        if online_uuid:
            resolved_uuid = online_uuid
            if online_name:
                resolved_name = online_name
                aliases.add(online_name.lower())

    # 6. If UUID is resolved, expand aliases across all historical databases & files
    if resolved_uuid:
        # From esi_points.db
        if POINTS_DB.exists():
            try:
                conn = sqlite3.connect(POINTS_DB)
                rows = conn.execute("SELECT DISTINCT username FROM esi_points WHERE LOWER(uuid) = LOWER(?)", (resolved_uuid,)).fetchall()
                for r in rows:
                    if r and r[0]:
                        aliases.add(r[0].lower())
                conn.close()
            except Exception:
                pass

        # From tracked_guild.json
        if TRACKED_GUILD.exists():
            try:
                with open(TRACKED_GUILD, "r", encoding="utf-8") as f:
                    tg = json.load(f)
                mh = tg.get("member_history", {})
                if resolved_uuid.lower() in mh:
                    entry = mh[resolved_uuid.lower()]
                    if entry.get("username"):
                        aliases.add(entry["username"].lower())
            except Exception:
                pass

        # From username_matches.json
        if USERNAME_MATCHES.exists():
            try:
                with open(USERNAME_MATCHES, "r", encoding="utf-8") as f:
                    matches = json.load(f)
                for did, entry in matches.items():
                    if isinstance(entry, dict):
                        m_uuid = entry.get("uuid")
                        if m_uuid and m_uuid.lower() == resolved_uuid.lower():
                            m_uname = entry.get("username")
                            if m_uname:
                                aliases.add(m_uname.lower())
            except Exception:
                pass

        # From historical api_tracking snapshots
        if API_TRACKING_DIR.exists():
            try:
                latest_db = _get_latest_api_db()
                if latest_db:
                    conn = sqlite3.connect(latest_db)
                    rows = conn.execute("SELECT DISTINCT username FROM player_stats WHERE LOWER(uuid) = LOWER(?)", (resolved_uuid,)).fetchall()
                    for r in rows:
                        if r and r[0]:
                            aliases.add(r[0].lower())
                    conn.close()
            except Exception:
                pass

    return resolved_uuid, resolved_name, sorted(aliases)
