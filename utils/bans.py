"""
Command ban system.

Provides functions to check, add, and remove per-command bans for users.
Ban data is stored in data/user_bans.json.
"""

import discord
from datetime import datetime, timezone
import json

from utils.paths import DATA_DIR

BAN_DB_PATH = DATA_DIR / "user_bans.json"


def load_bans() -> dict:
    """Load bans database from JSON file."""
    try:
        if BAN_DB_PATH.exists():
            with open(BAN_DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[WARN] Failed to load bans: {e}")
    return {}


def save_bans(data: dict):
    """Save bans database to JSON file."""
    try:
        with open(BAN_DB_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Failed to save bans: {e}")


def remove_ban(user_id: int):
    """Remove a user's ban."""
    bans = load_bans()
    user_id_str = str(user_id)

    if user_id_str in bans:
        bans.pop(user_id_str)
        save_bans(bans)


def is_user_banned(user_id: int, command_name: str) -> dict:
    """
    Check if user is banned from a specific command.
    Returns ban info dict if banned, None otherwise.
    """
    bans = load_bans()
    user_id_str = str(user_id)

    if user_id_str not in bans:
        return None

    ban_entry = bans[user_id_str]

    if command_name not in ban_entry["banned_commands"] and "All" not in ban_entry["banned_commands"]:
        return None

    return ban_entry


async def check_user_ban(interaction: discord.Interaction, command_name: str) -> bool:
    """
    Check if a user is banned and send the appropriate message.

    Returns True if banned (caller should stop), False otherwise.
    """
    ban_info = is_user_banned(interaction.user.id, command_name)

    if not ban_info:
        return False

    reason = ban_info.get("reason", "")
    ban_message = "You are **permanently banned** from using this command."

    if reason:
        ban_message += f"\n**Reason:** {reason}"

    ban_message += "\n\nIf you think this is a mistake, please contact a staff member or use `/contact_support` to get in touch with the bot owner."

    embed = discord.Embed(
        title="\U0001f6ab Command Banned",
        description=ban_message,
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )

    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        print(f"[BAN_CHECK] Failed to send ban message: {e}")

    return True
