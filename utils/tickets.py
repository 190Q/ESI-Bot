import os
import json
from pathlib import Path
from typing import Optional, Union
import discord
from utils.paths import PROJECT_ROOT, DATA_DIR

CATEGORY_THRESHOLD = 50

# Default ticket guild and role IDs
TICKET_GUILD_ID = 1448532791686860923
BASE_TICKET_ROLE_ID = 1448533030091227227
DEVELOPER_ROLE_ID = 1464696049896788104
USER_SUPPORT_ROLE_ID = 1464695189380530423
RECRUITMENT_MANAGER_ROLE_ID = 1479622592456294550
SINDIAN_CITIZEN_ROLE_ID = 554889169705500672
PARLIAMENT_ROLE_ID = 600185623474601995
JUROR_ROLE_ID = 954566591520063510

TICKET_PERMS = discord.PermissionOverwrite(
    view_channel=True,
    send_messages=True,
    attach_files=True,
    add_reactions=True,
    use_external_emojis=True,
    use_external_stickers=True,
    read_message_history=True,
)


def _load_ticket_data(tracking_file: Union[str, Path]) -> dict:
    """Load ticket/category JSON data safely with fallback."""
    tracking_path = Path(tracking_file)
    if not tracking_path.exists():
        return {"tickets": {}, "categories": {}, "archived_tickets": {}}
    try:
        with open(tracking_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"tickets": {}, "categories": {}, "archived_tickets": {}}
            if "categories" not in data:
                data["categories"] = {}
            if "tickets" not in data:
                data["tickets"] = {}
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"tickets": {}, "categories": {}, "archived_tickets": {}}


def _save_ticket_data(tracking_file: Union[str, Path], data: dict) -> bool:
    """Save ticket/category JSON data safely."""
    tracking_path = Path(tracking_file)
    tracking_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(tracking_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return True
    except Exception as e:
        print(f"[TICKETS] Error saving tracking file {tracking_file}: {e}")
        return False


async def get_or_create_category(
    guild: discord.Guild,
    category_name: str,
    tracking_file: Optional[Union[str, Path]] = None,
    threshold: int = CATEGORY_THRESHOLD
) -> Optional[discord.CategoryChannel]:
    """Get existing category with space or create a new numbered one."""
    if tracking_file is None:
        tracking_file = DATA_DIR / "support_tickets.json"

    base_name = category_name.split(' #')[0]
    data = _load_ticket_data(tracking_file)

    tracked_categories = data.get("categories", {}).get(base_name, [])
    valid_categories = []

    for cat_id in tracked_categories:
        category = guild.get_channel(cat_id)
        if category and len(category.channels) < threshold:
            valid_categories.append(cat_id)
            # Return first category with space if it was already tracked
            if len(valid_categories) == len(tracked_categories):
                return category
        elif category and len(category.channels) >= threshold:
            valid_categories.append(cat_id)
        # If category doesn't exist on Discord, omit it from valid_categories

    if "categories" not in data:
        data["categories"] = {}
    data["categories"][base_name] = valid_categories

    # Return existing category with space
    for cat_id in valid_categories:
        category = guild.get_channel(cat_id)
        if category and len(category.channels) < threshold:
            _save_ticket_data(tracking_file, data)
            return category

    # Create new category
    try:
        next_number = len(valid_categories) + 1
        new_category_name = f"{category_name} #{next_number}" if next_number > 1 else category_name

        position = None
        if valid_categories:
            last_cat = guild.get_channel(valid_categories[-1])
            if last_cat:
                position = last_cat.position + 1

        new_category = await guild.create_category(new_category_name, position=position)

        if base_name not in data["categories"]:
            data["categories"][base_name] = []
        data["categories"][base_name].append(new_category.id)
        _save_ticket_data(tracking_file, data)

        print(f"[TICKETS] Created new category: {new_category_name} at position {position}")
        return new_category
    except Exception as e:
        print(f"[TICKETS] Error creating category: {e}")
        return None


async def cleanup_empty_categories(
    guild: discord.Guild,
    base_name: str,
    tracking_file: Optional[Union[str, Path]] = None
) -> None:
    """Remove empty numbered categories and update tracking."""
    if tracking_file is None:
        tracking_file = DATA_DIR / "support_tickets.json"

    data = _load_ticket_data(tracking_file)
    if "categories" not in data or base_name not in data["categories"]:
        return

    tracked = data["categories"][base_name]
    valid = []

    for cat_id in tracked:
        category = guild.get_channel(cat_id)
        if category:
            if '#' in category.name and len(category.channels) == 0:
                try:
                    await category.delete()
                    print(f"[TICKETS] Deleted empty category: {category.name}")
                except Exception as e:
                    print(f"[TICKETS] Failed to delete category: {e}")
                    valid.append(cat_id)
            else:
                valid.append(cat_id)

    data["categories"][base_name] = valid
    _save_ticket_data(tracking_file, data)
