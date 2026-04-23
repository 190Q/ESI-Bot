import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timezone

API_URL = "https://api.wynncraft.com/v3/guild/prefix/ESI"
DB_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "databases", "api_tracking")


def fetch_guild_data():
    """Fetch guild JSON from the Wynncraft API."""
    req = urllib.request.Request(API_URL, headers={"User-Agent": "ESI-Bot"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def get_sorted_api_folders():
    """Return a list of (datetime_date, folder_path) sorted chronologically."""
    folders = []
    for name in os.listdir(DB_ROOT):
        if not name.startswith("api_") or not os.path.isdir(os.path.join(DB_ROOT, name)):
            continue
        date_part = name[4:]
        try:
            d = datetime.strptime(date_part, "%d-%m-%Y").date()
        except ValueError:
            continue
        folders.append((d, os.path.join(DB_ROOT, name)))
    folders.sort(key=lambda x: x[0])
    return folders


def find_closest_folders(folders, target_date, max_results=5):
    """Return up to *max_results* folders sorted by proximity to *target_date*."""
    ranked = sorted(folders, key=lambda x: abs((x[0] - target_date).days))
    return ranked[:max_results]


def pick_db_file(folder_path):
    """Return the path to the first .db file in *folder_path*."""
    for f in sorted(os.listdir(folder_path)):
        if f.endswith(".db"):
            return os.path.join(folder_path, f)
    return None


def query_player_stats(db_path, username):
    """Return (wars,) for *username*, or None."""
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='player_stats'")
        if not c.fetchone():
            conn.close()
            return None
        c.execute(
            "SELECT wars FROM player_stats WHERE username = ?",
            (username,),
        )
        row = c.fetchone()
        conn.close()
        return row
    except sqlite3.Error:
        return None


def query_guild_raid_stats(db_path, username):
    """Return (total_graids, graids_list_json) for *username*, or None if table/row missing."""
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='guild_raid_stats'")
        if not c.fetchone():
            conn.close()
            return None
        c.execute(
            "SELECT total_graids, canyon_colossus, orphions_nexus, grootslangs, nameless_anomaly "
            "FROM guild_raid_stats WHERE username = ?",
            (username,),
        )
        row = c.fetchone()
        conn.close()
        if row is None:
            return None
        total_graids, canyon, orphion, groot, nameless = row
        graids_list = {
            "The Canyon Colossus": canyon or 0,
            "Orphion's Nexus of Light": orphion or 0,
            "Nest of the Grootslangs": groot or 0,
            "The Nameless Anomaly": nameless or 0,
        }
        return (total_graids or 0, json.dumps(graids_list))
    except sqlite3.Error:
        return None


def parse_raids_list(raw):
    """Parse the raids_list JSON string into a dict."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def main():
    print("Fetching guild data from API...")
    guild = fetch_guild_data()
    recruiters = guild["members"]["recruiter"]

    folders = get_sorted_api_folders()
    if not folders:
        print("ERROR: No api_* database folders found.")
        return

    latest_folder = folders[-1]
    latest_db = pick_db_file(latest_folder[1])
    if not latest_db:
        print(f"ERROR: No .db file in latest folder {latest_folder[1]}")
        return
    print(f"Latest DB snapshot: {os.path.basename(latest_db)}  ({latest_folder[0]})\n")

    results = []

    for name, info in recruiters.items():
        joined_str = info["joined"]
        joined_dt = datetime.fromisoformat(joined_str.replace("Z", "+00:00"))
        joined_date = joined_dt.date()

        # Use the actual guild join date as the reference point
        reference_date = joined_date

        candidates = find_closest_folders(folders, reference_date)
        if not candidates:
            results.append((name, joined_date, None, "No matching DB folder"))
            continue

        join_stats = None
        join_graid_stats = None
        join_db = None
        for _, cand_path in candidates:
            db = pick_db_file(cand_path)
            if db is None:
                continue
            stats = query_player_stats(db, name)
            if stats is not None:
                join_stats = stats
                join_graid_stats = query_guild_raid_stats(db, name)
                join_db = db
                break

        if join_stats is None:
            results.append((name, joined_date, None,
                            "Player not found in any nearby DB snapshot"))
            continue

        latest_stats = query_player_stats(latest_db, name)
        if latest_stats is None:
            results.append((name, joined_date, None,
                            f"Player not found in latest DB ({os.path.basename(latest_db)})"))
            continue

        latest_graid_stats = query_guild_raid_stats(latest_db, name)
        if latest_graid_stats is None:
            results.append((name, joined_date, None,
                            f"Guild raid data not found in latest DB ({os.path.basename(latest_db)})"))
            continue

        (join_wars,) = join_stats
        (latest_wars,) = latest_stats

        # Use 0 baseline if the reference snapshot predates guild_raid_stats tracking
        join_graids, join_graids_json = join_graid_stats if join_graid_stats else (0, "{}")
        latest_graids, latest_graids_json = latest_graid_stats

        delta_wars = latest_wars - join_wars
        delta_graids = latest_graids - join_graids

        join_graids_dict = parse_raids_list(join_graids_json)
        latest_graids_dict = parse_raids_list(latest_graids_json)

        all_graid_names = sorted(
            set(list(join_graids_dict.keys()) + list(latest_graids_dict.keys()))
        )
        graid_deltas = {}
        for rn in all_graid_names:
            graid_deltas[rn] = latest_graids_dict.get(rn, 0) - join_graids_dict.get(rn, 0)

        results.append((name, joined_date, {
            "join_db": os.path.basename(join_db),
            "reference_date": reference_date,
            "join_wars": join_wars,
            "join_graids": join_graids,
            "latest_wars": latest_wars,
            "latest_graids": latest_graids,
            "delta_wars": delta_wars,
            "delta_graids": delta_graids,
            "graid_deltas": graid_deltas,
        }, None))

    WAR_THRESHOLD = 50
    RAID_THRESHOLD = 25

    war_players = []
    raid_players = []

    for name, joined_date, data, error in sorted(results, key=lambda r: r[1]):
        if error or data is None:
            continue
        if data["delta_wars"] >= WAR_THRESHOLD:
            war_players.append((name, joined_date, data))
        if data["delta_graids"] >= RAID_THRESHOLD:
            raid_players.append((name, joined_date, data))

    lines = []
    lines.append(f"ESI Recruiter Delta Report  -  since join date  -  generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Latest DB snapshot: {os.path.basename(latest_db)}  ({latest_folder[0]})")

    lines.append("")
    lines.append(f"=== WARS ({WAR_THRESHOLD}+ delta) === ({len(war_players)} players)")
    lines.append(f"{'Player':<22} {'Joined':<12} {'D Wars':>8}")
    lines.append("=" * 44)
    for name, joined_date, data in war_players:
        lines.append(f"{name:<22} {str(data['reference_date']):<12} {data['delta_wars']:>+8d}")

    if not war_players:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"=== GUILD RAIDS ({RAID_THRESHOLD}+ delta) === ({len(raid_players)} players)")
    lines.append(f"{'Player':<22} {'Joined':<12} {'D Raids':>9}")
    lines.append("=" * 45)
    for name, joined_date, data in raid_players:
        lines.append(f"{name:<22} {str(data['reference_date']):<12} {data['delta_graids']:>+9d}")

    if not raid_players:
        lines.append("  (none)")

    output_text = "\n".join(lines)
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "recruiter_delta.txt")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(output_text)

    print(output_text)
    print(f"\nSaved to {output_path}")


main()