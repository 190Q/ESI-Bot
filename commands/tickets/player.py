import discord
from discord import app_commands
from discord.ext import commands
import os
import aiohttp
import asyncio
from PIL import Image, ImageDraw, ImageFont
import io
from dotenv import load_dotenv

# ============================================================================
# CONFIGURATION
# ============================================================================
load_dotenv()

WYNNCRAFT_KEYS = []
key_index = 1
while True:
    key = os.getenv(f'WYNNCRAFT_KEY_{key_index}')
    if key is None:
        break
    # Filter out placeholder keys
    if not key.startswith('your_key_'):
        WYNNCRAFT_KEYS.append(key)
    key_index += 1

print(f"[INFO] Loaded {len(WYNNCRAFT_KEYS)} valid API keys")

# ============================================================================
# API HANDLER
# ============================================================================
class WynncraftProfileAPI:
    """Handles Wynncraft API requests"""
    
    BASE_URL = "https://api.wynncraft.com/v3/player"
    _current_key_index = 0
    
    @classmethod
    def _get_headers(cls):
        """Get headers with rotating API key"""
        if not WYNNCRAFT_KEYS:
            return {}
        
        key = WYNNCRAFT_KEYS[cls._current_key_index % len(WYNNCRAFT_KEYS)]
        cls._current_key_index += 1
        return {'apikey': key}
    
    @classmethod
    async def fetch_guild(cls, guild_name: str):
        """Fetch guild data from Wynncraft API"""
        if not guild_name or not guild_name.strip():
            return None
            
        try:
            headers = cls._get_headers()
            
            async with aiohttp.ClientSession() as session:
                url = f"https://api.wynncraft.com/v3/guild/{guild_name.strip()}"
                timeout = aiohttp.ClientTimeout(total=10)
                
                async with session.get(url, headers=headers, timeout=timeout) as response:
                    if response.status == 200:
                        data = await response.json()
                        # Validate that we got proper guild data
                        if isinstance(data, dict) and 'name' in data:
                            return data
                        return None
                    elif response.status == 404:
                        return None
                    else:
                        print(f"API Error {response.status} for guild: {guild_name}")
                        return None
        except asyncio.TimeoutError:
            print(f"Timeout fetching guild: {guild_name}")
            return None
        except Exception as e:
            print(f"Error fetching guild {guild_name}: {e}")
            return None

    @classmethod
    async def _fetch_single_player(cls, session, player_id, headers, timeout):
        """Fetch a single player by username or UUID, with ?fullResult fallback"""
        for use_full_result in [True, False]:
            suffix = "?fullResult" if use_full_result else ""
            url = f"{cls.BASE_URL}/{player_id}{suffix}"
            try:
                async with session.get(url, headers=headers, timeout=timeout) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, dict) and 'username' in data:
                            if not use_full_result:
                                print(f"[INFO] Used non-fullResult fallback for: {player_id}")
                            return data
                        continue
                    elif response.status in (300, 404):
                        # Return status so caller can handle these specifically
                        data = await response.json() if response.status == 300 else None
                        return {'_status': response.status, '_data': data}
                    else:
                        print(f"API Error {response.status} for player: {player_id} (fullResult={use_full_result})")
                        continue
            except asyncio.TimeoutError:
                print(f"Timeout fetching player: {player_id} (fullResult={use_full_result})")
                continue
            except Exception as e:
                print(f"Error fetching player {player_id} (fullResult={use_full_result}): {e}")
                continue
        return None

    @classmethod
    async def fetch_player(cls, username: str):
        """Fetch player data with full result, falling back to without ?fullResult"""
        if not username or not username.strip():
            return None
            
        try:
            headers = cls._get_headers()
            
            async with aiohttp.ClientSession() as session:
                timeout = aiohttp.ClientTimeout(total=10)
                
                result = await cls._fetch_single_player(session, username.strip(), headers, timeout)
                
                if result is None:
                    return None
                
                # Handle special status codes returned by _fetch_single_player
                if isinstance(result, dict) and '_status' in result:
                    status = result['_status']
                    
                    if status == 404:
                        return None
                    
                    if status == 300:
                        # Multiple usernames found - pick the most recent one
                        data = result['_data']
                        players_dict = data.get('objects', {}) if data else {}
                        
                        if not players_dict:
                            print(f"No valid players found for username: {username}")
                            return None
                        
                        print(f"[INFO] Multiple usernames found for {username}: {data}")
                        
                        # Fetch full data for each UUID to get lastJoin
                        most_recent_player = None
                        most_recent_last_join = None
                        
                        for player_uuid in players_dict.keys():
                            player_data = await cls._fetch_single_player(session, player_uuid, headers, timeout)
                            
                            # Skip if fetch failed or returned a special status
                            if not player_data or (isinstance(player_data, dict) and '_status' in player_data):
                                print(f"[WARN] Could not fetch player data for UUID {player_uuid}")
                                continue
                            
                            last_join = player_data.get('lastJoin')
                            if last_join and (most_recent_last_join is None or last_join > most_recent_last_join):
                                most_recent_last_join = last_join
                                most_recent_player = player_data
                        
                        if most_recent_player:
                            print(f"[INFO] Selected player {most_recent_player.get('username')} with UUID {most_recent_player.get('uuid')} (most recent lastJoin: {most_recent_last_join})")
                            return most_recent_player
                        else:
                            print(f"Could not determine the correct player for username: {username}")
                            # Fallback to return multiple players info for compatibility
                            return {"multiple": True, "players": data}
                
                return result
        except Exception as e:
            print(f"Error fetching player {username}: {e}")
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
                        print(f"Skin fetch failed with status {response.status}")
                        return None
        except Exception as e:
            print(f"Error fetching skin: {e}")
            return None

# ============================================================================
# IMAGE GENERATOR
# ============================================================================
class PlayerStatsImageGenerator:
    """Generates player stats card images"""
    
    @staticmethod
    def format_large_number(num: int) -> str:
        """Format large numbers with K, M, B, T suffixes"""
        try:
            num = int(num)
        except (ValueError, TypeError):
            return "0"
        
        if num < 1000:
            return str(num)
        elif num < 1_000_000:
            return f"{num / 1_000:.3f}k"
        elif num < 1_000_000_000:
            return f"{num / 1_000_000:.3f}M"
        elif num < 1_000_000_000_000:
            return f"{num / 1_000_000_000:.3f}B"
        else:
            return f"{num / 1_000_000_000_000:.3f}T"
    
    @staticmethod
    def draw_rounded_rectangle(draw, xy, radius, fill, outline=None, width=1):
        """Helper function to draw rounded rectangles"""
        x1, y1, x2, y2 = xy
        draw.rectangle([x1 + radius, y1, x2 - radius, y2], fill=fill)
        draw.rectangle([x1, y1 + radius, x2, y2 - radius], fill=fill)
        draw.pieslice([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=fill)
        draw.pieslice([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=fill)
        draw.pieslice([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=fill)
        draw.pieslice([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=fill)
        if outline:
            draw.arc([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=outline, width=width)
            draw.arc([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=outline, width=width)
            draw.arc([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=outline, width=width)
            draw.arc([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=outline, width=width)
            draw.line([x1 + radius, y1, x2 - radius, y1], fill=outline, width=width)
            draw.line([x1 + radius, y2, x2 - radius, y2], fill=outline, width=width)
            draw.line([x1, y1 + radius, x1, y2 - radius], fill=outline, width=width)
            draw.line([x2, y1 + radius, x2, y2 - radius], fill=outline, width=width)
    
    @classmethod
    def load_fonts(cls):
        """Load fonts with fallbacks"""
        try:
            username_font = ImageFont.truetype("arialbd.ttf", 28)
            title_font = ImageFont.truetype("arialbd.ttf", 16)
            label_font = ImageFont.truetype("arial.ttf", 13)
            value_font = ImageFont.truetype("arialbd.ttf", 15)
            big_number_font = ImageFont.truetype("arialbd.ttf", 22)
            small_font = ImageFont.truetype("arial.ttf", 12)
        except:
            try:
                username_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
                title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
                label_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
                value_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 15)
                big_number_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
                small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
            except:
                username_font = ImageFont.load_default()
                title_font = ImageFont.load_default()
                label_font = ImageFont.load_default()
                value_font = ImageFont.load_default()
                big_number_font = ImageFont.load_default()
                small_font = ImageFont.load_default()
        
        return username_font, title_font, label_font, value_font, big_number_font, small_font
    
    @classmethod
    async def generate(cls, player_data: dict, skin_bytes: bytes = None):
        """Generate the complete player stats card image"""
        if not player_data or not isinstance(player_data, dict):
            raise ValueError("Invalid player data provided")
        
        if 'username' not in player_data:
            raise ValueError("Player data missing username field")
        
        # Card dimensions
        width, height = 850, 300
        
        # Extract player data
        username = player_data.get('username', 'Unknown')
        global_data = player_data.get('globalData', {})
        
        if not isinstance(global_data, dict):
            global_data = {}
        
        # Get stats
        rank = player_data.get('supportRank', player_data.get('rank', 'Player'))
        wars = global_data.get('wars', 0)
        total_level = global_data.get('totalLevel', 0)
        playtime = player_data.get('playtime', 0)
        
        # Ensure numeric values
        try:
            wars = int(wars) if wars else 0
            total_level = int(total_level) if total_level else 0
            playtime = float(playtime) if playtime else 0.0
        except (ValueError, TypeError):
            wars = 0
            total_level = 0
            playtime = 0.0
        
        playtime_hours = playtime
        
        # Guild info
        guild_data = player_data.get('guild', {})
        if not isinstance(guild_data, dict):
            guild_data = {}
        
        guild_name = guild_data.get('name', 'None')
        guild_prefix = guild_data.get('prefix', 'N/A')
        guild_rank = guild_data.get('rank', 'None')
        
        # Create base image
        img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        
        # Create dark background
        try:
            bg_img = Image.open('images/background.png')
            bg_img = bg_img.resize((width, height), Image.Resampling.LANCZOS)
            
            mask = Image.new('L', (width, height), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle([0, 0, width, height], radius=15, fill=255)
            
            overlay = Image.new('RGBA', (width, height), (15, 15, 20, 230))
            bg_img = Image.alpha_composite(bg_img.convert('RGBA'), overlay)
            
            img.paste(bg_img, (0, 0), mask)
        except Exception as e:
            print(f"Could not load background: {e}")
            draw_temp = ImageDraw.Draw(img)
            draw_temp.rounded_rectangle([0, 0, width, height], radius=15, fill='#0f0f14')
        
        draw = ImageDraw.Draw(img, 'RGBA')
        
        # Load fonts
        username_font, title_font, label_font, value_font, big_number_font, small_font = cls.load_fonts()
        
        # Layout
        left_margin = 25
        top_margin = 25
        avatar_size = 160
        
        # Draw player avatar
        if skin_bytes:
            try:
                skin_img = Image.open(io.BytesIO(skin_bytes))
                if skin_img.mode != 'RGBA':
                    skin_img = skin_img.convert('RGBA')
                skin_img = skin_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
                
                mask = Image.new('L', (avatar_size, avatar_size), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.rounded_rectangle([0, 0, avatar_size, avatar_size], radius=10, fill=255)
                
                img.paste(skin_img, (left_margin, top_margin), mask)
            except Exception as e:
                print(f"Error loading skin: {e}")
                cls.draw_rounded_rectangle(draw, [left_margin, top_margin, left_margin + avatar_size, top_margin + avatar_size], 
                                         radius=10, fill='#1a1a24', outline='#3a3a44', width=2)
        else:
            cls.draw_rounded_rectangle(draw, [left_margin, top_margin, left_margin + avatar_size, top_margin + avatar_size], 
                                     radius=10, fill='#1a1a24', outline='#3a3a44', width=2)
        
        # Draw username and rank below avatar
        username_y = top_margin + avatar_size + 15
        
        # Rank colors
        rank_lower = str(rank).lower()
        rank_colors = {
            'player': '#7ec8e3',
            'vip': '#55FF55',
            'vipplus': '#5555FF',
            'hero': '#AA00AA',
            'heroplus': '#FF55FF',
            'champion': '#FFAA00'
        }
        
        if rank_lower == 'vipplus':
            rank_text = 'VIP+'
        elif rank_lower == 'heroplus':
            rank_text = 'HERO+'
        elif rank_lower == 'champion':
            rank_text = 'CHAMPION'
        else:
            rank_text = str(rank).upper()
        
        rank_color = rank_colors.get(rank_lower, '#7ec8e3')
        
        # Draw rank badge
        rank_bbox = draw.textbbox((0, 0), rank_text, font=label_font)
        rank_width = rank_bbox[2] - rank_bbox[0]
        rank_x = left_margin + (avatar_size - rank_width) // 2

        draw.text((rank_x, username_y), rank_text, fill=rank_color, font=label_font)

        # Draw username with dynamic font sizing
        max_username_width = avatar_size - 10  # 5px padding on each side
        username_font_to_use = username_font
        username_bbox = draw.textbbox((0, 0), username, font=username_font_to_use)
        username_width = username_bbox[2] - username_bbox[0]

        # If username is too wide, scale down the font
        if username_width > max_username_width:
            font_size = 28
            while username_width > max_username_width and font_size > 12:
                font_size -= 1
                try:
                    username_font_to_use = ImageFont.truetype("arialbd.ttf", font_size)
                except:
                    try:
                        username_font_to_use = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
                    except:
                        username_font_to_use = ImageFont.load_default()
                        break
                username_bbox = draw.textbbox((0, 0), username, font=username_font_to_use)
                username_width = username_bbox[2] - username_bbox[0]

        username_x = left_margin + (avatar_size - username_width) // 2

        draw.text((username_x, username_y + 22), username, fill='#ffffff', font=username_font_to_use)
        
        # Top row stats (Warcount and Guild XP)
        stat_start_x = left_margin + avatar_size + 30
        stat_y = top_margin
        stat_width = 180
        stat_height = 85
        stat_gap = 15
        
        # Warcount box
        war_box_overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        war_box_draw = ImageDraw.Draw(war_box_overlay)
        war_box_draw.rounded_rectangle(
            [stat_start_x, stat_y, stat_start_x + stat_width, stat_y + stat_height],
            radius=10,
            fill=(25, 25, 35, 200)
        )
        img.paste(war_box_overlay, (0, 0), war_box_overlay)
        
        # Warcount title and value
        draw.text((stat_start_x + 15, stat_y + 15), "Warcount", fill='#ffffff', font=title_font)
        draw.text((stat_start_x + 15, stat_y + 45), str(wars), fill='#e74c3c', font=big_number_font)
        
        # Guild XP box
        gxp_x = stat_start_x + stat_width + stat_gap
        gxp_box_overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        gxp_box_draw = ImageDraw.Draw(gxp_box_overlay)
        gxp_box_draw.rounded_rectangle(
            [gxp_x, stat_y, gxp_x + stat_width, stat_y + stat_height],
            radius=10,
            fill=(25, 25, 35, 200)
        )
        img.paste(gxp_box_overlay, (0, 0), gxp_box_overlay)
        
        # Guild XP title and value
        guild_data = await WynncraftProfileAPI.fetch_guild(guild_name)

        if not guild_data:
            # Handle case where guild wasn't found
            contribution = 0
            guild_xp_display = "N/A"
        else:
            def get_player_guild_contribution(guild_data: dict, username: str) -> int:
                """Get a player's contribution to the guild"""
                members = guild_data.get('members', {})
                
                # Search through all member roles (owner, chief, strategist, captain, recruiter, recruit)
                for role, role_members in members.items():
                    if role == 'total':  # Skip the total count
                        continue
                    if isinstance(role_members, dict):
                        if username in role_members:
                            return role_members[username].get('contributed', 0)
                
                return 0
            
            contribution = get_player_guild_contribution(guild_data, username)
            guild_xp_display = cls.format_large_number(contribution)

        draw.text((gxp_x + 15, stat_y + 15), "Guild XP", fill='#ffffff', font=title_font)
        draw.text((gxp_x + 15, stat_y + 45), f"{guild_xp_display}", fill='#2ecc71', font=big_number_font)

        # Player last seen box (top right)
        last_seen_x = gxp_x + stat_width + stat_gap
        last_seen_width = width - last_seen_x - 25
        
        last_seen_overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        last_seen_draw = ImageDraw.Draw(last_seen_overlay)
        last_seen_draw.rounded_rectangle(
            [last_seen_x, stat_y, last_seen_x + last_seen_width, stat_y + stat_height],
            radius=10,
            fill=(25, 25, 35, 200)
        )
        img.paste(last_seen_overlay, (0, 0), last_seen_overlay)
        
        # Last seen text
        draw.text((last_seen_x + 15, stat_y + 15), "Player last seen:", fill='#ffffff', font=label_font)
        
        # Get last join date
        last_join = player_data.get('lastJoin', '')
        if last_join:
            from datetime import datetime
            try:
                dt = datetime.fromisoformat(last_join.replace('Z', '+00:00'))
                last_seen_str = dt.strftime("%H:%M  %m/%d/%Y")
            except:
                last_seen_str = "Unknown"
        else:
            last_seen_str = "Unknown"
        
        draw.text((last_seen_x + 15, stat_y + 40), last_seen_str, fill='#999999', font=value_font)
        
        # Bottom section: Guild and Stats
        bottom_y = stat_y + stat_height + stat_gap
        bottom_height = height - bottom_y - 25

        # Guild box - make it more compact with fixed height
        guild_width = stat_width + 80
        guild_height = 70  # Reduced height to make room for playtime box
        guild_overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        guild_draw = ImageDraw.Draw(guild_overlay)
        guild_draw.rounded_rectangle(
            [stat_start_x, bottom_y, stat_start_x + guild_width, bottom_y + guild_height],
            radius=10,
            fill=(25, 25, 35, 200)
        )
        img.paste(guild_overlay, (0, 0), guild_overlay)

        # Guild title
        draw.text((stat_start_x + 15, bottom_y + 15), "Guild", fill='#ffffff', font=title_font)

        # Guild name with prefix
        guild_display = f"{guild_name} [{guild_prefix}]" if guild_prefix != "N/A" else guild_name
        draw.text((stat_start_x + 15, bottom_y + 38), guild_display, fill='#f39c12', font=small_font)

        # Guild rank
        draw.text((stat_start_x + 15, bottom_y + 53), f"Rank: {guild_rank}", fill='#ecf0f1', font=small_font)

        # Total Playtime box - below guild box
        playtime_y = bottom_y + guild_height + stat_gap
        playtime_height = bottom_height - guild_height - stat_gap
        playtime_overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        playtime_draw = ImageDraw.Draw(playtime_overlay)
        playtime_draw.rounded_rectangle(
            [stat_start_x, playtime_y, stat_start_x + guild_width, playtime_y + playtime_height],
            radius=10,
            fill=(25, 25, 35, 200)
        )
        img.paste(playtime_overlay, (0, 0), playtime_overlay)

        # Playtime title and value
        draw.text((stat_start_x + 15, playtime_y + 10), "Total Playtime", fill='#ffffff', font=title_font)
        draw.text((stat_start_x + 15, playtime_y + 35), f"{playtime_hours:.1f}h", fill='#3498db', font=big_number_font)

        # Stats box - keeps full bottom_height
        stats_x = stat_start_x + guild_width + stat_gap
        stats_width = width - stats_x - 25

        stats_overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        stats_draw = ImageDraw.Draw(stats_overlay)
        stats_draw.rounded_rectangle(
            [stats_x, bottom_y, stats_x + stats_width, bottom_y + bottom_height],  # Use bottom_height here
            radius=10,
            fill=(25, 25, 35, 200)
        )
        img.paste(stats_overlay, (0, 0), stats_overlay)

        # Stats title
        draw.text((stats_x + 15, bottom_y + 15), "Stats", fill='#ffffff', font=title_font)

        # Stats content - adjusted spacing
        stats_content_y = bottom_y + 40
        line_height = 22

        stats_list = [
            ("Total Levels: ", f"{total_level} Levels"),
            ("Mobs Killed: ", f"{global_data.get('mobsKilled', 0):,} Mobs"),
            ("Chests Opened: ", f"{global_data.get('chestsFound', 0):,} Chests"),
            ("Quests Completed: ", f"{global_data.get('completedQuests', 0)} Quests"),
            ("Dungeons Completed: ", f"{global_data.get('dungeons', {}).get('total', 0)} Dungeons")
        ]

        for i, (label, value) in enumerate(stats_list):
            y_pos = stats_content_y + (i * line_height)
            draw.text((stats_x + 15, y_pos), label, fill='#95a5a6', font=small_font)
            draw.text((stats_x + 155, y_pos), value, fill='#ecf0f1', font=small_font)
        
        # Save to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        
        return img_bytes

# ============================================================================
# PLAYER STATS COG
# ============================================================================
class PlayerStatsCog(commands.Cog):
    """Discord cog for player statistics card generation"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @app_commands.command(name="player", description="Generate a player's statistics card")
    @app_commands.describe(user="The Wynncraft player username")
    async def player(self, interaction: discord.Interaction, user: str):
        """Generate a player statistics card image"""
        await interaction.response.defer()
        
        try:
            # Validate username
            if not user or not user.strip():
                await interaction.followup.send(
                    "❌ Please provide a valid username.",
                    ephemeral=True
                )
                return
            
            # Fetch player data from Wynncraft API
            player_data = await WynncraftProfileAPI.fetch_player(user)
            
            # Check if data was received
            if player_data is None:
                await interaction.followup.send(
                    f"❌ Could not find player `{user}` on Wynncraft.",
                    ephemeral=True
                )
                return
            
            # Check for multiple players
            if isinstance(player_data, dict) and player_data.get('multiple'):
                await interaction.followup.send(
                    f"❌ Multiple players found for `{user}`. Please be more specific.",
                    ephemeral=True
                )
                return
            
            # Verify we have valid player data structure
            if not isinstance(player_data, dict):
                await interaction.followup.send(
                    f"❌ Invalid data format received for `{user}`.",
                    ephemeral=True
                )
                return
            
            if 'username' not in player_data:
                await interaction.followup.send(
                    f"❌ Invalid player data received for `{user}`. Missing username field.",
                    ephemeral=True
                )
                return
            
            # Get player info
            player_name = player_data.get('username', user)
            player_uuid = player_data.get('uuid', '')
            
            # Fetch player skin using UUID
            skin_bytes = None
            if player_uuid:
                skin_bytes = await WynncraftProfileAPI.fetch_player_skin(player_uuid)
            
            # Generate the image
            img_bytes = await PlayerStatsImageGenerator.generate(player_data, skin_bytes)
            
            if img_bytes is None:
                await interaction.followup.send(
                    f"❌ Failed to generate player card for `{player_name}`.",
                    ephemeral=True
                )
                return
            
            # Send the image file
            file = discord.File(img_bytes, filename=f"player_{player_name}.png")
            await interaction.followup.send(file=file)
            
        except ValueError as ve:
            await interaction.followup.send(
                content=f"❌ Invalid data: {str(ve)}",
                ephemeral=True
            )
            print(f"[ERROR] Validation error: {ve}")
            
        except Exception as e:
            await interaction.followup.send(
                content=f"❌ Error generating player card: {str(e)}",
                ephemeral=True
            )
            print(f"[ERROR] Player stats command failed: {e}")
            import traceback
            traceback.print_exc()

# ============================================================================
# SETUP
# ============================================================================
async def setup(bot):
    """Setup function for the cog"""
    await bot.add_cog(PlayerStatsCog(bot))