#!/usr/bin/env python3
"""
Back up every role in a Discord server, with the complete role object the API
returns.

Each role is stored twice in the backup: a decoded block (hex/RGB colours,
gradient stops, permission names, icon URL) and the untouched API payload under
"api", so nothing the API exposes is lost even if the decoded view changes.

Examples:
  # Back up every role into exports/roles (auto-detects the guild if the bot is
  # only in one server)
  python scripts/backup_roles.py

  # List the servers this token can see, then exit
  python scripts/backup_roles.py --list-guilds

  # Back up a specific server
  python scripts/backup_roles.py --guild-id 123456789012345678

  # Just list the roles and exit, without writing a file
  python scripts/backup_roles.py --list

  # Include how many members currently hold each role
  python scripts/backup_roles.py --member-counts

  # Write to a specific file and refresh exports/roles/latest.json
  python scripts/backup_roles.py --output exports/roles/backup.json --latest

Notes:
  - Requires DISCORD_TOKEN in the project .env (same token as the bot).
  - The token's bot only needs the Server Members Intent if --member-counts is
    used; the role list itself needs no privileged intent.
  - --guild-id is optional. Without it the script uses the only server the token
    is in, and errors out (listing the servers) when there is more than one.
  - Role colours come from the newer "colors" object (primary, secondary,
    tertiary). Gradient and holographic roles fill the secondary/tertiary slots,
    which is what the decoded "gradient" block reports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "exports" / "roles"

ROLE_ICON_URL = "https://cdn.discordapp.com/role-icons/{role_id}/{icon}.png"


def _load_token() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN not found in .env")
    return token


def _hex(value: int) -> str:
    return f"#{value & 0xFFFFFF:06X}"


def _rgb(value: int) -> List[int]:
    return [(value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF]


def _decode_colour(value: Optional[int]) -> Optional[Dict[str, Any]]:
    """Turn a raw colour int into a hex/RGB block. None means the slot is empty."""
    if value is None:
        return None
    return {"value": value, "hex": _hex(value), "rgb": _rgb(value)}


def _decode_flags(value: int) -> Dict[str, Any]:
    names: List[str] = []
    try:
        names = [name for name, enabled in discord.RoleFlags._from_value(value) if enabled]
    except Exception:
        # Unknown flag bits fall back to the raw value only.
        pass
    return {"value": value, "names": names}


def _decode_permissions(value: int) -> Dict[str, Any]:
    permissions = discord.Permissions(value)
    enabled = [name for name, granted in permissions if granted]
    return {
        "value": str(value),
        "enabled": enabled,
        "all": {name: granted for name, granted in permissions},
    }


def _decode_icon(role_id: int, icon_hash: Optional[str]) -> Optional[Dict[str, Any]]:
    if not icon_hash:
        return None
    return {"hash": icon_hash, "url": ROLE_ICON_URL.format(role_id=role_id, icon=icon_hash)}


def _decode_tags(tags: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    tags = tags or {}
    return {
        "bot_id": tags.get("bot_id"),
        "integration_id": tags.get("integration_id"),
        "premium_subscriber": "premium_subscriber" in tags,
        "available_for_purchase": "available_for_purchase" in tags,
        "guild_connections": "guild_connections" in tags,
        "raw": tags,
    }


def _decode_colors(colors: Dict[str, Any]) -> Dict[str, Any]:
    """Decode the API 'colors' object, including the gradient view."""
    primary = _decode_colour(colors.get("primary_color"))
    secondary = _decode_colour(colors.get("secondary_color"))
    tertiary = _decode_colour(colors.get("tertiary_color"))

    stops = [colour["hex"] for colour in (primary, secondary, tertiary) if colour]
    return {
        "primary": primary,
        "secondary": secondary,
        "tertiary": tertiary,
        "gradient": {
            "is_gradient": secondary is not None or tertiary is not None,
            "stops": stops,
        },
    }


def _decode_role(
    payload: Dict[str, Any],
    *,
    member_count: Optional[int],
    include_raw: bool,
) -> Dict[str, Any]:
    role_id = int(payload["id"])
    colours = payload.get("colors") or {}

    role: Dict[str, Any] = {
        "id": role_id,
        "name": payload.get("name"),
        "position": payload.get("position"),
        "hoist": payload.get("hoist"),
        "managed": payload.get("managed"),
        "mentionable": payload.get("mentionable"),
        "unicode_emoji": payload.get("unicode_emoji"),
        "icon": _decode_icon(role_id, payload.get("icon")),
        "flags": _decode_flags(payload.get("flags", 0)),
        "colors": _decode_colors(colours),
        "permissions": _decode_permissions(int(payload.get("permissions", 0))),
        "tags": _decode_tags(payload.get("tags")),
        "member_count": member_count,
        "created_at": discord.utils.snowflake_time(role_id).isoformat(),
    }

    if include_raw:
        role["api"] = payload
    return role


def _print_guilds(client: discord.Client) -> None:
    guilds = sorted(client.guilds, key=lambda item: item.name.lower())
    if not guilds:
        print("This token is not in any server. Check DISCORD_TOKEN in .env.", file=sys.stderr)
        return
    print(f"This token can see {len(guilds)} server(s):", file=sys.stderr)
    for guild in guilds:
        print(f"  {guild.id}  {guild.name}", file=sys.stderr)


async def _resolve_guild(client: discord.Client, guild_id: Optional[int]) -> discord.Guild:
    if guild_id is None:
        guilds = list(client.guilds)
        if len(guilds) == 1:
            return guilds[0]
        if not guilds:
            raise SystemExit("This token is not in any server. Check DISCORD_TOKEN in .env.")
        print("Pass --guild-id to pick a server.", file=sys.stderr)
        _print_guilds(client)
        raise SystemExit(1)

    guild = client.get_guild(guild_id)
    if guild is not None:
        return guild
    try:
        return await client.fetch_guild(guild_id)
    except discord.NotFound:
        print(f"Guild {guild_id} not found, or the bot is not a member.", file=sys.stderr)
        _print_guilds(client)
        raise SystemExit(1) from None


async def _process(client: discord.Client, args: argparse.Namespace) -> int:
    if args.list_guilds:
        _print_guilds(client)
        return 0

    guild = await _resolve_guild(client, args.guild_id)

    # Raw payloads, straight from the API, so nothing gets lost in parsing.
    payloads: List[Dict[str, Any]] = await guild._state.http.get_roles(guild.id)

    member_counts: Dict[str, int] = {}
    if args.member_counts:
        member_counts = await guild._state.http.get_role_member_counts(guild.id)

    if args.list:
        print(f"Roles in {guild.name} ({guild.id}): {len(payloads)}\n")
        for payload in sorted(payloads, key=lambda item: item.get("position", 0), reverse=True):
            colours = payload.get("colors") or {}
            colour = _decode_colour(colours.get("primary_color"))
            gradient = " gradient" if colours.get("secondary_color") is not None else ""
            print(
                f"  {payload['id']}  pos={payload.get('position'):<4}"
                f"  {colour['hex'] if colour else '#000000'}{gradient}  {payload.get('name')}"
            )
        return 0

    roles = [
        _decode_role(
            payload,
            member_count=member_counts.get(payload["id"]) if member_counts else None,
            include_raw=not args.no_raw,
        )
        for payload in payloads
    ]
    roles.sort(key=lambda item: item["position"], reverse=True)

    backup = {
        "guild_id": guild.id,
        "guild_name": guild.name,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "role_count": len(roles),
        "includes_member_counts": bool(args.member_counts),
        "includes_raw_api_payload": not args.no_raw,
        "roles": roles,
    }

    if args.output:
        output_path = Path(args.output)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = Path(args.output_dir) / f"roles_{guild.id}_{stamp}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(backup, ensure_ascii=False, indent=args.indent) + "\n",
        encoding="utf-8",
    )
    print(f"[OK] Backed up {len(roles)} role(s) to {output_path}")

    if args.latest:
        latest_path = output_path.parent / "latest.json"
        latest_path.write_text(
            json.dumps(backup, ensure_ascii=False, indent=args.indent) + "\n",
            encoding="utf-8",
        )
        print(f"[OK] Latest copy: {latest_path}")

    return 0


async def _run(args: argparse.Namespace) -> int:
    intents = discord.Intents.none()
    intents.guilds = True

    client = discord.Client(intents=intents)
    exit_code = 0
    failure: Optional[BaseException] = None

    @client.event
    async def on_ready() -> None:
        nonlocal exit_code, failure
        try:
            exit_code = await _process(client, args)
        except BaseException as exc:
            failure = exc
        finally:
            await client.close()

    try:
        await client.start(_load_token())
    except discord.LoginFailure:
        print("Failed to log in - check DISCORD_TOKEN in .env.", file=sys.stderr)
        return 1

    if failure is not None:
        raise failure
    return exit_code


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Back up every role in a Discord server with all API fields.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python scripts/backup_roles.py\n"
            "  python scripts/backup_roles.py --list-guilds\n"
            "  python scripts/backup_roles.py --list\n"
            "  python scripts/backup_roles.py --member-counts --latest\n"
            "  python scripts/backup_roles.py --guild-id 123456789012345678\n"
        ),
    )
    parser.add_argument(
        "--guild-id",
        type=int,
        default=None,
        help="Discord server (guild) ID. Defaults to the only server the bot is in.",
    )
    parser.add_argument(
        "--list-guilds",
        action="store_true",
        help="Print the servers this token can see, then exit.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Exact file to write. Defaults to a timestamped file in --output-dir.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Directory for the timestamped backup (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Also write (or refresh) latest.json next to the backup file.",
    )
    parser.add_argument(
        "--member-counts",
        action="store_true",
        help="Include the number of members holding each role (needs one extra request).",
    )
    parser.add_argument(
        "--no-raw",
        action="store_true",
        help="Drop the untouched API payload from each role (smaller file).",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation (default: 2). Use 0 for compact output.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the roles and exit without writing a backup.",
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = _build_parser().parse_args()

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nCancelled by user.", file=sys.stderr)
        return 130
    except discord.Forbidden as exc:
        print(f"Missing access: {exc}", file=sys.stderr)
        return 1
    except discord.HTTPException as exc:
        print(f"Discord API error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
