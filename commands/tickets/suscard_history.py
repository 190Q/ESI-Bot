import asyncio
from typing import Callable, Iterable, List, Optional

import aiohttp


WYNNCRAFT_API_BASE = "https://api.wynncraft.com/v3"
HISTORY_SEPARATOR = "  •  "


def format_guild_history(names: Iterable[str]) -> str:
    cleaned = [name.strip() for name in names if name and name.strip()]
    return HISTORY_SEPARATOR.join(cleaned) if cleaned else "No public guild history"


def choose_recent_guild_names(
    names: Iterable[str],
    measure_text: Callable[[str], float],
    max_width: float,
) -> List[str]:
    cleaned = [name.strip() for name in names if name and name.strip()]
    if not cleaned:
        return []

    maximum = min(4, len(cleaned))
    minimum = min(2, maximum)
    for count in range(maximum, minimum - 1, -1):
        selected = cleaned[-count:]
        if measure_text(format_guild_history(selected)) <= max_width:
            return selected

    return cleaned[-minimum:]


def resolve_guild_names(guild_ids: Iterable[str], directory: dict) -> List[str]:
    recent_ids = list(dict.fromkeys(guild_ids))[-4:]
    names = []
    for guild_uuid in recent_ids:
        guild = directory.get(guild_uuid) or {}
        name = guild.get("name")
        if name:
            names.append(name)
    return names


async def fetch_guild_history_names(
    player_data: dict,
    headers: Optional[dict] = None,
) -> List[str]:
    guild_ids = player_data.get("guildHistory") or []
    if not guild_ids:
        return []

    timeout = aiohttp.ClientTimeout(total=15)
    url = f"{WYNNCRAFT_API_BASE}/guild/list/guild?identifier=uuid"
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers or {}) as response:
                if response.status != 200:
                    return []
                directory = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return []

    return resolve_guild_names(guild_ids, directory)
