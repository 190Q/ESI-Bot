import discord
from discord import app_commands
import aiohttp
import os
import sys
import math
import asyncio
import random
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import matplotlib.pyplot as plt
import io
import glob
import sqlite3
from pathlib import Path

# Add player.py directory to path
PLAYER_MODULE_PATH = '/home/ubuntu/DiscordBots/kira/python-commands/coj'
if PLAYER_MODULE_PATH not in sys.path:
    sys.path.insert(0, PLAYER_MODULE_PATH)

# ============================================================================
# CONFIGURATION
# ============================================================================
RECRUITMENT_CHANNEL_ID = 683093425452744778
# RECRUITMENT_CHANNEL_ID = 784352935198064660
REQUIRED_ROLE_ID = 954566591520063510

load_dotenv()

WYNNCRAFT_KEYS = [
    os.getenv('WYNNCRAFT_KEY_1'),
    os.getenv('WYNNCRAFT_KEY_2'),
    os.getenv('WYNNCRAFT_KEY_3')
]
WYNNCRAFT_KEYS = [key for key in WYNNCRAFT_KEYS if key]

# ============================================================================
# API HANDLER
# ============================================================================
class RecruitmentAPI:
    BASE_URL = "https://api.wynncraft.com/v3"
    SEQUOIA_URL = "https://api.sequoia.ooo/dw/player/delta"
    _current_key_index = 0
    
    @classmethod
    def _get_headers(cls):
        if not WYNNCRAFT_KEYS:
            return {}
        key = WYNNCRAFT_KEYS[cls._current_key_index % len(WYNNCRAFT_KEYS)]
        cls._current_key_index += 1
        return {'apikey': key}
    
    @classmethod
    async def fetch_player_data(cls, username: str):
        try:
            headers = cls._get_headers()
            async with aiohttp.ClientSession() as session:
                timeout = aiohttp.ClientTimeout(total=10)
                
                # Try with ?fullResult first, fall back to without
                for use_full_result in [True, False]:
                    suffix = "?fullResult" if use_full_result else ""
                    url = f"{cls.BASE_URL}/player/{username}{suffix}"
                    try:
                        async with session.get(url, headers=headers, timeout=timeout) as response:
                            if response.status == 200:
                                if not use_full_result:
                                    print(f"[INFO] Used non-fullResult fallback for: {username}")
                                return await response.json()
                            elif response.status == 300:
                                data = await response.json()
                                return {"multiple": True, "objects": data.get('objects', data)}
                            elif response.status == 429:
                                print(f"API rate limit hit for player {username}")
                                return {"error": "rate_limit", "message": "API rate limit exceeded. Please try again later."}
                            elif response.status == 404:
                                print(f"Player not found: {username}")
                                return None
                            else:
                                error_text = await response.text()
                                print(f"API returned status {response.status} for {username} (fullResult={use_full_result}): {error_text}")
                                continue  # Try without ?fullResult
                    except asyncio.TimeoutError:
                        print(f"Timeout fetching player data for {username} (fullResult={use_full_result})")
                        continue
                    except Exception as e:
                        print(f"Error fetching player data for {username} (fullResult={use_full_result}): {e}")
                        continue
                
                return None
        except Exception as e:
            print(f"Error fetching player data: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    @classmethod
    async def select_highest_playtime_player(cls, players_data: dict):
        """Select the player UUID with the highest playtime from multiple entries"""
        try:
            highest_playtime_uuid = None
            highest_playtime = -1
            
            for uuid in players_data.keys():
                try:
                    player_full_data = await cls.fetch_player_data(uuid)
                    
                    if player_full_data and not player_full_data.get('multiple'):
                        playtime = player_full_data.get('playtime', 0)
                        if playtime > highest_playtime:
                            highest_playtime = playtime
                            highest_playtime_uuid = uuid
                except Exception as inner_e:
                    print(f"Error fetching data for UUID {uuid}: {inner_e}")
                    continue
            
            if highest_playtime_uuid is None and players_data:
                highest_playtime_uuid = next(iter(players_data.keys()))
            
            return highest_playtime_uuid
        except Exception as e:
            print(f"Error selecting player with highest playtime: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    @classmethod
    async def fetch_playtime_data(cls, uuid_or_username: str, days: int = 7):
        try:
            params = {
                "player": uuid_or_username,
                "from": f"-{days}d",
                "to": "now"
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(cls.SEQUOIA_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        return await response.json()
                    return None
        except Exception as e:
            print(f"Error fetching playtime data: {e}")
            return None
    
    @classmethod
    async def fetch_player_skin(cls, player_uuid: str):
        """Fetch player skin image from vzge.me using UUID"""
        if not player_uuid:
            return None
            
        try:
            url = f"https://vzge.me/bust/512/{player_uuid}.png"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            async with aiohttp.ClientSession() as session:
                timeout = aiohttp.ClientTimeout(total=15)
                
                async with session.get(url, headers=headers, timeout=timeout) as response:
                    if response.status == 200:
                        data = await response.read()
                        return data
                    else:
                        return None
        except Exception as e:
            print(f"Error fetching skin: {e}")
            return None

# ============================================================================
# SUSPICIOUSNESS CALCULATOR
# ============================================================================
class SuspiciousnessCalculator:
    @staticmethod
    def sigmoid(x: float) -> float:
        try:
            return 100 / (1 + math.exp(-0.1 * (x - 50)))
        except OverflowError:
            return 0.0 if x < 0 else 100.0
    
    @classmethod
    def calculate(cls, player_data: dict) -> dict:
        support_ranks = ['vip', 'vipplus', 'hero', 'heroplus', 'champion']
        
        try:
            first_join = player_data.get('firstJoin')
            playtime = player_data.get('playtime', 0)
            global_data = player_data.get('globalData', {})
            total_level = global_data.get('totalLevel', 0)
            completed_quests = global_data.get('completedQuests', 0)
            support_rank = player_data.get('supportRank')
            
            characters = player_data.get('characters', {})
            total_raid_count = 0
            
            if characters and isinstance(characters, dict):
                for char_data in characters.values():
                    if not char_data or not isinstance(char_data, dict):
                        continue
                    raids_data = char_data.get('raids', {})
                    if raids_data and isinstance(raids_data, dict):
                        total_raid_count += raids_data.get('total', 0)
            
            if first_join:
                join_timestamp = datetime.fromisoformat(first_join.replace('Z', '+00:00')).timestamp()
                current_timestamp = datetime.now(timezone.utc).timestamp()
                two_years = 63072000
                join_sus_value = max(0, (current_timestamp - join_timestamp - two_years) * -1) * 100 / two_years
            else:
                join_sus_value = 50.0
            
            playtime_sus_value = max(0, (playtime - 800) * -1) * 100 / 800
            total_level_sus_value = max(0, (total_level - 250) * -1) * 100 / 250
            quest_sus_value = max(0, (completed_quests - 150) * -1) * 100 / 150
            
            if first_join:
                join_date = datetime.fromisoformat(first_join.replace('Z', '+00:00'))
                days_since_join = max(1, (datetime.now(timezone.utc) - join_date).days)
                time_spent_percentage = (playtime / (days_since_join * 24)) * 100
                time_spent_sus_value = cls.sigmoid(time_spent_percentage)
            else:
                time_spent_sus_value = 100.0
            
            if support_rank in support_ranks:
                rank_index = support_ranks.index(support_rank)
                rank_sus_value = max(0, (rank_index - 2) * -1) * 100 / 2
            else:
                rank_sus_value = 100.0
            
            raids_sus_value = 0 if total_raid_count >= 50 else max(0, (50 - total_raid_count) * 100 / 50)
            
            overall_sus = (join_sus_value + rank_sus_value + total_level_sus_value + 
                          playtime_sus_value + quest_sus_value + time_spent_sus_value +
                          raids_sus_value) / 7
            
            return {
                'overall_sus': overall_sus,
                'join_sus': join_sus_value,
                'rank_sus': rank_sus_value,
                'level_sus': total_level_sus_value,
                'playtime_sus': playtime_sus_value,
                'quest_sus': quest_sus_value,
                'time_spent_sus': time_spent_sus_value,
                'raids_sus': raids_sus_value
            }
        except Exception as e:
            print(f"Suspiciousness calculation error: {e}")
            return None

# ============================================================================
# PLAYTIME GRAPH GENERATOR
# ============================================================================
class PlaytimeGraphGenerator:
    @staticmethod
    def create_chart_from_deltas(daily_deltas: list, player_name: str, days_requested: int) -> io.BytesIO:
        """Create chart from pre-calculated daily deltas"""
        try:
            # Get the date range
            if daily_deltas:
                latest_date = daily_deltas[-1]['date']
            else:
                latest_date = datetime.now(timezone.utc)
            
            # Create complete date range
            all_dates = []
            all_deltas = []
            
            for i in range(days_requested - 1, -1, -1):
                date = latest_date - timedelta(days=i)
                date_str = date.strftime('%Y-%m-%d')
                all_dates.append(date.strftime('%m/%d'))
                
                # Find matching delta or use 0
                found_delta = 0
                for day_data in daily_deltas:
                    if day_data['date'].strftime('%Y-%m-%d') == date_str:
                        found_delta = day_data['delta']
                        break
                
                all_deltas.append(round(found_delta, 2))
            
            # Create figure with dark theme
            plt.style.use('dark_background')
            fig, ax = plt.subplots(figsize=(12, 6))
            fig.patch.set_facecolor('#2b2d31')
            ax.set_facecolor('#2b2d31')
            
            # Create bars
            bars = ax.bar(all_dates, all_deltas, color='#5865f2', edgecolor='#4752c4', linewidth=1.5)
            
            # Add value labels on top of bars
            for bar in bars:
                height = bar.get_height()
                if height > 0:
                    ax.text(bar.get_x() + bar.get_width()/2., height,
                        f'{height:.1f}h',
                        ha='center', va='bottom', color='white', fontsize=10, fontweight='bold')
            
            # Customize the plot
            ax.set_xlabel('Date', fontsize=12, color='white', fontweight='bold')
            ax.set_ylabel('Hours', fontsize=12, color='white', fontweight='bold')
            ax.set_title(f'{player_name} - Daily Playtime (Last {days_requested} Days)', 
                        fontsize=16, color='white', fontweight='bold', pad=20)
            
            # Grid styling
            ax.grid(True, alpha=0.3, linestyle='--', linewidth=0.5)
            ax.set_axisbelow(True)
            
            # Tick styling
            ax.tick_params(colors='white', labelsize=10)
            
            # Rotate x-axis labels for better readability
            plt.xticks(rotation=45, ha='right')
            
            # Set y-axis to start at 0
            ax.set_ylim(bottom=0)
            
            # Tight layout to prevent label cutoff
            plt.tight_layout()
            
            # Save to BytesIO
            buffer = io.BytesIO()
            plt.savefig(buffer, format='png', dpi=150, facecolor='#2b2d31', edgecolor='none')
            buffer.seek(0)
            plt.close(fig)
            
            return buffer
        except Exception as e:
            print(f"Error creating chart: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    @staticmethod
    def create_chart(playtime_data: dict, player_name: str) -> io.BytesIO:
        try:
            from collections import defaultdict
            
            date_totals = defaultdict(float)
            
            if "series" not in playtime_data:
                return None
            
            # Aggregate playtime by date
            for timestamp_str, day_data in playtime_data["series"].items():
                try:
                    timestamp = int(timestamp_str)
                    date = datetime.fromtimestamp(timestamp)
                    date_key = date.strftime("%Y-%m-%d")
                    
                    total_playtime_hours = 0.0
                    
                    if "character_deltas" in day_data:
                        for character in day_data["character_deltas"]:
                            playtime_delta = character.get("playtime_delta", 0)
                            total_playtime_hours += float(playtime_delta)
                    
                    date_totals[date_key] += total_playtime_hours
                except Exception as e:
                    print(f"Error processing timestamp: {e}")
                    continue
            
            if not date_totals:
                return None
            
            # Get the latest date from data
            sorted_dates = sorted(date_totals.keys())
            if not sorted_dates:
                return None
            
            latest_date_str = sorted_dates[-1]
            latest_date = datetime.strptime(latest_date_str, '%Y-%m-%d')
            
            # Create complete 7-day range
            all_dates = []
            all_deltas = []
            
            for i in range(6, -1, -1):
                date = latest_date - timedelta(days=i)
                date_key = date.strftime('%Y-%m-%d')
                all_dates.append(date.strftime('%m/%d'))
                
                # Get delta for this date or 0 if no data
                all_deltas.append(round(date_totals.get(date_key, 0.0), 2))
            
            # Create figure with dark theme
            plt.style.use('dark_background')
            fig, ax = plt.subplots(figsize=(12, 6))
            fig.patch.set_facecolor('#2b2d31')
            ax.set_facecolor('#2b2d31')
            
            # Create bars
            bars = ax.bar(all_dates, all_deltas, color='#5865f2', edgecolor='#4752c4', linewidth=1.5)
            
            # Add value labels on top of bars
            for bar in bars:
                height = bar.get_height()
                if height > 0:
                    ax.text(bar.get_x() + bar.get_width()/2., height,
                           f'{height:.1f}h',
                           ha='center', va='bottom', color='white', fontsize=10, fontweight='bold')
            
            # Customize the plot
            ax.set_xlabel('Date', fontsize=12, color='white', fontweight='bold')
            ax.set_ylabel('Hours', fontsize=12, color='white', fontweight='bold')
            ax.set_title(f'{player_name} - Daily Playtime (Last 7 Days)', 
                         fontsize=16, color='white', fontweight='bold', pad=20)
            
            # Grid styling
            ax.grid(True, alpha=0.3, linestyle='--', linewidth=0.5)
            ax.set_axisbelow(True)
            
            # Tick styling
            ax.tick_params(colors='white', labelsize=10)
            
            # Rotate x-axis labels for better readability
            plt.xticks(rotation=45, ha='right')
            
            # Set y-axis to start at 0
            ax.set_ylim(bottom=0)
            
            # Tight layout to prevent label cutoff
            plt.tight_layout()
            
            # Save to BytesIO
            buffer = io.BytesIO()
            plt.savefig(buffer, format='png', dpi=150, facecolor='#2b2d31', edgecolor='none')
            buffer.seek(0)
            plt.close(fig)
            
            return buffer
        except Exception as e:
            print(f"Error creating chart: {e}")
            import traceback
            traceback.print_exc()
            return None

# ============================================================================
# PLAYER CARD GENERATOR - IMPORTED FROM PLAYER.PY
# ============================================================================
async def generate_player_card(player_data: dict, skin_bytes: bytes = None):
    """Generate player card using the same method as the player command"""
    try:
        # Import the generator from player.py using dynamic import
        import sys
        import os
        
        # Get the directory containing this file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)
        
        import player
        from player import PlayerStatsImageGenerator
        
        # Generate the card using the exact same method
        return await PlayerStatsImageGenerator.generate(player_data, skin_bytes)
    except ImportError as e:
        print(f"ERROR: Could not import PlayerStatsImageGenerator - {e}")
        import traceback
        traceback.print_exc()
        raise

def create_sus_embed(player_data: dict, sus_data: dict) -> discord.Embed:
    """Create suspiciousness breakdown embed"""
    username = player_data.get('username', 'Unknown').replace('_', '\\_')
    uuid = player_data.get('uuid', '')
    
    embed = discord.Embed(
        title=f"Suspiciousness - {username}: {sus_data['overall_sus']:.1f}%",
        description="Detailed breakdown",
        color=0x00ffff
    )
    
    if uuid:
        embed.set_thumbnail(url=f"https://vzge.me/bust/512/{uuid}.png")
    
    embed.add_field(name="Join Date Sus", value=f"{sus_data['join_sus']:.1f}%", inline=True)
    embed.add_field(name="Rank Sus", value=f"{sus_data['rank_sus']:.1f}%", inline=True)
    embed.add_field(name="Level Sus", value=f"{sus_data['level_sus']:.1f}%", inline=True)
    embed.add_field(name="Playtime Sus", value=f"{sus_data['playtime_sus']:.1f}%", inline=True)
    embed.add_field(name="Quest Sus", value=f"{sus_data['quest_sus']:.1f}%", inline=True)
    embed.add_field(name="Time Spent Sus", value=f"{sus_data['time_spent_sus']:.1f}%", inline=True)
    embed.add_field(name="Raids Sus", value=f"{sus_data['raids_sus']:.1f}%", inline=True)
    
    return embed

# ============================================================================
# RECRUITMENT MODAL
# ============================================================================
class RecruitmentModal(discord.ui.Modal, title="Player Recruitment"):
    username_input = discord.ui.TextInput(
        label="Player Username",
        placeholder="e.g., 190Q",
        required=True,
        min_length=1,
        max_length=100
    )
    
    message_context = None
    
    async def on_submit(self, interaction: discord.Interaction):
        username = str(self.username_input.value).strip()
        
        if not username or username == '{username}':
            await interaction.response.send_message(content="Error: Please enter a valid username", ephemeral=True)
            return
        
        await interaction.response.send_message(content="Creating recruitment vote...", ephemeral=True)
        
        try:
            recruitment_channel = interaction.client.get_channel(RECRUITMENT_CHANNEL_ID)
            if not recruitment_channel:
                await interaction.followup.send(content=f"Error: Recruitment channel not found (ID: {RECRUITMENT_CHANNEL_ID})", ephemeral=True)
                return
            
            # Fetch initial player data
            player_data = await RecruitmentAPI.fetch_player_data(username)
            
            if not player_data:
                await interaction.followup.send(content=f"Error: Unable to find player '{username}'", ephemeral=True)
                return
            
            # Handle API errors
            if player_data.get('error'):
                error_msg = player_data.get('message', 'Unknown API error')
                await interaction.followup.send(content=f"Error: {error_msg}", ephemeral=True)
                return
            
            # Handle multiple players - select the one with highest playtime
            if player_data.get('multiple'):
                players_data = player_data.get('objects', {})
                
                if not players_data:
                    await interaction.followup.send(content="Error: No player data found in multiple entries response.", ephemeral=True)
                    return
                
                selected_uuid = await RecruitmentAPI.select_highest_playtime_player(players_data)
                
                if not selected_uuid:
                    await interaction.followup.send(content="Error: Could not determine player from multiple entries.", ephemeral=True)
                    return
                
                # Fetch the full data for the selected player
                player_data = await RecruitmentAPI.fetch_player_data(selected_uuid)
                
                if not player_data or player_data.get('multiple'):
                    await interaction.followup.send(content="Error: Failed to fetch selected player data.", ephemeral=True)
                    return
            
            # Get UUID for single player or already resolved multiple
            selected_uuid = player_data.get('uuid')
            
            # Fetch player skin
            skin_bytes = await RecruitmentAPI.fetch_player_skin(selected_uuid)
            
            # Generate player card using the same method as /player command
            player_card_bytes = await generate_player_card(player_data, skin_bytes)
            
            # Fetch playtime data from local database
            try:
                # Get databases for 7 days
                db_folder = "databases"
                days = 7
                db_pattern = os.path.join(db_folder, "ESI_*.db")
                db_files = sorted(glob.glob(db_pattern), key=os.path.getmtime, reverse=True)
                
                playtime_chart_buffer = None
                
                if db_files:
                    latest_db = db_files[0]
                    latest_time = datetime.fromtimestamp(os.path.getmtime(latest_db), tz=timezone.utc)
                    target_time = latest_time - timedelta(days=days)
                    
                    # Get all databases within the timeframe
                    databases_in_range = []
                    for db_file in db_files:
                        db_time = datetime.fromtimestamp(os.path.getmtime(db_file), tz=timezone.utc)
                        if db_time >= target_time:
                            databases_in_range.append((db_file, db_time))
                    
                    if len(databases_in_range) >= 2:
                        # Sort by time (oldest to newest)
                        databases_in_range.sort(key=lambda x: x[1])
                        
                        # Calculate daily deltas
                        from collections import defaultdict
                        date_totals = defaultdict(float)
                        player_username = player_data.get('username', username)
                        
                        for i in range(len(databases_in_range) - 1):
                            db1_path, db1_time = databases_in_range[i]
                            db2_path, db2_time = databases_in_range[i + 1]
                            
                            # Get playtime from both databases using LOCAL playtime data (player_playtime table)
                            try:
                                conn1 = sqlite3.connect(db1_path)
                                cursor1 = conn1.cursor()
                                
                                # Try to get from player_playtime table (local data) first
                                cursor1.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='player_playtime'")
                                has_local_data = cursor1.fetchone() is not None
                                
                                if has_local_data:
                                    cursor1.execute("SELECT total_playtime FROM player_playtime WHERE LOWER(username) = LOWER(?)", (player_username,))
                                    result1 = cursor1.fetchone()
                                    # Convert from seconds to hours
                                    playtime1 = result1[0] / 3600.0 if result1 else None
                                else:
                                    # Fallback to API data
                                    cursor1.execute("SELECT playtime FROM player_stats WHERE LOWER(username) = LOWER(?)", (player_username,))
                                    result1 = cursor1.fetchone()
                                    playtime1 = result1[0] if result1 else None
                                
                                conn1.close()
                                
                                conn2 = sqlite3.connect(db2_path)
                                cursor2 = conn2.cursor()
                                
                                # Check for local data in second database
                                cursor2.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='player_playtime'")
                                has_local_data2 = cursor2.fetchone() is not None
                                
                                if has_local_data2:
                                    cursor2.execute("SELECT total_playtime FROM player_playtime WHERE LOWER(username) = LOWER(?)", (player_username,))
                                    result2 = cursor2.fetchone()
                                    # Convert from seconds to hours
                                    playtime2 = result2[0] / 3600.0 if result2 else None
                                else:
                                    # Fallback to API data
                                    cursor2.execute("SELECT playtime FROM player_stats WHERE LOWER(username) = LOWER(?)", (player_username,))
                                    result2 = cursor2.fetchone()
                                    playtime2 = result2[0] if result2 else None
                                
                                conn2.close()
                                
                                if playtime1 is not None and playtime2 is not None:
                                    delta = playtime2 - playtime1
                                    date_key = db2_time.strftime('%Y-%m-%d')
                                    date_totals[date_key] += delta
                                    
                                    # Log data source for transparency
                                    data_source = "local" if has_local_data and has_local_data2 else "API"
                                    if i == 0:  # Only log once
                                        print(f"[RECRUITMENT] Using {data_source} playtime data for {player_username}")
                            except Exception as db_error:
                                print(f"Error querying database: {db_error}")
                                continue
                        
                        # Convert to list sorted by date
                        daily_deltas = []
                        for date_str, total_delta in sorted(date_totals.items()):
                            date_obj = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                            daily_deltas.append({
                                'date': date_obj,
                                'delta': total_delta
                            })
                        
                        # Generate chart if we have data
                        if daily_deltas:
                            playtime_chart_buffer = PlaytimeGraphGenerator.create_chart_from_deltas(
                                daily_deltas, 
                                player_username, 
                                days
                            )
            except Exception as e:
                print(f"Error fetching playtime from database: {e}")
                import traceback
                traceback.print_exc()
            
            # Calculate suspiciousness
            sus_data = SuspiciousnessCalculator.calculate(player_data)
            
            # Send player card as image
            player_card_file = discord.File(player_card_bytes, filename=f"player_{player_data.get('username', 'player')}.png")
            main_message = await recruitment_channel.send(
                content=f"New recruitment vote created!",
                file=player_card_file
            )
            
            # Create thread
            safe_username = player_data.get('username', username).replace('_', ' ')
            thread = await main_message.create_thread(
                name=f"New Member - {safe_username}",
                auto_archive_duration=1440
            )
            
            user_mention = self.message_context.author.mention if self.message_context else player_data.get('username', username)
            await main_message.edit(content=f"New recruitment vote created for {user_mention}! Go to {thread.mention} to vote.")
            
            # Send sus embed in thread
            if sus_data:
                sus_embed = create_sus_embed(player_data, sus_data)
                await thread.send(embed=sus_embed)
            
            # Send playtime chart in thread
            if playtime_chart_buffer:
                try:
                    file = discord.File(playtime_chart_buffer, filename='playtime.png')
                    await thread.send(file=file)
                except Exception as e:
                    print(f"Error sending chart: {e}")
            
            # Send context message with reactions
            if self.message_context:
                message_content = self.message_context.content or "(no text)"
                sent_message = await thread.send(f"```\n{message_content}\n```")
                
                try:
                    await sent_message.add_reaction("<:approve:820858331811020833>")
                    await sent_message.add_reaction("<:deny:820858264249958413>")
                except Exception as e:
                    print(f"Error adding reactions: {e}")
            
            user_mention = self.message_context.author.mention if self.message_context else "User"
            forward_messages = [
                f"Hello there {user_mention}. Thank you for applying! Your app has been forwarded to our jurors, you can expect to hear back from us within a day.",
                f"Hello there {user_mention}. Your app has been sent to our jurors! You can expect to hear back from us within a day.",
                f"Hello there {user_mention}. Your application is under review! You can expect a response within 24 hours.",
                f"Hello there {user_mention}. We've received your application and passed it along to our jurors for review, you should receive an update soon.",
                f"Hello there {user_mention}. Your application has been successfully submitted to our review jurors! Expect a response within the next day.",
                f"Hello there {user_mention}. Thanks for applying! Our jurors are reviewing your submission and will reach out with a decision shortly.",
                f"Hello there {user_mention}. Your application is now in the hands of our jurors, you'll hear from us once their review is complete.",
                f"Hello there {user_mention}. We appreciate your interest! Your application has been sent to the jury for final evaluation.",
                f"Hello there {user_mention}. Your application has entered the review stage! Expect to hear back within the next 24 hours.",
                f"Hello there {user_mention}. Our jurors are now reviewing your application, thank you for your patience during this process.",
                f"Hello there {user_mention}. Your submission has been forwarded to our review board, you'll receive an update soon!"
            ]
            print(f"[INFO] Thread URL: {thread.jump_url}")
            content = f"New recruitment vote created! [View thread]({thread.jump_url}) to vote.\n\n"
            content += f"**Forward message:**\n"
            content += f"```{random.choice(forward_messages)}```"
            await interaction.followup.send(content=content, ephemeral=True)
        
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            try:
                await interaction.followup.send(content=f"Error: {str(e)}", ephemeral=True)
            except:
                pass

# ============================================================================
# PERMISSION CHECKER
# ============================================================================
def check_user_has_required_role(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        print(f"No guild found for interaction")
        return False
    
    member = interaction.user
    if not member:
        print(f"No member found")
        return False
    
    if not hasattr(member, 'roles'):
        print(f"Member has no roles attribute")
        return False
    
    user_role_ids = [role.id for role in member.roles]
    print(f"User {member.name} has roles: {user_role_ids}")
    print(f"Required role ID: {REQUIRED_ROLE_ID}")
    
    has_role = any(role.id == REQUIRED_ROLE_ID for role in member.roles)
    print(f"Has required role: {has_role}")
    
    return has_role

# ============================================================================
# SETUP
# ============================================================================
def setup(bot, has_required_role=None, config=None):
    @bot.tree.context_menu(name="Recruitment Profile")
    async def recruitment(interaction: discord.Interaction, message: discord.Message):
        if not check_user_has_required_role(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this app.",
                ephemeral=True
            )
            return
        
        modal = RecruitmentModal()
        modal.message_context = message
        await interaction.response.send_modal(modal)
    
    print("[OK] Loaded recruitment command")