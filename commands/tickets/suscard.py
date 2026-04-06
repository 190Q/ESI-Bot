import discord
from discord import app_commands
import aiohttp
import os
from datetime import datetime, timezone
import math
from typing import Optional, Dict
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
import io

# Load environment variables
load_dotenv()

# Get Wynncraft API keys from environment
WYNNCRAFT_KEYS = [
    os.getenv('WYNNCRAFT_KEY_1'),
    os.getenv('WYNNCRAFT_KEY_2'),
    os.getenv('WYNNCRAFT_KEY_3')
]
WYNNCRAFT_KEYS = [key for key in WYNNCRAFT_KEYS if key]

class WynncraftAPI:
    """API handler with key rotation"""
    BASE_URL = "https://api.wynncraft.com/v3"
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

def sigmoid(x: float) -> float:
    """Sigmoid function for time calculation"""
    try:
        return 100 / (1 + math.exp(-0.1 * (x - 50)))
    except OverflowError:
        return 0.0 if x < 0 else 100.0

async def fetch_player_data(name_or_uuid: str) -> Optional[Dict]:
    """Fetch player data from Wynncraft API with authentication"""
    try:
        headers = WynncraftAPI._get_headers()
        async with aiohttp.ClientSession() as session:
            url = f"{WynncraftAPI.BASE_URL}/player/{name_or_uuid}?fullResult"
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 300:
                    data = await response.json()
                    return {"multiple": True, "players": data}
                return None
    except Exception as e:
        print(f"API Error: {e}")
        return None

def calculate_suspiciousness(player_data: Dict) -> Optional[Dict]:
    """Calculate suspiciousness with all metrics"""
    support_ranks = ['vip', 'vipplus', 'hero', 'heroplus', 'champion']
    
    try:
        # Extract data
        username = player_data.get('username', '')
        uuid = player_data.get('uuid', '')
        first_join = player_data.get('firstJoin')
        playtime = player_data.get('playtime', 0)
        global_data = player_data.get('globalData', {})
        total_level = global_data.get('totalLevel', 0)
        completed_quests = global_data.get('completedQuests', 0)
        support_rank = player_data.get('supportRank')
        veteran = player_data.get('veteran', False)
        
        # Character and raid analysis
        characters = player_data.get('characters', {})
        total_raid_count = 0
        
        if characters and isinstance(characters, dict):
            for char_uuid, char_data in characters.items():
                if not char_data or not isinstance(char_data, dict):
                    continue
                
                raids_data = char_data.get('raids', {})
                if raids_data and isinstance(raids_data, dict):
                    total_raid_count += raids_data.get('total', 0)
        
        # Calculate join suspiciousness
        if first_join:
            join_timestamp = datetime.fromisoformat(first_join.replace('Z', '+00:00')).timestamp()
            current_timestamp = datetime.now(timezone.utc).timestamp()
            two_years = 63072000  # seconds
            join_sus_value = max(0, (current_timestamp - join_timestamp - two_years) * -1) * 100 / two_years
        else:
            join_sus_value = 50.0
        
        # Calculate playtime suspiciousness
        playtime_sus_value = max(0, (playtime - 800) * -1) * 100 / 800
        
        # Calculate total level suspiciousness
        total_level_sus_value = max(0, (total_level - 250) * -1) * 100 / 250
        
        # Calculate quest suspiciousness
        quest_sus_value = max(0, (completed_quests - 150) * -1) * 100 / 150
        
        # Time spent calculation
        if first_join:
            join_date = datetime.fromisoformat(first_join.replace('Z', '+00:00'))
            days_since_join = max(1, (datetime.now(timezone.utc) - join_date).days)
            time_spent_percentage = (playtime / (days_since_join * 24)) * 100
            time_spent_sus_value = sigmoid(time_spent_percentage)
        else:
            time_spent_percentage = 100.0
            time_spent_sus_value = 100.0
        
        # Rank suspiciousness
        if support_rank in support_ranks:
            rank_index = support_ranks.index(support_rank)
            rank_sus_value = max(0, (rank_index - 2) * -1) * 100 / 2
        else:
            rank_sus_value = 100.0
        
        # Raid suspiciousness
        if total_raid_count >= 50:
            raids_sus_value = 0
        else:
            raids_sus_value = max(0, (50 - total_raid_count) * 100 / 50)
        
        # Overall calculation
        overall_sus = (join_sus_value + rank_sus_value + total_level_sus_value + 
                      playtime_sus_value + quest_sus_value + time_spent_sus_value +
                      raids_sus_value) / 7
        
        # Format rank display
        rank_display = 'No rank'
        if support_rank:
            if support_rank == 'vipplus':
                rank_display = 'VIP+'
            elif support_rank == 'heroplus':
                rank_display = 'HERO+'
            else:
                rank_display = support_rank.upper()
        if veteran:
            rank_display += ' (VET)'
        
        # Guild info
        guild_info = player_data.get('guild', {})
        guild_name = guild_info.get('name', 'None') if guild_info else 'None'
        guild_rank = guild_info.get('rank', 'None') if guild_info else 'None'
        
        return {
            'username': username,
            'uuid': uuid,
            'overall_sus': overall_sus,
            'join_date': first_join.split('T')[0] if first_join else 'Unknown',
            'join_sus': join_sus_value,
            'playtime': playtime,
            'playtime_sus': playtime_sus_value,
            'time_spent_percentage': time_spent_percentage,
            'time_spent_sus': time_spent_sus_value,
            'total_level': total_level,
            'total_level_sus': total_level_sus_value,
            'quests': completed_quests,
            'quests_sus': quest_sus_value,
            'rank': rank_display,
            'rank_sus': rank_sus_value,
            'raids': total_raid_count,
            'raids_sus': raids_sus_value,
            'guild_name': guild_name,
            'guild_rank': guild_rank
        }
    except Exception as e:
        print(f"Calculation error: {e}")
        import traceback
        traceback.print_exc()
        return None

class SusCardImageGenerator:
    """Generates sus card images"""
    
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
            title_font = ImageFont.truetype("arialbd.ttf", 24)
            label_font = ImageFont.truetype("arial.ttf", 12)
            value_font = ImageFont.truetype("arialbd.ttf", 14)
            percentage_font = ImageFont.truetype("arialbd.ttf", 13)
            chart_label_font = ImageFont.truetype("arialbd.ttf", 10)
        except:
            try:
                title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
                label_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
                value_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
                percentage_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
                chart_label_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 10)
            except:
                title_font = ImageFont.load_default()
                label_font = ImageFont.load_default()
                value_font = ImageFont.load_default()
                percentage_font = ImageFont.load_default()
                chart_label_font = ImageFont.load_default()
        
        return title_font, label_font, value_font, percentage_font, chart_label_font
    
    @classmethod
    async def generate(cls, sus_data: dict, skin_bytes: bytes = None):
        """Generate the complete sus card image"""
        # Card dimensions
        width, height = 1000, 600
        
        # Create base image
        img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        
        # Load and paste background image
        try:
            bg_img = Image.open('images/background.png')
            bg_img = bg_img.resize((width, height), Image.Resampling.LANCZOS)
            
            # Create rounded mask
            mask = Image.new('L', (width, height), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle([0, 0, width, height], radius=20, fill=255)
            
            # Apply dark overlay for readability
            overlay = Image.new('RGBA', (width, height), (20, 25, 35, 180))
            bg_img = Image.alpha_composite(bg_img.convert('RGBA'), overlay)
            
            img.paste(bg_img, (0, 0), mask)
        except Exception as e:
            print(f"Could not load background: {e}")
            # Fallback dark background
            draw_temp = ImageDraw.Draw(img)
            draw_temp.rounded_rectangle([0, 0, width, height], radius=20, fill='#1e2238')
        
        draw = ImageDraw.Draw(img, 'RGBA')
        
        # Load fonts
        title_font, label_font, value_font, percentage_font, chart_label_font = cls.load_fonts()
        
        # Left section - Player avatar and info
        left_x = 50
        top_y = 30
        
        # Draw player avatar (smaller size)
        avatar_size = 140
        avatar_y = top_y
        
        if skin_bytes:
            try:
                skin_img = Image.open(io.BytesIO(skin_bytes))
                if skin_img.mode != 'RGBA':
                    skin_img = skin_img.convert('RGBA')
                skin_img = skin_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
                
                # Create rounded mask
                mask = Image.new('L', (avatar_size, avatar_size), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.rounded_rectangle([0, 0, avatar_size, avatar_size], radius=12, fill=255)
                
                img.paste(skin_img, (left_x, avatar_y), mask)
            except Exception as e:
                print(f"Error loading skin: {e}")
                cls.draw_rounded_rectangle(draw, [left_x, avatar_y, left_x + avatar_size, avatar_y + avatar_size], 
                                         radius=12, fill='#2a3550', outline='#5dade2', width=3)
        else:
            cls.draw_rounded_rectangle(draw, [left_x, avatar_y, left_x + avatar_size, avatar_y + avatar_size], 
                                     radius=12, fill='#2a3550', outline='#5dade2', width=3)
        
        # Draw username below avatar
        username_y = avatar_y + avatar_size + 12
        
        # Calculate username centering
        username_bbox = draw.textbbox((0, 0), sus_data['username'], font=title_font)
        username_width = username_bbox[2] - username_bbox[0]
        username_x = left_x + (avatar_size - username_width) // 2
        
        draw.text((username_x, username_y), sus_data['username'], fill='#7ec8e3', font=title_font)
        
        # Draw rank below username with proper color
        rank_y = username_y + 35
        
        # Determine rank color
        rank_lower = sus_data['rank'].lower().replace(' (vet)', '')
        rank_colors = {
            'player': '#7ec8e3',
            'no rank': '#7ec8e3',
            'vip': '#55FF55',
            'vip+': '#5555FF',
            'hero': '#AA00AA',
            'hero+': '#FF55FF',
            'champion': '#FFAA00'
        }
        rank_color = rank_colors.get(rank_lower, '#7ec8e3')
        
        # Calculate rank centering
        rank_bbox = draw.textbbox((0, 0), sus_data['rank'], font=value_font)
        rank_width = rank_bbox[2] - rank_bbox[0]
        rank_x = left_x + (avatar_size - rank_width) // 2
        
        draw.text((rank_x, rank_y), sus_data['rank'], fill=rank_color, font=value_font)
        
        # Draw guild info box (positioned lower to avoid overlap)
        guild_y = rank_y + 35
        guild_box_width = avatar_size
        guild_box_height = 80
        
        guild_overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        guild_draw = ImageDraw.Draw(guild_overlay)
        guild_draw.rounded_rectangle(
            [left_x, guild_y, left_x + guild_box_width, guild_y + guild_box_height],
            radius=12,
            fill=(30, 35, 50, 170)
        )
        img.paste(guild_overlay, (0, 0), guild_overlay)
        
        guild_text_x = left_x + 10
        guild_text_y = guild_y + 10
        
        draw.text((guild_text_x, guild_text_y), "GUILD", fill='#999999', font=label_font)
        draw.text((guild_text_x, guild_text_y + 20), sus_data['guild_name'], fill='#7ec8e3', font=value_font)
        draw.text((guild_text_x, guild_text_y + 42), f"RANK: {sus_data['guild_rank']}", fill='#7ec8e3', font=label_font)
        
        # Draw bar chart in top right
        chart_width = 480
        chart_height = 280
        chart_x = width - chart_width - 25
        chart_y = top_y + 5
        
        # Create a new image for the chart
        chart_img = Image.new('RGBA', (chart_width, chart_height), (0, 0, 0, 0))
        chart_draw = ImageDraw.Draw(chart_img)
        
        # Draw chart background
        chart_draw.rounded_rectangle(
            [0, 0, chart_width, chart_height],
            radius=10,
            fill=(30, 35, 50, 170)
        )
        
        # Chart data
        labels = ['Join\nDate', 'Play\ntime', 'Time\nSpent', 'Total\nLevel', 'Quests', 'Rank', 'Raids']
        percentages = [
            sus_data['join_sus'],
            sus_data['playtime_sus'],
            sus_data['time_spent_sus'],
            sus_data['total_level_sus'],
            sus_data['quests_sus'],
            sus_data['rank_sus'],
            sus_data['raids_sus']
        ]
        
        # Bar colors matching the stats
        bar_colors = [
            (231, 76, 60),   # Join Date - red
            (46, 204, 113),  # Playtime - green
            (243, 156, 18),  # Time Spent - orange
            (155, 89, 182),  # Total Level - purple
            (26, 188, 156),  # Quests - teal
            (233, 30, 99),   # Rank - pink
            (93, 173, 226),  # Raids - blue
        ]
        
        # Bar chart dimensions
        bar_area_x = 60
        bar_area_y = 40
        bar_area_width = chart_width - 80
        bar_area_height = chart_height - 80
        
        num_bars = len(labels)
        bar_spacing = 8
        total_spacing = bar_spacing * (num_bars - 1)
        bar_width = (bar_area_width - total_spacing) // num_bars
        
        # Draw grid lines (horizontal)
        for i in range(6):
            y = bar_area_y + bar_area_height - (i * bar_area_height // 5)
            chart_draw.line(
                [(bar_area_x, y), (bar_area_x + bar_area_width, y)],
                fill=(100, 100, 100, 100),
                width=1
            )
            # Draw percentage labels on the left
            perc_text = f"{i * 20}%"
            chart_draw.text((10, y - 6), perc_text, fill='#888', font=chart_label_font)
        
        # Draw bars
        for i, (label, percentage, color) in enumerate(zip(labels, percentages, bar_colors)):
            bar_x = bar_area_x + i * (bar_width + bar_spacing)
            bar_height = int((percentage / 100) * bar_area_height)
            bar_y = bar_area_y + bar_area_height - bar_height
            
            # Draw bar with gradient effect
            for j in range(bar_height):
                alpha = 200 + int(55 * (j / max(bar_height, 1)))
                gradient_color = color + (min(alpha, 255),)
                chart_draw.rectangle(
                    [bar_x, bar_y + j, bar_x + bar_width, bar_y + j + 1],
                    fill=gradient_color
                )
            
            # Draw bar outline
            chart_draw.rectangle(
                [bar_x, bar_y, bar_x + bar_width, bar_area_y + bar_area_height],
                outline=color,
                width=2
            )
            
            # Draw percentage on top of bar
            perc_text = f"{percentage:.1f}%"
            bbox = chart_label_font.getbbox(perc_text)
            text_width = bbox[2] - bbox[0]
            text_x = bar_x + (bar_width - text_width) // 2
            text_y = max(bar_y - 15, bar_area_y - 15)
            chart_draw.text((text_x, text_y), perc_text, fill='white', font=chart_label_font)
            
            # Draw label below bar
            label_lines = label.split('\n')
            label_y = bar_area_y + bar_area_height + 8
            for line_idx, line in enumerate(label_lines):
                bbox = chart_label_font.getbbox(line)
                text_width = bbox[2] - bbox[0]
                text_x = bar_x + (bar_width - text_width) // 2
                chart_draw.text((text_x, label_y + line_idx * 12), line, fill='#ccc', font=chart_label_font)
        
        # Paste chart onto main image
        img.paste(chart_img, (chart_x, chart_y), chart_img)
        
        # Stats grid - 4 columns x 2 rows at the bottom
        stat_width = 230
        stat_height = 80
        gap_x = 15
        gap_y = 15
        stats_start_x = left_x
        stats_start_y = height - (2 * stat_height + gap_y + 30)
        
        stats = [
            ('OVERALL SUS', f"{sus_data['overall_sus']:.2f}%", 'Suspiciousness', '#5dade2'),
            ('JOIN DATE', sus_data['join_date'], f"{sus_data['join_sus']:.2f}%", '#e74c3c'),
            ('PLAYTIME', f"{sus_data['playtime']:.0f}h", f"{sus_data['playtime_sus']:.2f}%", '#2ecc71'),
            ('TIME SPENT', f"{sus_data['time_spent_percentage']:.1f}%", f"{sus_data['time_spent_sus']:.2f}%", '#f39c12'),
            ('TOTAL LEVEL', f"{sus_data['total_level']:,}", f"{sus_data['total_level_sus']:.2f}%", '#9b59b6'),
            ('QUESTS', str(sus_data['quests']), f"{sus_data['quests_sus']:.2f}%", '#1abc9c'),
            ('RANK', sus_data['rank'], f"{sus_data['rank_sus']:.2f}%", '#e91e63'),
            ('RAIDS', str(sus_data['raids']), f"{sus_data['raids_sus']:.2f}%", '#5dade2'),
        ]
        
        for idx, (label, value, percentage, color) in enumerate(stats):
            col = idx % 4
            row = idx // 4
            
            x = stats_start_x + col * (stat_width + gap_x)
            y = stats_start_y + row * (stat_height + gap_y)
            
            # Draw stat box
            stat_overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
            stat_draw = ImageDraw.Draw(stat_overlay)
            stat_draw.rounded_rectangle(
                [x, y, x + stat_width, y + stat_height],
                radius=10,
                fill=(30, 35, 50, 170)
            )
            img.paste(stat_overlay, (0, 0), stat_overlay)
            
            # Colored left border
            draw.rounded_rectangle([x, y, x + 4, y + stat_height], radius=2, fill=color)
            
            # Draw text with adjusted positioning to prevent cutoff
            draw.text((x + 15, y + 10), label, fill='#aaa', font=label_font)
            
            # Check if value text is too long and might get cut off
            try:
                bbox = value_font.getbbox(value)
                text_width = bbox[2] - bbox[0]
                # If text is too wide, use a smaller font or truncate
                if text_width > (stat_width - 25):
                    # Try with smaller font
                    smaller_value_font = ImageFont.truetype("arialbd.ttf", 14) if value_font != ImageFont.load_default() else value_font
                    draw.text((x + 15, y + 30), value, fill='white', font=smaller_value_font)
                else:
                    draw.text((x + 15, y + 30), value, fill='white', font=value_font)
            except:
                draw.text((x + 15, y + 30), value, fill='white', font=value_font)
            
            draw.text((x + 15, y + 58), percentage, fill='#5dade2', font=percentage_font)
        
        # Save to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        
        return img_bytes

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    @bot.tree.command(name="suscard", description="Generate a visual sus card for a player")
    @app_commands.describe(username="The name of who you want to check")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def suscard(interaction: discord.Interaction, username: str):
        await interaction.response.defer()
        
        try:
            # Fetch player data
            player_data = await fetch_player_data(username)
            
            if not player_data:
                await interaction.followup.send(
                    f"❌ Could not find player `{username}`",
                    ephemeral=True
                )
                return
            
            if player_data.get('multiple'):
                await interaction.followup.send(
                    f"❌ Multiple players found for `{username}`. Please be more specific.",
                    ephemeral=True
                )
                return
            
            # Calculate sus data
            sus_data = calculate_suspiciousness(player_data)
            
            if not sus_data:
                await interaction.followup.send(
                    "❌ Could not calculate suspiciousness",
                    ephemeral=True
                )
                return
            
            # Fetch player skin
            skin_bytes = None
            if sus_data['uuid']:
                skin_bytes = await WynncraftAPI.fetch_player_skin(sus_data['uuid'])
            
            # Generate image
            img_bytes = await SusCardImageGenerator.generate(sus_data, skin_bytes)
            
            # Send image
            file = discord.File(img_bytes, filename=f"suscard_{sus_data['username']}.png")
            await interaction.followup.send(file=file)
            
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error generating sus card: {str(e)}",
                ephemeral=True
            )
            print(f"[ERROR] Sus card command failed: {e}")
            import traceback
            traceback.print_exc()
    
    print("[OK] Loaded suscard command")