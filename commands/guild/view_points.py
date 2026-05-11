import discord
from discord import app_commands
from datetime import datetime, timezone
import sqlite3
import io
import os
from pathlib import Path

from utils.esi_points import (
    get_cycle_id,
    get_cycle_bounds,
    POINTS_DB,
    _player_points_table,
    is_dirty_reason,
)

# Paths - mirror what api_tracker.py uses
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DB_FOLDER = BASE_DIR / "databases"
API_TRACKING_FOLDER = DB_FOLDER / "api_tracking"

# HR guild ranks
HR_RANKS = {"strategist", "chief", "owner"}

#  Helpers
def _get_guild_ranks() -> dict[str, str]:
    """Return {lowercased_username: lowercased_guild_rank} from the latest API DB."""
    db = _get_latest_api_db()
    if db is None:
        return {}
    try:
        conn = sqlite3.connect(db)
        c = conn.cursor()
        c.execute("SELECT username, guild_rank FROM player_stats WHERE guild_rank IS NOT NULL")
        result = {row[0].lower(): (row[1] or "").lower() for row in c.fetchall()}
        conn.close()
        return result
    except Exception:
        return {}


def _calc_le(username: str, total_points: int, history: list[dict], guild_ranks: dict) -> float:
    """
    Calculate LE for a player (legacy runtime fallback).
    - HR players (strategist/chief/owner): dirty reasons don't count toward LE.
    - Everyone else: all points count (10 pts = 1 LE).
    """
    rank = guild_ranks.get(username.lower(), "")
    if rank in HR_RANKS:
        real_points = sum(r["points_gained"] for r in history if not is_dirty_reason(r["reason"]))
        return real_points / 10
    return total_points / 10


def _get_clean_dirty_for_cycles(uuid: str, cycle_ids: list[int],
                                 history: list[dict] | None = None,
                                 guild_ranks: dict | None = None,
                                 username: str | None = None) -> dict[int, tuple[int, int]]:
    """
    Return {cycle_id: (clean_ep, dirty_ep)} from persisted data.
    Falls back to runtime calculation if persisted columns are both 0
    and the record likely predates the migration.
    """
    conn = sqlite3.connect(POINTS_DB)
    c = conn.cursor()
    placeholders = ",".join("?" * len(cycle_ids))
    c.execute(
        f"SELECT cycle_id, clean_ep, dirty_ep, points FROM esi_points "
        f"WHERE uuid = ? AND cycle_id IN ({placeholders})",
        [uuid] + cycle_ids,
    )
    rows = {r[0]: (r[1], r[2], r[3]) for r in c.fetchall()}
    conn.close()

    result: dict[int, tuple[int, int]] = {}
    for cid in cycle_ids:
        clean, dirty, points = rows.get(cid, (0, 0, 0))
        # Fallback: if both persisted values are 0 but points > 0, use runtime calc
        if clean == 0 and dirty == 0 and points > 0 and history is not None and guild_ranks is not None and username:
            rank = guild_ranks.get(username.lower(), "")
            cycle_hist = [r for r in history if r["cycle_id"] == cid]
            if rank in HR_RANKS:
                dirty = sum(r["points_gained"] for r in cycle_hist if is_dirty_reason(r["reason"]))
                clean = points - dirty
            else:
                clean = points
                dirty = 0
        result[cid] = (clean, dirty)
    return result


def _get_active_reservations(uuid: str) -> int:
    """Return total EP currently reserved (unreleased) for a player."""
    conn = sqlite3.connect(POINTS_DB)
    c = conn.cursor()
    try:
        c.execute(
            "SELECT COALESCE(SUM(reserved_amount), 0) FROM ep_reservations "
            "WHERE uuid = ? AND released_at IS NULL",
            (uuid,),
        )
        total = c.fetchone()[0]
    except sqlite3.OperationalError:
        total = 0
    conn.close()
    return total


def _get_latest_api_db() -> Path | None:
    """Return the most-recently-modified .db file across all api_tracking day folders."""
    if not API_TRACKING_FOLDER.exists():
        return None
    all_dbs = list(API_TRACKING_FOLDER.rglob("*.db"))
    if not all_dbs:
        return None
    return max(all_dbs, key=lambda p: p.stat().st_mtime)


def _get_guild_usernames() -> set[str]:
    """
    Return a set of lowercased usernames currently in the guild,
    sourced from the latest api_tracking database.
    """
    db = _get_latest_api_db()
    if db is None:
        return set()
    try:
        conn = sqlite3.connect(db)
        c = conn.cursor()
        c.execute("SELECT username FROM player_stats")
        names = {row[0].lower() for row in c.fetchall() if row[0]}
        conn.close()
        return names
    except Exception:
        return set()


def _get_points_for_cycles(cycle_ids: list[int]) -> list[dict]:
    """
    Return a list of dicts {uuid, username, points, clean_ep, dirty_ep}
    summed across the given cycle_ids, restricted to players currently in the guild.
    """
    guild_names = _get_guild_usernames()

    conn = sqlite3.connect(POINTS_DB)
    c = conn.cursor()

    placeholders = ",".join("?" * len(cycle_ids))
    c.execute(
        f"SELECT uuid, username, SUM(points), SUM(clean_ep), SUM(dirty_ep) "
        f"FROM esi_points WHERE cycle_id IN ({placeholders}) GROUP BY uuid",
        cycle_ids,
    )
    rows = c.fetchall()
    conn.close()

    results = []
    for uuid, username, pts, clean, dirty in rows:
        if guild_names and username.lower() not in guild_names:
            continue
        results.append({
            "uuid": uuid,
            "username": username,
            "points": pts or 0,
            "clean_ep": clean or 0,
            "dirty_ep": dirty or 0,
        })

    results.sort(key=lambda x: x["points"], reverse=True)
    return results


def _get_player_history(uuid: str) -> list[dict]:
    """Return every history record for a player, newest first."""
    table = _player_points_table(uuid)
    conn = sqlite3.connect(POINTS_DB)
    c = conn.cursor()
    try:
        c.execute(
            f'SELECT record_id, username, points_gained, cycle_id, reason, timestamp, '
            f'COALESCE(is_dirty, 0) '
            f'FROM "{table}" ORDER BY timestamp DESC'
        )
        rows = c.fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()

    return [
        {
            "record_id": r[0],
            "username": r[1],
            "points_gained": r[2],
            "cycle_id": r[3],
            "reason": r[4],
            "timestamp": r[5],
            "is_dirty": r[6],
        }
        for r in rows
    ]


def _cycle_label(cycle_id: int) -> str:
    start, end = get_cycle_bounds(cycle_id)
    return f"Cycle {cycle_id} ({start.strftime('%d %b')} – {end.strftime('%d %b %Y')})"


#  TXT builders
def _(players: list[dict], cycle_ids: list[int]) -> str:
    lines = []
    lines.append("ESI Points – Full Leaderboard")
    lines.append("=" * 50)
    lines.append("Cycles: " + ", ".join(_cycle_label(c) for c in cycle_ids))
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Total players: {len(players)}")
    lines.append("")
    lines.append(f"{'Rank':<6} {'Username':<24} {'Points':>8}")
    lines.append("-" * 42)

    for i, p in enumerate(players, 1):
        lines.append(f"{i:<6} {p['username']:<24} {p['points']:>8}")

    return "\n".join(lines)


def _build_player_history_txt(username: str, uuid: str, points_by_cycle: dict,
                               history: list[dict], guild_ranks: dict,
                               clean_dirty: dict[int, tuple[int, int]] | None = None) -> str:
    lines = []
    lines.append(f"ESI Points – History for {username}")
    lines.append("=" * 50)
    lines.append(f"UUID: {uuid}")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")

    lines.append("Points per cycle:")
    for cycle_id, pts in sorted(points_by_cycle.items()):
        if clean_dirty and cycle_id in clean_dirty:
            c_ep, d_ep = clean_dirty[cycle_id]
            lines.append(f"  {_cycle_label(cycle_id)}: {c_ep} CEP / {d_ep} DEP")
        else:
            lines.append(f"  {_cycle_label(cycle_id)}: {pts} pts")
    lines.append("")

    lines.append("Full history (newest first):")
    lines.append(f"{'Timestamp':<28} {'Cycle':<8} {'Reason':<20} {'Points':>7}  {'Dirty':>5}")
    lines.append("-" * 76)
    for r in history:
        ts = r["timestamp"][:19].replace("T", " ")
        dirty_flag = "yes" if r.get("is_dirty") else ""
        lines.append(f"{ts:<28} {r['cycle_id']:<8} {r['reason']:<20} {r['points_gained']:>+7}  {dirty_flag:>5}")

    if not history:
        lines.append("  (no records found)")

    return "\n".join(lines)


def _build_leaderboard_txt(players: list[dict], cycle_ids: list[int], guild_ranks: dict) -> str:
    lines = []
    lines.append("ESI Points – Full Leaderboard")
    lines.append("=" * 78)
    lines.append("Cycles: " + ", ".join(_cycle_label(c) for c in cycle_ids))
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Total players: {len(players)}")
    lines.append("")
    lines.append(f"{'Rank':<6} {'Username':<24} {'Points':>8}  {'CEP':>8}  {'DEP':>8}")
    lines.append("-" * 62)

    for i, p in enumerate(players, 1):
        h = _get_player_history(p["uuid"])
        clean = p.get("clean_ep", 0)
        dirty = p.get("dirty_ep", 0)
        # Fallback if persisted values are 0 but points > 0
        if clean == 0 and dirty == 0 and p["points"] > 0:
            rank = guild_ranks.get(p["username"].lower(), "")
            if rank in HR_RANKS:
                dirty = sum(r["points_gained"] for r in h if is_dirty_reason(r["reason"]))
                clean = p["points"] - dirty
            else:
                clean = p["points"]
        lines.append(f"{i:<6} {p['username']:<24} {p['points']:>8}  {clean:>8}  {dirty:>8}")

    return "\n".join(lines)


#  Setup
def setup(bot, has_required_role, config):
    """Setup function for bot integration."""

    cycle_choices = [
        app_commands.Choice(name="Current cycle", value="current"),
        app_commands.Choice(name="Previous cycle", value="previous"),
        app_commands.Choice(name="Both cycles",   value="both"),
    ]

    @bot.tree.command(
        name="view_points",
        description="View the ESI points leaderboard or a specific player's points.",
    )
    @app_commands.describe(
        cycle="Which cycle(s) to display",
        username="(Optional) Show details for a specific player only",
    )
    @app_commands.choices(cycle=cycle_choices)
    async def view_points(
        interaction: discord.Interaction,
        cycle: app_commands.Choice[str],
        username: str = None,
    ):
        await interaction.response.defer()

        current_cycle = get_cycle_id()
        previous_cycle = current_cycle - 1

        if cycle.value == "current":
            cycle_ids = [current_cycle]
        elif cycle.value == "previous":
            cycle_ids = [previous_cycle]
        else:
            cycle_ids = [previous_cycle, current_cycle]

        cycle_title = " + ".join(_cycle_label(c) for c in cycle_ids)

        # Single-player view────────────────────────────────────────
        if username:
            # Look up UUID by username (case-insensitive) from points DB
            conn = sqlite3.connect(POINTS_DB)
            c = conn.cursor()
            c.execute(
                "SELECT uuid, username FROM esi_points WHERE LOWER(username) = LOWER(?) LIMIT 1",
                (username,),
            )
            row = c.fetchone()
            conn.close()

            if not row:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="Player Not Found",
                        description=f"No points records found for **{username}**.",
                        color=0xFF4444,
                    ),
                    ephemeral=True,
                )
                return

            uuid, resolved_name = row

            # Points per requested cycle
            conn = sqlite3.connect(POINTS_DB)
            c = conn.cursor()
            placeholders = ",".join("?" * len(cycle_ids))
            c.execute(
                f"SELECT cycle_id, points FROM esi_points WHERE uuid = ? AND cycle_id IN ({placeholders})",
                [uuid] + cycle_ids,
            )
            cycle_rows = {r[0]: r[1] for r in c.fetchall()}
            conn.close()

            total_pts = sum(cycle_rows.values())

            # Rank among guild players
            all_players = _get_points_for_cycles(cycle_ids)
            rank = next((i + 1 for i, p in enumerate(all_players) if p["uuid"] == uuid), None)

            # Full history
            history = _get_player_history(uuid)

            guild_ranks = _get_guild_ranks()

            # Embed
            embed = discord.Embed(
                title=f"Points for {resolved_name}",
                description=cycle_title,
                color=0x5865F2,
            )

            # Fetch persisted clean/dirty split
            clean_dirty = _get_clean_dirty_for_cycles(
                uuid, cycle_ids, history=history,
                guild_ranks=guild_ranks, username=resolved_name,
            )

            for cid in cycle_ids:
                pts = cycle_rows.get(cid, 0)
                c_ep, d_ep = clean_dirty.get(cid, (pts, 0))
                start, end = get_cycle_bounds(cid)
                field_name = f"Cycle {cid} ({start.strftime('%d %b')} – {end.strftime('%d %b')})"
                embed.add_field(
                    name=field_name,
                    value=f"**{c_ep}** CEP / **{d_ep}** DEP",
                    inline=True,
                )

            if len(cycle_ids) > 1:
                total_clean = sum(v[0] for v in clean_dirty.values())
                total_dirty = sum(v[1] for v in clean_dirty.values())
                embed.add_field(
                    name="Combined Total",
                    value=f"**{total_clean}** CEP / **{total_dirty}** DEP",
                    inline=True,
                )

            if rank:
                embed.add_field(name="Guild Rank", value=f"**#{rank}** of {len(all_players)}", inline=True)

            # Show active EP reservations if any
            reserved = _get_active_reservations(uuid)
            if reserved > 0:
                embed.add_field(
                    name="Reserved EP",
                    value=f"({reserved} EP reserved — auction pending)",
                    inline=False,
                )

            # Last 5 history entries as a quick preview
            if history:
                preview_lines = []
                for r in history[:5]:
                    ts = r["timestamp"][:10]
                    preview_lines.append(f"`{ts}` **{r['reason']}** → +{r['points_gained']} pts (Cycle {r['cycle_id']})")
                embed.add_field(
                    name="Recent Activity",
                    value="\n".join(preview_lines),
                    inline=False,
                )

            embed.set_footer(text="Full history attached below")

            # TXT attachment
            txt_content = _build_player_history_txt(
                resolved_name, uuid, cycle_rows, history, guild_ranks,
                clean_dirty=clean_dirty,
            )
            txt_file = discord.File(
                fp=io.BytesIO(txt_content.encode("utf-8")),
                filename=f"points_{resolved_name}_{cycle.value}.txt",
            )

            await interaction.followup.send(embed=embed, file=txt_file)
            return

        # Leaderboard view
        players = _get_points_for_cycles(cycle_ids)

        if not players:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="No Data",
                    description=f"No points records found for {cycle_title}.",
                    color=0xFF4444,
                ),
                ephemeral=True,
            )
            return

        top10 = players[:10]

        embed = discord.Embed(
            title="ESI Points Leaderboard",
            description=cycle_title,
            color=0xFFD700,
        )

        guild_ranks = _get_guild_ranks()
        # Pre-fetch full history for top10 only (needed for HR LE calc)
        top10_history = {p["uuid"]: _get_player_history(p["uuid"]) for p in top10}

        board_lines = []
        for i, p in enumerate(top10, 1):
            h = top10_history[p["uuid"]]
            c_ep = p.get("clean_ep", 0)
            d_ep = p.get("dirty_ep", 0)
            if c_ep == 0 and d_ep == 0 and p["points"] > 0:
                rank_str = guild_ranks.get(p["username"].lower(), "")
                if rank_str in HR_RANKS:
                    d_ep = sum(r["points_gained"] for r in h if is_dirty_reason(r["reason"]))
                    c_ep = p["points"] - d_ep
                else:
                    c_ep = p["points"]
            board_lines.append(f"#{i} **{p['username']}** — {c_ep} CEP / {d_ep} DEP")

        embed.add_field(name="Top 10", value="\n".join(board_lines), inline=False)
        embed.set_footer(text=f"Full leaderboard ({len(players)} players) attached below")

        # TXT attachment
        txt_content = _build_leaderboard_txt(players, cycle_ids, guild_ranks)
        txt_file = discord.File(
            fp=io.BytesIO(txt_content.encode("utf-8")),
            filename=f"leaderboard_{cycle.value}.txt",
        )

        await interaction.followup.send(embed=embed, file=txt_file)

    print("[OK] Loaded view_points command")