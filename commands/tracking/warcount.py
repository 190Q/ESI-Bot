import discord
from discord import app_commands
import sqlite3
import os
import glob
from datetime import datetime, timedelta, timezone
from typing import Optional
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from io import BytesIO
import sys
from pathlib import Path
import json
import tempfile
import statistics
from utils.permissions import has_roles
from utils.paths import PROJECT_ROOT, DATA_DIR, DB_DIR

API_TRACKING_FOLDER = DB_DIR / "api_tracking"

OWNER_ID = int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0
REQUIRED_ROLES = [
    OWNER_ID,
    554889169705500672
]

def create_warcount_graph(username: str, daily_deltas: list, days_requested: int, avg_wars: float, median_wars: float) -> BytesIO:
    """Create a bar graph showing daily warcount deltas."""
    
    # Prepare data for the graph
    dates = [d['date'].strftime('%m/%d') for d in daily_deltas]
    deltas = [d['delta'] for d in daily_deltas]
    
    # Create figure with dark theme
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor('#2b2d31')
    ax.set_facecolor('#2b2d31')
    
    # Create bars
    bars = ax.bar(dates, deltas, color='#5865f2', edgecolor='#4752c4', linewidth=1.5)
    
    # Add value labels on top of bars
    for bar in bars:
        height = bar.get_height()
        if height > 0:
            ax.text(bar.get_x() + bar.get_width()/2., height,
                   f'{int(height)}',
                   ha='center', va='bottom', color='white', fontsize=10, fontweight='bold')
    
    # Customize the plot
    ax.set_xlabel('Date', fontsize=12, color='white', fontweight='bold')
    ax.set_ylabel('Wars', fontsize=12, color='white', fontweight='bold')
    ax.set_title(f'{username} - Daily War Count (Last {len(daily_deltas)} Days)', 
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
    median_line_y = max(median_wars, 0.00)
    ax.axhline(y=median_line_y, color='#e74c3c', linestyle='-', linewidth=1.5, label=f'Median: {median_wars:.1f}')
    
    # Add average line (thin blue) - ensure minimum visibility
    avg_line_y = max(avg_wars, 0.00)
    ax.axhline(y=avg_line_y, color='#3498db', linestyle='-', linewidth=1.5, label=f'Average: {avg_wars:.1f}')
    
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

def get_databases_in_timeframe(days: int):
    """Get all databases from api_tracking within the timeframe."""
    try:
        if not API_TRACKING_FOLDER.exists():
            return None, "API tracking folder not found"
        
        # Collect all .db files from api_tracking day folders
        db_files = []
        for day_folder in API_TRACKING_FOLDER.iterdir():
            if day_folder.is_dir() and day_folder.name.startswith("api_"):
                for db_file in day_folder.glob("ESI_*.db"):
                    db_files.append(db_file)
        
        if not db_files:
            return None, "No database files found"
        
        # Sort by modification time
        db_files.sort(key=lambda f: f.stat().st_mtime)
        
        latest_time = datetime.fromtimestamp(db_files[-1].stat().st_mtime, tz=timezone.utc)
        target_time = latest_time - timedelta(days=days)
        
        databases_in_range = []
        for db_file in db_files:
            db_time = datetime.fromtimestamp(db_file.stat().st_mtime, tz=timezone.utc)
            if db_time >= target_time:
                databases_in_range.append((str(db_file), db_time))
        
        if len(databases_in_range) < 2:
            return None, "Not enough historical data for comparison"
        
        # Sort by time (oldest to newest)
        databases_in_range.sort(key=lambda x: x[1])
        
        return databases_in_range, None
    
    except Exception as e:
        return None, f"Error: {str(e)}"

def get_player_warcount(db_path: str, username: str) -> Optional[int]:
    """Get player's warcount from a database."""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='player_stats'")
        if not cursor.fetchone():
            conn.close()
            print(f"Warning: player_stats table not found in {db_path}")
            return None
        
        # Query case-insensitive
        cursor.execute(
            "SELECT wars FROM player_stats WHERE LOWER(username) = LOWER(?)",
            (username,)
        )
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return result[0]
        return None
    
    except Exception as e:
        print(f"Error querying database {db_path}: {e}")
        return None

def get_daily_warcount_deltas(databases: list, username: str) -> list:
    """Calculate daily warcount deltas for a player."""
    from collections import defaultdict
    
    daily_deltas = []
    date_totals = defaultdict(int)  # Track total delta per day
    
    for i in range(len(databases) - 1):
        db1_path, db1_time = databases[i]
        db2_path, db2_time = databases[i + 1]
        
        warcount1 = get_player_warcount(db1_path, username)
        warcount2 = get_player_warcount(db2_path, username)
        
        if warcount1 is not None and warcount2 is not None:
            delta = warcount2 - warcount1
            
            # Group by date (day) instead of individual timestamps
            date_key = db2_time.strftime('%Y-%m-%d')
            date_totals[date_key] += delta
    
    # Convert to list sorted by date
    for date_str, total_delta in sorted(date_totals.items()):
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        daily_deltas.append({
            'date': date_obj,
            'delta': total_delta
        })
    
    return daily_deltas


def fill_daily_deltas(daily_deltas: list, days: int) -> list:
    """Fill missing days with 0 delta, covering the full requested range.
    
    Always returns data for the full requested range, using today as the end date.
    """
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = today
    start_date = end_date - timedelta(days=days - 1)
    
    # Create lookup from existing deltas
    delta_lookup = {}
    for d in daily_deltas:
        date_key = d['date'].strftime('%Y-%m-%d')
        delta_lookup[date_key] = d['delta']
    
    filled = []
    current_date = start_date
    while current_date <= end_date:
        date_key = current_date.strftime('%Y-%m-%d')
        filled.append({
            'date': current_date,
            'delta': delta_lookup.get(date_key, 0)
        })
        current_date += timedelta(days=1)
    
    return filled


def setup(bot, has_required_role, config):
    """Playtime Delta Command"""
    
    @bot.tree.command(
        name="warcount",
        description="Check how many wars a player participated in over a time period"
    )
    @app_commands.describe(
        username="The player's username (or '%all%' for leaderboard)",
        delta="Select amount of days to check (default: 7, max: 60)"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def warcount(
        interaction: discord.Interaction,
        username: str,
        delta: Optional[int] = 7
    ):
        """Check warcount gained by a player with daily breakdown"""
        
        # Check permissions
        if interaction.guild:
            if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
                await interaction.response.send_message(
                    "❌ You don't have permission to use this command!",
                    ephemeral=True
                )
                return
        
        await interaction.response.defer()

        if delta < 1 or delta > 60:
            await interaction.followup.send("❌ Please select a valid number of days (1-60).", ephemeral=True)
            return
        
        # Check if requesting all players
        show_all = username.lower() == "%all%"
        
        try:
            # Get databases for specified days
            databases, error = get_databases_in_timeframe(delta)
            
            if not databases:
                error_embed = discord.Embed(
                    title="⚠️ Insufficient Data",
                    description=(
                        f"Not enough historical data available for a {delta}-day comparison.\n\n"
                        f"**Why?** {error}\n\n"
                        f"💡 **Tip:** The bot automatically fetches data every 30 minutes. "
                        f"Wait for more data to be collected, then try again!"
                    ),
                    color=0xFFA500
                )
                await interaction.followup.send(embed=error_embed)
                return
            
            # Get warcount from oldest and newest databases
            oldest_db, oldest_time = databases[0]
            latest_db, latest_time = databases[-1]
            
            if show_all:
                # Get all players and their warcount deltas
                conn_latest = sqlite3.connect(latest_db)
                cursor_latest = conn_latest.cursor()
                cursor_latest.execute("SELECT username FROM player_stats WHERE username IS NOT NULL")
                all_players = [row[0] for row in cursor_latest.fetchall()]
                conn_latest.close()
                
                player_deltas = []
                for player in all_players:
                    oldest_warcount = get_player_warcount(oldest_db, player)
                    latest_warcount = get_player_warcount(latest_db, player)
                    
                    if oldest_warcount is not None and latest_warcount is not None:
                        delta = latest_warcount - oldest_warcount
                        # Only include players with non-zero delta
                        if delta != 0:
                            player_deltas.append({
                                'username': player,
                                'old_count': oldest_warcount,
                                'new_count': latest_warcount,
                                'delta': delta
                            })
                
                # Sort by delta (highest first)
                player_deltas.sort(key=lambda x: x['delta'], reverse=True)
                
                if not player_deltas:
                    await interaction.followup.send("❌ No player data found.")
                    return
                
                # Create text file with all players
                actual_days = (latest_time - oldest_time).total_seconds() / 86400
                report_lines = []
                report_lines.append(f"War Count Delta Report - Last {actual_days:.1f} Days")
                report_lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
                report_lines.append(f"Total Players: {len(player_deltas)}")
                report_lines.append(f"Time Range: {oldest_time.strftime('%Y-%m-%d %H:%M UTC')} to {latest_time.strftime('%Y-%m-%d %H:%M UTC')}")
                report_lines.append("")
                report_lines.append("WAR COUNT LEADERBOARD")
                report_lines.append("=" * 100)
                report_lines.append("")
                
                # Calculate column widths
                max_username_len = max(len(p['username']) for p in player_deltas) if player_deltas else 8
                username_width = max(max_username_len, 8)
                
                header = f"{'Rank':<6} | {'Username':<{username_width}} | {'Old Count':<10} | {'New Count':<10} | {'Delta':<10}"
                report_lines.append(header)
                report_lines.append("-" * len(header))
                
                for idx, player_data in enumerate(player_deltas, 1):
                    line = (
                        f"{idx:<6} | "
                        f"{player_data['username']:<{username_width}} | "
                        f"{player_data['old_count']:<10} | "
                        f"{player_data['new_count']:<10} | "
                        f"{player_data['delta']:<10}"
                    )
                    report_lines.append(line)
                
                report_lines.append("")
                report_lines.append("=" * 100)
                report_lines.append("")
                report_lines.append("SUMMARY")
                report_lines.append("-" * 40)
                total_delta = sum(p['delta'] for p in player_deltas)
                report_lines.append(f"Total Players: {len(player_deltas)}")
                report_lines.append(f"Total War Count Increase: {total_delta:,}")
                report_lines.append(f"Average per Player: {total_delta / len(player_deltas):.2f}")
                
                # Create temp file
                with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
                    f.write("\n".join(report_lines))
                    temp_file_path = f.name
                
                file_attachment = discord.File(temp_file_path, filename=f"warcount_report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt")
                
                # Create embed with top 10
                embed = discord.Embed(
                    title=f"War Count Leaderboard - Last {actual_days:.1f} Days",
                    description=f"Showing top 10 players by war count increase (full report attached)",
                    color=0x00FF00
                )
                
                # Add top 10
                top_10_text = []
                for idx, player_data in enumerate(player_deltas[:10], 1):
                    top_10_text.append(
                        f"{idx} **{player_data['username']}**: +{player_data['delta']:,} wars "
                        f"({player_data['old_count']:,} → {player_data['new_count']:,})"
                    )
                
                embed.add_field(
                    name="Top 10 Players",
                    value="\n".join(top_10_text),
                    inline=False
                )
                
                # Add summary
                embed.add_field(
                    name="Summary",
                    value=f"**Total Players:** {len(player_deltas)}\n**Total Wars Added:** {total_delta:,}\n**Average per Player:** {total_delta / len(player_deltas):.1f}",
                    inline=False
                )
                
                embed.set_footer(
                    text=f"Data from {oldest_time.strftime('%Y-%m-%d %H:%M UTC')} to {latest_time.strftime('%Y-%m-%d %H:%M UTC')}"
                )
                
                await interaction.followup.send(embed=embed, file=file_attachment)
                
                # Clean up temp file
                try:
                    os.unlink(temp_file_path)
                except:
                    pass
                
                return
            
            # Single player logic
            # Validate username exists in the guild (latest database)
            if get_player_warcount(latest_db, username) is None:
                await interaction.followup.send(
                    f"❌ Player **{username}** was not found in the guild.",
                    ephemeral=True
                )
                return

            # Calculate daily deltas and fill missing days with 0
            daily_deltas = get_daily_warcount_deltas(databases, username)
            daily_deltas = fill_daily_deltas(daily_deltas, delta)
            
            # Calculate stats
            actual_days = len(daily_deltas)
            delta_values = [d['delta'] for d in daily_deltas]
            total_delta = sum(delta_values)
            avg_delta = total_delta / actual_days if actual_days > 0 else 0
            median_delta = statistics.median(delta_values) if delta_values else 0
            
            start_date = daily_deltas[0]['date'].strftime('%Y-%m-%d')
            end_date = daily_deltas[-1]['date'].strftime('%Y-%m-%d')
            
            # Generate graph
            graph_buffer = create_warcount_graph(username, daily_deltas, delta, avg_delta, median_delta)
            graph_file = discord.File(graph_buffer, filename=f"{username}_warcount.png")
            
            embed = discord.Embed(
                title=f"⚔️ War Count - {username}",
                description=f"Daily war count over the last **{actual_days}** days",
                color=0x5865f2
            )
            
            # Set the graph as the embed image
            embed.set_image(url=f"attachment://{username}_warcount.png")
            
            # Add stats
            embed.add_field(
                name="Total Wars",
                value=f"**{total_delta:,}**",
                inline=True
            )
            
            embed.add_field(
                name="Daily Average",
                value=f"**{avg_delta:.1f}**",
                inline=True
            )
            
            embed.add_field(
                name="Daily Median",
                value=f"**{median_delta:.1f}**",
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
            print(f"Error in playtime command: {e}")
            import traceback
            traceback.print_exc()
    
    print("[OK] Loaded warcount_delta command")
