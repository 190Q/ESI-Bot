import discord
from discord import app_commands
import os
import json
import aiohttp
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from utils.permissions import has_roles

load_dotenv()

USERNAME_MATCH_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data/username_matches.json",
)

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
]

# Load all WYNNCRAFT_KEY_* environment variables (same as fetch_api)
WYNNCRAFT_KEYS = []
key_index = 1
while True:
    key = os.getenv(f'WYNNCRAFT_KEY_{key_index}')
    if key is None or key_index > 6:
        break
    # Filter out placeholder keys
    if not key.startswith('your_key_'):
        WYNNCRAFT_KEYS.append(key)
    key_index += 1

print(f"[MIGRATE] Loaded {len(WYNNCRAFT_KEYS)} valid API keys")

_current_key_index = 0

def _get_next_api_key() -> str:
    """Get the next API key in rotation"""
    global _current_key_index
    if not WYNNCRAFT_KEYS:
        return None
    key = WYNNCRAFT_KEYS[_current_key_index % len(WYNNCRAFT_KEYS)]
    _current_key_index += 1
    return key

async def fetch_player_uuid(username: str) -> str:
    """Fetch player UUID from Wynncraft API with rate limiting"""
    try:
        api_key = _get_next_api_key()
        if not api_key:
            print(f"[MIGRATE] No API keys available")
            return None
        
        headers = {'Authorization': f'Bearer {api_key}'}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.wynncraft.com/v3/player/{username}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('uuid')
                elif response.status == 404:
                    print(f"[MIGRATE] Player '{username}' not found on Wynncraft")
                    return None
                elif response.status == 429:
                    print(f"[MIGRATE] Rate limited for '{username}', waiting 5 seconds...")
                    await asyncio.sleep(5)
                    return None
                else:
                    print(f"[MIGRATE] API error {response.status} for '{username}'")
                    return None
    except asyncio.TimeoutError:
        print(f"[MIGRATE] Timeout fetching UUID for '{username}'")
        return None
    except Exception as e:
        print(f"[MIGRATE] Error fetching UUID for '{username}': {e}")
        return None

async def migrate_uuids():
    """Migrate all usernames in the JSON to include UUIDs"""
    try:
        # Load existing data
        if not os.path.exists(USERNAME_MATCH_DB_PATH):
            print(f"[MIGRATE] File not found: {USERNAME_MATCH_DB_PATH}")
            return 0, 0, 0
        
        with open(USERNAME_MATCH_DB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        migrated_count = 0
        already_migrated = 0
        failed_count = 0
        total_count = len(data)
        
        print(f"[MIGRATE] Starting migration of {total_count} entries...")
        
        for user_id, entry in data.items():
            # Check if already migrated (is a dict with username and uuid)
            if isinstance(entry, dict) and 'username' in entry and 'uuid' in entry:
                already_migrated += 1
                print(f"[MIGRATE] [{already_migrated + migrated_count}/{total_count}] Already migrated: {entry['username']}")
                continue
            
            # Extract username from either string or dict
            if isinstance(entry, str):
                username = entry
            elif isinstance(entry, dict) and 'username' in entry:
                username = entry['username']
            else:
                print(f"[MIGRATE] Skipping invalid entry for user {user_id}: {entry}")
                failed_count += 1
                continue
            
            # Fetch UUID from API
            print(f"[MIGRATE] [{already_migrated + migrated_count + 1}/{total_count}] Fetching UUID for '{username}'...")
            uuid = await fetch_player_uuid(username)
            
            # Add delay between requests to respect API rate limits
            await asyncio.sleep(0.5)
            
            if uuid:
                # Update entry with UUID
                data[user_id] = {
                    'username': username,
                    'uuid': uuid
                }
                migrated_count += 1
                print(f"[MIGRATE] ✓ Successfully migrated '{username}' with UUID: {uuid}")
            else:
                print(f"[MIGRATE] ✗ Failed to get UUID for '{username}', keeping as string")
                failed_count += 1
                # Keep the string format if we can't fetch UUID
                if not isinstance(entry, str):
                    data[user_id] = username
        
        # Save migrated data
        with open(USERNAME_MATCH_DB_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        print(f"[MIGRATE] Migration complete!")
        print(f"[MIGRATE] Successfully migrated: {migrated_count}")
        print(f"[MIGRATE] Already migrated: {already_migrated}")
        print(f"[MIGRATE] Failed to fetch: {failed_count}")
        
        return migrated_count, already_migrated, failed_count
    
    except Exception as e:
        print(f"[MIGRATE] Error during migration: {e}")
        import traceback
        traceback.print_exc()
        return 0, 0, 0

def setup(bot, has_required_role, config):
    """Setup migration command"""
    
    @bot.tree.command(
        name="migrate_uuids",
        description="[ADMIN] Migrate username_matches.json to include UUIDs (temporary command)"
    )
    async def migrate_uuids_command(interaction: discord.Interaction):
        """Migrate all usernames to include UUIDs from Wynncraft API"""
        
        # Check permissions
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            await interaction.response.send_message(
                "❌ You don't have permission to use this command!",
                ephemeral=True
            )
            return
        
        # Defer since this will take time
        await interaction.response.defer(ephemeral=False)
        
        # Run migration
        migrated, already_migrated, failed = await migrate_uuids()
        
        # Create result embed
        embed = discord.Embed(
            title="✅ UUID Migration Complete",
            description="Successfully migrated username_matches.json to include UUIDs",
            color=0x00FF00
        )
        
        embed.add_field(
            name="Migration Results",
            value=(
                f"**Newly Migrated:** {migrated}\n"
                f"**Already Migrated:** {already_migrated}\n"
                f"**Failed to Fetch:** {failed}\n"
                f"**Total:** {migrated + already_migrated + failed}"
            ),
            inline=False
        )
        
        embed.add_field(
            name="Status",
            value="✅ All entries have been processed and saved to username_matches.json",
            inline=False
        )
        
        await interaction.followup.send(embed=embed)
    
    print("[OK] Loaded migrate_uuids command (TEMPORARY)")
