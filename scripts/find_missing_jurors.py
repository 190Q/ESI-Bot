#!/usr/bin/env python3
"""
Find guild members who hold one or more nobility ranks but do not have Juror.

Ranks checked:
  Viscount, Count, Duke, Grand Duke, Archduke

Usage:
  python scripts/find_missing_jurors.py
  python scripts/find_missing_jurors.py --json
  python scripts/find_missing_jurors.py --output missing_jurors.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Parliament guild (same as bot.py)
DEFAULT_GUILD_ID = 802999599060221992

RANK_ROLES = {
    591769392828776449: "Viscount",
    1391424890938195998: "Count",
    591765870272053261: "Duke",
    1396112289832243282: "Grand Duke",
    554514823191199747: "Archduke",
}

JUROR_ROLE_ID = 954566591520063510

# Display order (highest first)
RANK_ORDER = [
    "Archduke",
    "Grand Duke",
    "Duke",
    "Count",
    "Viscount",
]


def _load_token() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN not found in .env")
    return token


def _rank_sort_key(rank_names: list[str]) -> tuple:
    indices = [RANK_ORDER.index(name) for name in rank_names if name in RANK_ORDER]
    return (min(indices) if indices else len(RANK_ORDER), -len(rank_names))


async def find_missing_jurors(guild_id: int) -> list[dict]:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True

    client = discord.Client(intents=intents)
    results: list[dict] = []
    error: Exception | None = None

    @client.event
    async def on_ready() -> None:
        nonlocal error
        try:
            guild = client.get_guild(guild_id)
            if guild is None:
                guild = await client.fetch_guild(guild_id)

            print(f"Connected as {client.user}. Loading members for {guild.name} ({guild.id})...")
            # Ensure full member cache (requires Server Members Intent)
            if not guild.chunked:
                await guild.chunk()

            rank_role_ids = set(RANK_ROLES.keys())
            missing: list[dict] = []

            for member in guild.members:
                if member.bot:
                    continue

                member_role_ids = {role.id for role in member.roles}
                held_ranks = [
                    RANK_ROLES[role_id]
                    for role_id in rank_role_ids
                    if role_id in member_role_ids
                ]
                if not held_ranks:
                    continue
                if JUROR_ROLE_ID in member_role_ids:
                    continue

                held_ranks.sort(key=lambda name: RANK_ORDER.index(name) if name in RANK_ORDER else 99)
                missing.append(
                    {
                        "id": member.id,
                        "name": member.name,
                        "display_name": member.display_name,
                        "nick": member.nick,
                        "mention": member.mention,
                        "ranks": held_ranks,
                    }
                )

            missing.sort(key=lambda row: (_rank_sort_key(row["ranks"]), row["name"].lower()))
            results.extend(missing)
        except Exception as exc:  # noqa: BLE001 - surface any fetch/chunk failure cleanly
            error = exc
        finally:
            await client.close()

    token = _load_token()
    try:
        await client.start(token)
    except discord.LoginFailure:
        raise SystemExit("Failed to log in — check DISCORD_TOKEN.") from None

    if error is not None:
        raise error
    return results


def _print_human(rows: list[dict]) -> None:
    if not rows:
        print("No rank holders are missing the Juror role.")
        return

    print(f"Found {len(rows)} member(s) with rank role(s) but without Juror:\n")
    for row in rows:
        ranks = ", ".join(row["ranks"])
        label = row["display_name"]
        if row["name"] != row["display_name"]:
            label = f"{row['display_name']} ({row['name']})"
        print(f"- {label} | id={row['id']} | ranks={ranks}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List members who have Viscount/Count/Duke/Grand Duke/Archduke but not Juror.",
    )
    parser.add_argument(
        "--guild-id",
        type=int,
        default=DEFAULT_GUILD_ID,
        help=f"Discord guild ID (default: {DEFAULT_GUILD_ID})",
    )
    parser.add_argument("--json", action="store_true", help="Print results as JSON.")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional path to also write the results (text or JSON matching --json).",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        rows = asyncio.run(find_missing_jurors(args.guild_id))
    except discord.Forbidden:
        print(
            "Missing access or privileged intent. "
            "Enable Server Members Intent for the bot in the Discord Developer Portal.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = json.dumps(rows, indent=2, ensure_ascii=False)
        print(payload)
        body = payload
    else:
        _print_human(rows)
        lines = []
        for row in rows:
            ranks = ", ".join(row["ranks"])
            label = row["display_name"]
            if row["name"] != row["display_name"]:
                label = f"{row['display_name']} ({row['name']})"
            lines.append(f"{label} | id={row['id']} | ranks={ranks}")
        body = "\n".join(lines) + ("\n" if lines else "")

    if args.output:
        out_path = Path(args.output).expanduser().resolve()
        out_path.write_text(body if body.endswith("\n") or not body else body + "\n", encoding="utf-8")
        print(f"\nWrote {len(rows)} result(s) to {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
