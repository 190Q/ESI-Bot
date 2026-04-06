import discord
from discord import app_commands
from discord.ui import View, Select
import os
import json
import aiohttp
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from utils.permissions import has_roles
from utils.paths import PROJECT_ROOT, DATA_DIR, DB_DIR

OWNER_ID = int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0
REQUIRED_ROLES = [
    OWNER_ID,
    554889169705500672
]

ASPECTS_FILE = DATA_DIR / "aspects.json"

RANK_PRIORITY = {'owner': 6, 'chief': 5, 'strategist': 4, 'captain': 3, 'recruiter': 2, 'recruit': 1}


def load_aspects_data():
    """Load aspects tracking data from JSON file."""
    try:
        if ASPECTS_FILE.exists():
            with open(ASPECTS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"[ASPECTS] Failed to load aspects data: {e}")
    return {"total_aspects": 22, "members": {}}


def save_aspects_data(data):
    """Save aspects tracking data to JSON file."""
    try:
        with open(ASPECTS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[ASPECTS] Failed to save aspects data: {e}")


async def fetch_guild_data():
    """Fetch current guild data from Wynncraft API."""
    url = "https://api.wynncraft.com/v3/guild/prefix/ESI"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    print(f"[ASPECTS] API returned status {response.status}")
    except Exception as e:
        print(f"[ASPECTS] Error fetching guild data: {e}")
    return None


def extract_members_with_graids(guild_data):
    """Extract all members with their UUIDs and guildRaids from guild data."""
    members = {}
    if not guild_data or 'members' not in guild_data:
        return members
    
    members_data = guild_data['members']
    for rank, rank_members in members_data.items():
        if rank == 'total':
            continue
        if isinstance(rank_members, dict):
            for username, member_info in rank_members.items():
                if isinstance(member_info, dict):
                    uuid = member_info.get('uuid', '')
                    graids = member_info.get('guildRaids', {})
                    total_graids = graids.get('total', 0) if isinstance(graids, dict) else 0
                    if uuid:
                        members[uuid] = {
                            'name': username,
                            'graids': total_graids,
                            'rank': rank
                        }
    return members


def update_aspects_data(aspects_data, current_members):
    """Update aspects data with current member graid info.
    
    For new members: set baseline to current graids (no retroactive aspects).
    For existing members: calculate new aspects earned since baseline, add to owed.
    Total owed across all members is capped at 120.
    """
    changed = False
    
    # Clamp total_aspects to the max of 120
    if aspects_data['total_aspects'] > 120:
        aspects_data['total_aspects'] = 120
        changed = True
    
    for uuid, member in current_members.items():
        if uuid not in aspects_data['members']:
            # New member - set baseline to current graids
            aspects_data['members'][uuid] = {
                'name': member['name'],
                'baseline_graids': member['graids'],
                'owed': 0
            }
            changed = True
        else:
            stored = aspects_data['members'][uuid]
            
            # Update name if changed
            if stored['name'] != member['name']:
                stored['name'] = member['name']
                changed = True
            
            # Calculate new aspects earned since baseline
            current_graids = member['graids']
            baseline = stored.get('baseline_graids', current_graids)
            new_graids = current_graids - baseline
            
            if new_graids >= 2:
                new_aspects = new_graids // 2
                current_owed = stored.get('owed', 0)
                stored['owed'] = current_owed + new_aspects
                aspects_data['total_aspects'] = min(120, aspects_data['total_aspects'] + new_aspects)
                # Advance baseline by the graids that were converted
                stored['baseline_graids'] = baseline + (new_aspects * 2)
                changed = True
    
    if changed:
        save_aspects_data(aspects_data)
    
    return aspects_data


def build_aspects_embed(aspects_data, current_members):
    """Build the embed showing aspect information."""
    embed = discord.Embed(
        title="Guild Raid Aspects",
        description=f"**Total Aspects Available:** {aspects_data['total_aspects']}\n*(2 graids = 1 aspect)*",
        color=0x5865f2,
        timestamp=datetime.now(timezone.utc)
    )
    
    # Build member list sorted by owed, then rank, then alphabetically
    member_entries = []
    total_owed = 0
    
    for uuid, member in current_members.items():
        stored = aspects_data['members'].get(uuid, {})
        owed = stored.get('owed', 0)
        total_owed += max(owed, 0)
        
        member_entries.append({
            'uuid': uuid,
            'name': member['name'],
            'graids': member['graids'],
            'owed': owed,
            'rank': member.get('rank', 'recruit')
        })
    
    member_entries.sort(key=lambda x: (-x['owed'], -RANK_PRIORITY.get(x['rank'], 0), x['name'].lower()))
    
    # Members owed aspects
    owed_lines = []
    for entry in member_entries:
        if entry['owed'] > 0:
            rank_label = entry['rank'].capitalize()
            owed_lines.append(
                f"**{entry['name']}** [{rank_label}]: {entry['owed']} aspect{'s' if entry['owed'] != 1 else ''}"
            )
    
    if owed_lines:
        # Split into fields if too long (Discord field limit: 1024 chars)
        field_text = ""
        field_num = 0
        for line in owed_lines:
            if len(field_text) + len(line) + 1 > 1024:
                field_name = "Members Owed Aspects" if field_num == 0 else "\u200b"
                embed.add_field(name=field_name, value=field_text.strip(), inline=False)
                field_text = ""
                field_num += 1
            field_text += line + "\n"
        
        if field_text.strip():
            field_name = "Members Owed Aspects" if field_num == 0 else "\u200b"
            embed.add_field(name=field_name, value=field_text.strip(), inline=False)
    else:
        embed.add_field(name="Members Owed Aspects", value="No members are currently owed aspects.", inline=False)
    
    embed.add_field(
        name="Summary",
        value=f"**Total Owed:** {total_owed} aspect{'s' if total_owed != 1 else ''}\n"
              f"**Total Pool:** {aspects_data['total_aspects']} aspect{'s' if aspects_data['total_aspects'] != 1 else ''}\n"
              f"**Members Tracked:** {len(aspects_data['members'])}",
        inline=False
    )
    
    return embed


class GiveAspectsSelect(Select):
    """Select menu to pick members and give them 1 aspect each."""
    
    def __init__(self, members_chunk, aspects_data, current_members, chunk_index):
        options = []
        for uuid, name, graids, owed, rank in members_chunk:
            rank_label = rank.capitalize()
            label = f"{name} [{rank_label}]"
            if len(label) > 100:
                label = label[:97] + "..."
            
            desc = f"Owed: {owed} aspect{'s' if owed != 1 else ''}"
            if len(desc) > 100:
                desc = desc[:97] + "..."
            
            options.append(discord.SelectOption(
                label=label,
                value=uuid,
                description=desc
            ))
        
        placeholder = f"Select member(s) to give 1 aspect each ({chunk_index + 1})"
        
        super().__init__(
            placeholder=placeholder,
            options=options,
            min_values=1,
            max_values=min(len(options), 25),
            custom_id=f"aspects_give_{chunk_index}"
        )
        self.aspects_data = aspects_data
        self.current_members = current_members
    
    async def callback(self, interaction: discord.Interaction):
        # Reload data to avoid race conditions
        self.aspects_data = load_aspects_data()
        
        cleared_names = []
        total_cleared = 0
        for uuid in self.values:
            if uuid in self.aspects_data['members']:
                owed = self.aspects_data['members'][uuid].get('owed', 0)
                if owed > 0:
                    self.aspects_data['total_aspects'] = max(0, self.aspects_data['total_aspects'] - owed)
                    total_cleared += owed
                    self.aspects_data['members'][uuid]['owed'] = 0
                    cleared_names.append(f"{self.aspects_data['members'][uuid]['name']} ({owed})")
        
        save_aspects_data(self.aspects_data)
        
        names_str = ", ".join(cleared_names)
        
        # Rebuild embed and view with updated data
        updated_embed = build_aspects_embed(self.aspects_data, self.current_members)
        updated_view = AspectsView(self.aspects_data, self.current_members)
        
        await interaction.response.edit_message(embed=updated_embed, view=updated_view)
        
        # Send a follow-up confirming what was cleared
        await interaction.followup.send(
            f"✅ Cleared owed aspects for: **{names_str}**\n"
            f"Total cleared: **{total_cleared}** aspect{'s' if total_cleared != 1 else ''}\n"
            f"Remaining pool: **{self.aspects_data['total_aspects']}** aspects",
            ephemeral=True
        )


class AspectsView(View):
    """View with select menus for giving aspects to members."""
    
    def __init__(self, aspects_data, current_members):
        super().__init__(timeout=300)
        
        # Build sorted member list for selectors (only members owed aspects)
        member_list = []
        for uuid, member in current_members.items():
            stored = aspects_data['members'].get(uuid, {})
            owed = stored.get('owed', 0)
            if owed > 0:
                rank = member.get('rank', 'recruit')
                member_list.append((uuid, member['name'], member['graids'], owed, rank))
        
        # Sort by owed (desc), then rank (desc), then name (asc)
        member_list.sort(key=lambda x: (-x[3], -RANK_PRIORITY.get(x[4], 0), x[1].lower()))
        
        # Split into chunks of 25 (Discord select limit)
        # Max 5 action rows per view
        chunk_size = 25
        max_selects = 5
        for i in range(0, min(len(member_list), chunk_size * max_selects), chunk_size):
            chunk = member_list[i:i + chunk_size]
            if chunk:
                select = GiveAspectsSelect(chunk, aspects_data, current_members, i // chunk_size)
                self.add_item(select)


def setup(bot, has_required_role, config):
    """Aspects Command"""
    
    @bot.tree.command(
        name="aspects",
        description="View and manage guild raid aspects (2 graids = 1 aspect)"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def aspects(interaction: discord.Interaction):
        """View and manage guild raid aspects"""
        
        # Check permissions
        if interaction.guild:
            if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
                await interaction.response.send_message(
                    "❌ You don't have permission to use this command!",
                    ephemeral=True
                )
                return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Fetch current guild data
            guild_data = await fetch_guild_data()
            if not guild_data:
                await interaction.followup.send(
                    "❌ Failed to fetch guild data from Wynncraft API.",
                    ephemeral=True
                )
                return
            
            # Extract members with graid data
            current_members = extract_members_with_graids(guild_data)
            
            if not current_members:
                await interaction.followup.send(
                    "❌ No members found in guild data.",
                    ephemeral=True
                )
                return
            
            # Load and update aspects data
            aspects_data = load_aspects_data()
            aspects_data = update_aspects_data(aspects_data, current_members)
            
            # Build embed
            embed = build_aspects_embed(aspects_data, current_members)
            
            # Create view with selectors
            view = AspectsView(aspects_data, current_members)
            
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        
        except Exception as e:
            await interaction.followup.send(
                f"❌ An error occurred: {str(e)}",
                ephemeral=True
            )
            print(f"[ASPECTS] Error in aspects command: {e}")
            import traceback
            traceback.print_exc()
    
    print("[OK] Loaded aspects command")
