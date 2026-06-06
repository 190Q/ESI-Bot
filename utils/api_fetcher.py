import aiohttp
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


async def make_api_request(
    session: aiohttp.ClientSession,
    url: str,
    headers: Dict[str, str],
    log_prefix: str = "[API]",
) -> Tuple[bool, Optional[Dict]]:
    """Execute a JSON GET request and return (success, payload)."""
    try:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                return True, data
            print(f"{log_prefix} Request failed with status {response.status}")
            return False, None
    except Exception as e:
        print(f"{log_prefix} Request failed for {url}: {e}")
        return False, None


async def fetch_guild_info(
    base_url: str,
    guild_name: str,
    headers: Dict[str, str],
    log_prefix: str = "[API]",
) -> Optional[Dict]:
    """Fetch guild information from Wynncraft API using guild prefix."""
    import urllib.parse

    encoded_guild_name = urllib.parse.quote(guild_name)
    url = f"{base_url}/guild/prefix/{encoded_guild_name}"

    async with aiohttp.ClientSession() as session:
        success, data = await make_api_request(session, url, headers=headers, log_prefix=log_prefix)
        if success:
            return data
        print(f"{log_prefix} API request failed.")
        return None


def extract_guild_members(guild_data: Dict) -> List[Dict]:
    """Extract member data with ranks, UUIDs, and full globalData from guild data."""
    members = []

    if "members" in guild_data:
        members_data = guild_data["members"]

        for rank, rank_members in members_data.items():
            if rank == "total":
                continue

            if isinstance(rank_members, dict):
                for username, member_info in rank_members.items():
                    member_dict = {"username": username, "rank": rank}

                    if isinstance(member_info, dict):
                        member_dict.update(member_info)

                        global_data = member_info.get("globalData") or {}
                        if isinstance(global_data, dict) and "guildRaids" in global_data:
                            member_dict["guildRaids"] = global_data["guildRaids"]

                    members.append(member_dict)

    return members


def extract_member_stats(
    member: Dict,
    guild_data: Optional[Dict] = None,
    log_prefix: str = "[API]",
) -> Dict:
    """Extract normalized member statistics from a guild member payload."""
    stats = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uuid": None,
        "guild": {
            "uuid": None,
            "name": None,
            "prefix": None,
            "rank": None,
        },
        "playtime": 0,
        "wars": 0,
        "totalLevel": 0,
        "mobsKilled": 0,
        "chestsFound": 0,
        "dungeons": {"total": 0, "list": {}},
        "raids": {"total": 0, "list": {}},
        "worldEvents": 0,
        "lootRuns": 0,
        "caves": 0,
        "completedQuests": 0,
        "pvp": {"kills": 0, "deaths": 0},
    }

    try:
        if "uuid" in member:
            stats["uuid"] = member.get("uuid")
        if "username" in member:
            stats["username"] = member.get("username")

        if isinstance(guild_data, dict):
            stats["guild"]["uuid"] = guild_data.get("uuid")
            stats["guild"]["name"] = guild_data.get("name")
            stats["guild"]["prefix"] = guild_data.get("prefix")
        stats["guild"]["rank"] = member.get("rank")

        if "playtime" in member and isinstance(member.get("playtime"), (int, float)):
            stats["playtime"] = member["playtime"]

        global_data = member.get("globalData") or {}
        if isinstance(global_data, dict):
            if stats["playtime"] == 0 and isinstance(global_data.get("playtime"), (int, float)):
                stats["playtime"] = global_data["playtime"]

            if isinstance(global_data.get("wars"), (int, float)):
                stats["wars"] = int(global_data["wars"])

            if "totalLevel" in global_data:
                stats["totalLevel"] = global_data["totalLevel"] or 0

            if "mobsKilled" in global_data:
                stats["mobsKilled"] = global_data["mobsKilled"] or 0
            elif "killedMobs" in global_data:
                stats["mobsKilled"] = global_data["killedMobs"] or 0

            if "chestsFound" in global_data:
                stats["chestsFound"] = global_data["chestsFound"] or 0
            elif "foundChests" in global_data:
                stats["chestsFound"] = global_data["foundChests"] or 0

            dungeons = global_data.get("dungeons")
            if isinstance(dungeons, dict):
                stats["dungeons"]["total"] = dungeons.get("total", 0) or 0
                if isinstance(dungeons.get("list"), dict):
                    stats["dungeons"]["list"] = dungeons["list"]

            raids = global_data.get("raids")
            if isinstance(raids, dict):
                stats["raids"]["total"] = raids.get("total", 0) or 0
                if isinstance(raids.get("list"), dict):
                    stats["raids"]["list"] = raids["list"]

            if "worldEvents" in global_data:
                stats["worldEvents"] = global_data["worldEvents"] or 0
            elif "completedWorldEvents" in global_data:
                stats["worldEvents"] = global_data["completedWorldEvents"] or 0

            if "lootRuns" in global_data:
                stats["lootRuns"] = global_data["lootRuns"] or 0
            elif "lootruns" in global_data:
                stats["lootRuns"] = global_data["lootruns"] or 0
            elif "completedLootRuns" in global_data:
                stats["lootRuns"] = global_data["completedLootRuns"] or 0

            if "caves" in global_data:
                stats["caves"] = global_data["caves"] or 0
            elif "completedCaves" in global_data:
                stats["caves"] = global_data["completedCaves"] or 0

            if isinstance(global_data.get("completedQuests"), (int, float)):
                stats["completedQuests"] = int(global_data["completedQuests"])

            pvp = global_data.get("pvp")
            if isinstance(pvp, dict):
                stats["pvp"]["kills"] = pvp.get("kills", 0) or 0
                stats["pvp"]["deaths"] = pvp.get("deaths", 0) or 0

        return stats

    except (KeyError, TypeError, ValueError) as e:
        print(f"{log_prefix} Error extracting player stats: {e}")
        return stats


def build_member_stats_from_guild_payload(
    guild_data: Dict,
    log_prefix: str = "[API]",
) -> Tuple[List[Dict], List[Dict]]:
    """Build normalized member list and member stats from one guild payload."""
    members = extract_guild_members(guild_data)
    member_stats = []

    for member in members:
        player_stats = extract_member_stats(member, guild_data=guild_data, log_prefix=log_prefix)
        member_stats.append(
            {
                "username": member.get("username"),
                "uuid": player_stats.get("uuid"),
                "timestamp": player_stats["timestamp"],
                "guild": player_stats["guild"],
                "playtime": player_stats["playtime"],
                "wars": player_stats["wars"],
                "totalLevel": player_stats["totalLevel"],
                "mobsKilled": player_stats["mobsKilled"],
                "chestsFound": player_stats["chestsFound"],
                "dungeons": player_stats["dungeons"],
                "raids": player_stats["raids"],
                "worldEvents": player_stats["worldEvents"],
                "lootRuns": player_stats["lootRuns"],
                "caves": player_stats["caves"],
                "completedQuests": player_stats["completedQuests"],
                "pvp": player_stats["pvp"],
            }
        )

    return members, member_stats
