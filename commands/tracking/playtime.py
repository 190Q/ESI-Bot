import discord
from discord import app_commands
import sqlite3
import os
import aiohttp
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from io import BytesIO
from pathlib import Path
import statistics
from utils.permissions import has_roles

OWNER_ID = int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0
WYNNCRAFT_KEY_11 = os.getenv('WYNNCRAFT_KEY_11')
REQUIRED_ROLES = []

# Database paths
DB_FOLDER = Path(__file__).resolve().parent.parent.parent / "databases"
PLAYTIME_TRACKING_FOLDER = DB_FOLDER / "playtime_tracking"


async def check_player_online(username: str) -> Tuple[bool, Optional[str]]:
    """Check if a player is currently online.
    
    Returns (is_online, server_name) tuple.
    """
    if not WYNNCRAFT_KEY_11:
        return False, None
    
    url = "https://api.wynncraft.com/v3/player"
    headers = {'Authorization': f'Bearer {WYNNCRAFT_KEY_11}'}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    players = data.get("players", {})
                    
                    # Case-insensitive search
                    for player_name, server in players.items():
                        if player_name.lower() == username.lower():
                            return True, server
                    
                    return False, None
                else:
                    return False, None
    except (asyncio.TimeoutError, Exception):
        return False, None


async def check_player_exists(username: str) -> Tuple[bool, Optional[str], bool, Optional[str]]:
    """Check if a player exists on Wynncraft.
    
    Returns (exists, correct_username, is_online, server) tuple.
    - exists: Whether the player exists on Wynncraft
    - correct_username: The correct username (may differ in case or if multiple accounts)
    - is_online: Whether the player is currently online
    - server: The server name if online
    """
    if not WYNNCRAFT_KEY_11:
        return True, username, False, None  # Assume exists if we can't check
    
    url = f"https://api.wynncraft.com/v3/player/{username}"
    headers = {'Authorization': f'Bearer {WYNNCRAFT_KEY_11}'}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                data = await response.json()
                
                if response.status == 200:
                    # Player exists
                    correct_username = data.get("username", username)
                    is_online = data.get("online", False)
                    server = data.get("server") if is_online else None
                    return True, correct_username, is_online, server
                
                elif response.status == 300:
                    # Multiple accounts found - pick the one with most recent lastJoin
                    error = data.get("error")
                    if error == "MultipleObjectsReturned":
                        objects = data.get("objects", {})
                        if not objects:
                            return False, username, False, None
                        
                        # Find the UUID with most recent lastJoin
                        best_uuid = None
                        best_last_join = None
                        
                        for uuid, player_data in objects.items():
                            # Need to fetch full player data to get lastJoin
                            player_url = f"https://api.wynncraft.com/v3/player/{uuid}"
                            async with session.get(player_url, headers=headers, timeout=10) as player_response:
                                if player_response.status == 200:
                                    player_full_data = await player_response.json()
                                    last_join = player_full_data.get("lastJoin")
                                    
                                    if last_join:
                                        if best_last_join is None or last_join > best_last_join:
                                            best_last_join = last_join
                                            best_uuid = uuid
                                            correct_username = player_full_data.get("username", username)
                                            is_online = player_full_data.get("online", False)
                                            server = player_full_data.get("server") if is_online else None
                        
                        if best_uuid:
                            return True, correct_username, is_online, server
                        
                        return False, username, False, None
                
                elif response.status == 404:
                    # Player not found
                    return False, username, False, None
                
                else:
                    # Unknown error, assume exists
                    return True, username, False, None
                    
    except (asyncio.TimeoutError, Exception) as e:
        print(f"[PLAYTIME] Error checking player existence: {e}")
        return True, username, False, None  # Assume exists if we can't check


def get_available_days():
    """Get list of available day folders sorted by date (oldest to newest)"""
    if not PLAYTIME_TRACKING_FOLDER.exists():
        return []
    
    day_folders = []
    for folder in PLAYTIME_TRACKING_FOLDER.iterdir():
        if folder.is_dir() and folder.name.startswith("playtime_"):
            try:
                date_str = folder.name.replace("playtime_", "")
                date_obj = datetime.strptime(date_str, "%d-%m-%Y")
                day_folders.append((folder, date_obj))
            except ValueError:
                continue
    
    # Sort by date (oldest first)
    day_folders.sort(key=lambda x: x[1])
    return day_folders


def get_final_playtime_for_day(day_folder: Path, username: str) -> Optional[int]:
    """Get the final playtime for a user from a day's backup folder.
    
    Returns the playtime from the most recent backup of that day.
    """
    if not day_folder.exists():
        return None
    
    # Get all .db files in the folder, sorted by modification time (newest last)
    db_files = sorted(day_folder.glob("*.db"), key=lambda f: f.stat().st_mtime)
    
    if not db_files:
        return None
    
    # Use the most recent backup
    latest_db = db_files[-1]
    
    try:
        conn = sqlite3.connect(latest_db)
        cursor = conn.cursor()
        
        # Query case-insensitive
        cursor.execute(
            "SELECT playtime_seconds FROM playtime WHERE LOWER(username) = LOWER(?)",
            (username,)
        )
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return result[0]
        return None
    
    except Exception as e:
        print(f"[PLAYTIME] Error querying database {latest_db}: {e}")
        return None


def get_daily_playtime_data(username: str, days: int) -> Tuple[list, bool]:
    """Get daily playtime data for a user over the specified number of days.
    
    Always returns data for the full requested range, filling missing days with 0.
    Returns (daily_data, user_found_in_any_db) tuple.
    """
    available_days = get_available_days()
    
    # Create a lookup dict for available days
    available_lookup = {date.strftime('%Y-%m-%d'): folder for folder, date in available_days}
    
    # Use today as the end date (or latest available if no data exists yet)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = today
    
    # Calculate the start date (days ago from end date)
    start_date = end_date - timedelta(days=days - 1)
    
    daily_data = []
    user_found_in_any_db = False
    current_date = start_date
    
    while current_date <= end_date:
        date_key = current_date.strftime('%Y-%m-%d')
        
        if date_key in available_lookup:
            # We have data for this day
            folder = available_lookup[date_key]
            playtime = get_final_playtime_for_day(folder, username)
            if playtime is not None:
                user_found_in_any_db = True
            daily_data.append({
                'date': current_date,
                'playtime_seconds': playtime if playtime is not None else 0
            })
        else:
            # No data for this day, fill with 0
            daily_data.append({
                'date': current_date,
                'playtime_seconds': 0
            })
        
        current_date += timedelta(days=1)
    
    return daily_data, user_found_in_any_db


def format_playtime(seconds: int) -> str:
    """Format seconds into a readable string (Xh Ym)"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    
    if hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"


def create_playtime_graph(username: str, daily_data: list, days_requested: int, avg_hours: float, median_hours: float) -> BytesIO:
    """Create a bar graph showing daily playtime."""
    
    # Prepare data for the graph
    dates = [d['date'].strftime('%m/%d') for d in daily_data]
    playtimes_hours = [d['playtime_seconds'] / 3600 for d in daily_data]  # Convert to hours
    
    # Create figure with dark theme
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor('#2b2d31')
    ax.set_facecolor('#2b2d31')
    
    # Create bars with gradient-like color
    bars = ax.bar(dates, playtimes_hours, color='#5865f2', edgecolor='#4752c4', linewidth=1.5)
    
    # Add value labels on top of bars
    for bar in bars:
        height = bar.get_height()
        if height > 0:
            # Format as hours and minutes
            total_minutes = int(height * 60)
            hours = total_minutes // 60
            minutes = total_minutes % 60
            if hours > 0:
                label = f'{hours}h {minutes}m'
            else:
                label = f'{minutes}m'
            ax.text(bar.get_x() + bar.get_width()/2., height,
                   label,
                   ha='center', va='bottom', color='white', fontsize=9, fontweight='bold')
    
    # Customize the plot
    ax.set_xlabel('Date', fontsize=12, color='white', fontweight='bold')
    ax.set_ylabel('Playtime (hours)', fontsize=12, color='white', fontweight='bold')
    ax.set_title(f'{username} - Daily Playtime (Last {len(daily_data)} Days)', 
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
    
    # Add median line (thin red) - ensure minimum visibility
    median_line_y = max(median_hours, 0.00)
    ax.axhline(y=median_line_y, color='#e74c3c', linestyle='-', linewidth=1.5, label=f'Median: {median_hours:.1f}h')
    
    # Add average line (thin blue) - ensure minimum visibility
    avg_line_y = max(avg_hours, 0.00)
    ax.axhline(y=avg_line_y, color='#3498db', linestyle='-', linewidth=1.5, label=f'Average: {avg_hours:.1f}h')
    
    # Add legend below the chart on the left
    ax.legend(loc='upper left', bbox_to_anchor=(0, -0.15), ncol=2, fontsize=9, facecolor='#2b2d31', edgecolor='#4752c4')
    
    # Tight layout to prevent label cutoff
    plt.tight_layout()
    
    # Save to BytesIO
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, facecolor='#2b2d31', edgecolor='none')
    buf.seek(0)
    plt.close(fig)
    
    return buf


def setup(bot, has_required_role, config):
    """Playtime Graph Command"""
    
    @bot.tree.command(
        name="playtime",
        description="View a player's daily playtime over a time period"
    )
    @app_commands.describe(
        username="The player's username",
        delta="Number of days to display (default: 7, max: 30)"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def playtime(
        interaction: discord.Interaction,
        username: str,
        delta: Optional[int] = 7
    ):
        """View a player's daily playtime with a graph"""
        
        # Check permissions
        if interaction.guild:
            if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
                await interaction.response.send_message(
                    "❌ You don't have permission to use this command!",
                    ephemeral=True
                )
                return
        
        await interaction.response.defer()
        
        # Validate delta
        if delta < 1 or delta > 30:
            await interaction.followup.send(
                "❌ Please select a valid number of days (1-30).",
                ephemeral=True
            )
            return
        
        try:
            # Check if tracking folder exists
            if not PLAYTIME_TRACKING_FOLDER.exists():
                await interaction.followup.send(
                    "❌ No playtime data available. Playtime tracking has not started yet.",
                    ephemeral=True
                )
                return
            
            # Get daily playtime data
            daily_data, user_found = get_daily_playtime_data(username, delta)
            
            start_date = daily_data[0]['date'].strftime('%Y-%m-%d')
            end_date = daily_data[-1]['date'].strftime('%Y-%m-%d')
            
            # Check if player exists on Wynncraft (also gets online status)
            player_exists, correct_username, is_online, server = await check_player_exists(username)
            
            # Check if user was found in any database
            if not user_found:
                if not player_exists:
                    # Player doesn't exist on Wynncraft at all
                    embed = discord.Embed(
                        title="❌ Player Not Found",
                        description=f"`{username}` is not a valid Wynncraft username.",
                        color=0xFF0000
                    )
                    embed.add_field(
                        name="What to do:",
                        value=(
                            f" - Check if you spelled the username correctly.\n"
                            f" - Make sure the player has logged into Wynncraft at least once.\n"
                            f"\n-# if you think this is a mistake, you can contact support using `/contact_support`."
                        ),
                        inline=False
                    )
                    embed.set_footer(text=f"Data from {start_date} to {end_date}")
                    
                    await interaction.followup.send(
                        embed=embed,
                        ephemeral=True
                    )
                    return
                # Player exists but not in database - continue with 0 playtime data
                username = correct_username
            else:
                # Use the correct username from Wynncraft
                if player_exists:
                    username = correct_username
            
            # Calculate total playtime
            total_playtime = sum(d['playtime_seconds'] for d in daily_data)
            
            # Calculate stats
            actual_days = len(daily_data)
            avg_playtime = total_playtime / actual_days if actual_days > 0 else 0
            playtime_values = [d['playtime_seconds'] for d in daily_data]
            median_playtime = statistics.median(playtime_values) if playtime_values else 0
            max_playtime = max(playtime_values)
            max_day = next(d for d in daily_data if d['playtime_seconds'] == max_playtime)
            
            # Generate graph
            avg_hours = avg_playtime / 3600
            median_hours = median_playtime / 3600
            graph_buffer = create_playtime_graph(username, daily_data, delta, avg_hours, median_hours)
            graph_file = discord.File(graph_buffer, filename=f"{username}_playtime.png")
            
            # Create embed
            online_status = f"🟢 Online ({server})" if is_online else "🔴 Offline"
            embed = discord.Embed(
                title=f"📊 Playtime - {username}",
                description=f"{online_status}\nDaily playtime over the last **{actual_days}** days",
                color=0x00FF00 if is_online else 0x5865f2
            )
            
            # Set the graph as the embed image
            embed.set_image(url=f"attachment://{username}_playtime.png")
            
            # Add stats
            embed.add_field(
                name="Total Playtime",
                value=f"**{format_playtime(total_playtime)}**",
                inline=True
            )
            
            embed.add_field(
                name="Daily Average",
                value=f"**{format_playtime(int(avg_playtime))}**",
                inline=True
            )
            
            embed.add_field(
                name="Daily Median",
                value=f"**{format_playtime(int(median_playtime))}**",
                inline=True
            )
            
            embed.set_footer(text=f"Data from {start_date} to {end_date}")
            
            await interaction.followup.send(embed=embed, file=graph_file)
        
        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xFF0000
            )
            await interaction.followup.send(embed=error_embed)
            print(f"[PLAYTIME] Error in playtime command: {e}")
            import traceback
            traceback.print_exc()
    
    print("[OK] Loaded playtime command")
