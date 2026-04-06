import discord
from discord import app_commands
from datetime import datetime, timedelta, timezone
import os
import sqlite3
import glob
import tempfile
import json
from pathlib import Path
from utils.permissions import has_roles

# Database paths
USERNAME_MATCHES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data/username_matches.json",
)
INACTIVITY_EXEMPTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data/inactivity_exemptions.json",
)
DB_FOLDER = Path(os.path.dirname(os.path.dirname(os.path.dirname((os.path.abspath(__file__)))))) / "databases"
PLAYTIME_TRACKING_FOLDER = DB_FOLDER / "playtime_tracking"

SKIP_IF_NO_DATA = False

# Default settings
DEFAULT_MIN_PLAYTIME_HOURS = 2
DEFAULT_SERVER_ID = 554418045397762048  # ESI server

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    600185623474601995,  # Parliament
    954566591520063510   # Jurors
]

# Roles that have restricted access (can view but not manage exemptions)
RESTRICTED_ROLES = [
    954566591520063510  # Jurors
]


def is_restricted_user(user):
    """Check if user has restricted access (can view but not manage)"""
    user_role_ids = [role.id for role in user.roles]
    has_parliament = 600185623474601995 in user_role_ids
    has_owner = user.id == int(os.getenv('OWNER_ID', '0'))
    has_restricted = any(role_id in user_role_ids for role_id in RESTRICTED_ROLES)
    return has_restricted and not has_parliament and not has_owner


def get_guild_db_for_date(target_date):
    """Get the guild database (ESI_*.db) closest to the target date.
    
    Prefers database from target_date or the first one AFTER if none exists on that date.
    Returns the path to the database or None if not found.
    """
    # Look in the new api_tracking folder structure
    api_tracking_folder = DB_FOLDER / "api_tracking"
    
    # Collect all .db files from all day folders
    db_files = []
    if api_tracking_folder.exists():
        for day_folder in api_tracking_folder.iterdir():
            if day_folder.is_dir() and day_folder.name.startswith("api_"):
                db_files.extend(day_folder.glob("ESI_*.db"))
    
    # Fallback: also check old flat structure for backwards compatibility
    db_files.extend(DB_FOLDER.glob("ESI_*.db"))
    
    # Convert to strings for consistent handling
    db_files = [str(f) for f in db_files]
    
    print(f"[INAC_CHECK] Looking for databases in: {api_tracking_folder}")
    
    if not db_files:
        return None
    
    # Get all databases with their dates
    db_files_with_time = []
    for db_file in db_files:
        mtime = os.path.getmtime(db_file)
        file_date = datetime.fromtimestamp(mtime, tz=timezone.utc).date()
        db_files_with_time.append((db_file, file_date, mtime))
    
    # Sort by date ascending
    db_files_with_time.sort(key=lambda x: x[1])
    
    # First, try to find one on the exact date
    for db_file, file_date, mtime in db_files_with_time:
        if file_date == target_date:
            print(f"[INAC_CHECK] Found database for {target_date}")
            return db_file
    
    # If not found, find the first one AFTER the target date
    for db_file, file_date, mtime in db_files_with_time:
        if file_date > target_date:
            print(f"[INAC_CHECK] Found database for {target_date}")
            return db_file
    
    print(f"[INAC_CHECK] No database found for {target_date}")
    
    # If no database after target date, return the most recent one
    return db_files_with_time[-1][0] if db_files_with_time else None


def get_players_from_guild_db(db_path):
    """Get all player usernames from the player_stats table."""
    if not db_path or not os.path.exists(db_path):
        return []
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='player_stats'")
        if not cursor.fetchone():
            conn.close()
            return []
        
        cursor.execute("SELECT username FROM player_stats WHERE username IS NOT NULL")
        players = [row[0] for row in cursor.fetchall()]
        conn.close()
        return players
    
    except Exception as e:
        print(f"[INAC_CHECK] Error reading guild database: {e}")
        return []


def get_playtime_folder_for_date(date):
    """Get the playtime tracking folder for a specific date."""
    date_str = date.strftime("%d-%m-%Y")
    folder = PLAYTIME_TRACKING_FOLDER / f"playtime_{date_str}"
    return folder if folder.exists() else None


def get_final_playtime_for_day(day_folder, username):
    """Get the final playtime for a user from a day's backup folder."""
    if not day_folder or not day_folder.exists():
        return 0
    
    # Get all .db files in the folder, sorted by modification time (newest last)
    db_files = sorted(day_folder.glob("*.db"), key=lambda f: f.stat().st_mtime)
    
    if not db_files:
        return 0
    
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
        
        return result[0] if result else 0
    
    except Exception as e:
        print(f"[INAC_CHECK] Error querying playtime database: {e}")
        return 0


def get_total_playtime_for_period(username, start_date, end_date):
    """Get total playtime for a user during a date range.
    
    Sums up the daily playtime for each day in the range.
    """
    total_seconds = 0
    current_date = start_date
    
    while current_date <= end_date:
        folder = get_playtime_folder_for_date(current_date)
        if folder:
            daily_playtime = get_final_playtime_for_day(folder, username)
            total_seconds += daily_playtime
        current_date += timedelta(days=1)
    
    return total_seconds


def count_available_days_in_period(start_date, end_date):
    """Count how many days in the period have playtime data available."""
    available_count = 0
    total_days = 0
    missing_dates = []
    current_date = start_date
    
    while current_date <= end_date:
        total_days += 1
        folder = get_playtime_folder_for_date(current_date)
        if folder:
            available_count += 1
        else:
            missing_dates.append(current_date.strftime("%d-%m-%Y"))
        current_date += timedelta(days=1)
    
    return available_count, total_days, missing_dates


def format_playtime(seconds):
    """Format seconds into a readable string (Xh Ym)"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    
    if hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"


def load_username_matches():
    """Load the username_matches.json file and return a reverse mapping.
    
    Returns: dict mapping lowercase minecraft username -> discord user id
    """
    if not os.path.exists(USERNAME_MATCHES_PATH):
        print(f"[INAC_CHECK] Username matches file not found: {USERNAME_MATCHES_PATH}")
        return {}
    
    try:
        with open(USERNAME_MATCHES_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Create reverse mapping: minecraft_username.lower() -> discord_id
        reverse_map = {}
        for discord_id, info in data.items():
            # Skip entries that aren't dicts
            if not isinstance(info, dict):
                continue
            mc_username = info.get('username', '')
            if mc_username and isinstance(mc_username, str):
                reverse_map[mc_username.lower()] = int(discord_id)
        
        print(f"[INAC_CHECK] Loaded {len(reverse_map)} username matches")
        return reverse_map
    except Exception as e:
        print(f"[INAC_CHECK] Error loading username matches: {e}")
        import traceback
        traceback.print_exc()
        return {}


def load_exemptions():
    """Load inactivity exemptions from JSON file.
    
    Returns: dict mapping discord_user_id (str) -> list of week keys ("YYYY-MM-DD_YYYY-MM-DD")
    """
    if not os.path.exists(INACTIVITY_EXEMPTIONS_PATH):
        return {}
    
    try:
        with open(INACTIVITY_EXEMPTIONS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[INAC_CHECK] Error loading exemptions: {e}")
        return {}


def save_exemptions(exemptions):
    """Save inactivity exemptions to JSON file."""
    try:
        with open(INACTIVITY_EXEMPTIONS_PATH, 'w', encoding='utf-8') as f:
            json.dump(exemptions, f, indent=2)
        return True
    except Exception as e:
        print(f"[INAC_CHECK] Error saving exemptions: {e}")
        return False


def cleanup_expired_exemptions():
    """Remove expired week exemptions from all users."""
    exemptions = load_exemptions()
    # Use full datetime for Thursday 23:59 expiry check
    now = datetime.now(timezone.utc)
    modified = False
    
    users_to_remove = []
    
    for user_key, data in exemptions.items():
        # Handle old format (list)
        if isinstance(data, list):
            original_len = len(data)
            data = [w for w in data if w == "permanent" or _is_week_valid(w, now)]
            if len(data) != original_len:
                modified = True
                if data:
                    exemptions[user_key] = data
                else:
                    users_to_remove.append(user_key)
        
        # Handle new format (dict)
        elif isinstance(data, dict):
            weeks = data.get("weeks", [])
            original_len = len(weeks)
            weeks = [w for w in weeks if w == "permanent" or _is_week_valid(w, now)]
            if len(weeks) != original_len:
                modified = True
                data["weeks"] = weeks
                if not weeks and not data.get("reason"):
                    users_to_remove.append(user_key)
    
    for user_key in users_to_remove:
        del exemptions[user_key]
        modified = True
    
    if modified:
        save_exemptions(exemptions)
        print(f"[INAC_CHECK] Cleaned up expired exemptions for {len(users_to_remove)} users")


def _is_week_valid(week_key: str, now: datetime) -> bool:
    """Check if a week exemption is still valid.

    Exemptions expire on Thursday at 23:59 UTC of the week their end date falls in.
    This ensures they are cleaned up before the second check runs.
    """
    try:
        _, end_str = week_key.split("_")
        end_date = datetime.fromisoformat(end_str).date()

        # Find the Thursday of the week containing end_date (weekday 3 = Thursday)
        days_to_thursday = (3 - end_date.weekday()) % 7
        expiry_date = end_date + timedelta(days=days_to_thursday)

        expiry_dt = datetime(
            expiry_date.year,
            expiry_date.month,
            expiry_date.day,
            23, 59, 0,
            tzinfo=timezone.utc
        )

        return now <= expiry_dt
    except:
        return False


def get_user_exemption_data(discord_id: int):
    """Get exemption data for a user, handling both old and new format.
    
    Returns: (weeks_list, reason_or_none)
    """
    exemptions = load_exemptions()
    user_key = str(discord_id)
    
    if user_key not in exemptions:
        return [], None
    
    data = exemptions[user_key]
    
    # Handle old format (just a list of weeks)
    if isinstance(data, list):
        return data, None
    
    # New format (dict with weeks and reason)
    if isinstance(data, dict):
        return data.get("weeks", []), data.get("reason")
    
    return [], None


def is_user_exempt(discord_id: int, start_date, end_date) -> bool:
    """Check if a user is exempt for a specific week.
    
    Checks if any exemption period overlaps with the check period.
    """
    weeks, _ = get_user_exemption_data(discord_id)
    
    if not weeks:
        return False
    
    # Check for permanent exemption
    if "permanent" in weeks:
        return True
    
    # Check for exact match first
    week_key = f"{start_date.isoformat()}_{end_date.isoformat()}"
    if week_key in weeks:
        return True
    
    # Check for overlapping exemption periods
    for exemption_key in weeks:
        if exemption_key == "permanent":
            continue
        try:
            exempt_start_str, exempt_end_str = exemption_key.split("_")
            exempt_start = datetime.fromisoformat(exempt_start_str).date()
            exempt_end = datetime.fromisoformat(exempt_end_str).date()
            
            # Check if periods overlap (start1 <= end2 AND start2 <= end1)
            if start_date <= exempt_end and exempt_start <= end_date:
                return True
        except (ValueError, AttributeError):
            continue
    
    return False


def get_future_weeks(num_weeks: int = 12, second_check: bool = True) -> list:
    """Get future weeks for exemption purposes.
    
    Args:
        num_weeks: Number of future weeks to return
        second_check: If True, returns weeks from Monday to Tuesday
    
    Returns a list of tuples: (week_label, start_date, end_date)
    """
    today = datetime.now(timezone.utc).date()
    
    # Find the next Tuesday (or today if it's Tuesday)
    days_until_tuesday = (1 - today.weekday()) % 7
    if days_until_tuesday == 0 and today.weekday() != 1:
        days_until_tuesday = 7
    
    next_tuesday = today + timedelta(days=days_until_tuesday)
    next_monday = next_tuesday - timedelta(days=8)  # Monday of the week being checked
    
    weeks = []
    for i in range(num_weeks):
        monday = next_monday + timedelta(weeks=i)
        tuesday = next_tuesday + timedelta(weeks=i)
        
        # Format: "Jan 01 - Jan 09, 2024"
        if monday.month == tuesday.month:
            week_label = f"{monday.strftime('%b %d')} - {tuesday.strftime('%d')}, {tuesday.year}"
        elif monday.year == tuesday.year:
            week_label = f"{monday.strftime('%b %d')} - {tuesday.strftime('%b %d')}, {tuesday.year}"
        else:
            week_label = f"{monday.strftime('%b %d, %Y')} - {tuesday.strftime('%b %d, %Y')}"
        
        weeks.append((week_label, monday, tuesday))
    
    return weeks


def get_previous_weeks(num_weeks: int = 5, second_check: bool = False) -> list:
    """Get the previous full weeks (Monday to Sunday) or partial weeks for second check.
    
    Args:
        num_weeks: Number of weeks to return
        second_check: If True, returns weeks from Monday to Tuesday (current week style)
    
    Returns a list of tuples: (week_label, start_date, end_date)
    """
    today = datetime.now(timezone.utc).date()
    
    if second_check:
        # Second check: weeks from Monday to Tuesday of the following week (9 days)
        # Find the most recent Tuesday
        days_since_tuesday = (today.weekday() - 1) % 7
        if days_since_tuesday == 0 and today.weekday() != 1:
            days_since_tuesday = 7  # If today is not Tuesday, find the previous one
        
        # If today is Tuesday, use today; otherwise find the last Tuesday
        if today.weekday() == 1:  # Tuesday
            last_tuesday = today
        else:
            last_tuesday = today - timedelta(days=days_since_tuesday)
        
        # Monday is 8 days before Tuesday (Monday of the previous week)
        last_monday = last_tuesday - timedelta(days=8)
        
        weeks = []
        for i in range(num_weeks):
            monday = last_monday - timedelta(weeks=i)
            tuesday = last_tuesday - timedelta(weeks=i)
            
            # Format: "Jan 01 - Jan 09, 2024"
            if monday.month == tuesday.month:
                week_label = f"{monday.strftime('%b %d')} - {tuesday.strftime('%d')}, {tuesday.year}"
            elif monday.year == tuesday.year:
                week_label = f"{monday.strftime('%b %d')} - {tuesday.strftime('%b %d')}, {tuesday.year}"
            else:
                week_label = f"{monday.strftime('%b %d, %Y')} - {tuesday.strftime('%b %d, %Y')}"
            
            weeks.append((week_label, monday, tuesday))
        
        return weeks
    
    else:
        # First check: full weeks Monday to Sunday
        # Find the most recent Sunday (end of the last complete week)
        days_since_sunday = (today.weekday() + 1) % 7
        if days_since_sunday == 0:
            days_since_sunday = 7  # If today is Sunday, go back a full week
        
        last_sunday = today - timedelta(days=days_since_sunday)
        last_monday = last_sunday - timedelta(days=6)
        
        weeks = []
        for i in range(num_weeks):
            monday = last_monday - timedelta(weeks=i)
            sunday = last_sunday - timedelta(weeks=i)
            
            # Format: "Jan 01 - Jan 07, 2024"
            if monday.month == sunday.month:
                week_label = f"{monday.strftime('%b %d')} - {sunday.strftime('%d')}, {sunday.year}"
            elif monday.year == sunday.year:
                week_label = f"{monday.strftime('%b %d')} - {sunday.strftime('%b %d')}, {sunday.year}"
            else:
                week_label = f"{monday.strftime('%b %d, %Y')} - {sunday.strftime('%b %d, %Y')}"
            
            weeks.append((week_label, monday, sunday))
        
        return weeks


class ReasonModal(discord.ui.Modal, title="Exemption Reason"):
    """Modal for entering exemption reason"""
    reason_input = discord.ui.TextInput(
        label="Reason",
        placeholder="Enter the reason for this exemption...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500
    )
    
    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
        # Pre-fill with existing reason
        if parent_view.reason:
            self.reason_input.default = parent_view.reason
    
    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.reason = self.reason_input.value.strip() or None
        
        # Update the button label to indicate reason is set
        if self.parent_view.reason:
            self.parent_view.reason_btn.label = "✏️ Edit Reason"
            self.parent_view.reason_btn.style = discord.ButtonStyle.primary
        else:
            self.parent_view.reason_btn.label = "📝 Add Reason"
            self.parent_view.reason_btn.style = discord.ButtonStyle.secondary
        
        # Update confirm button state
        selection_changed = set(self.parent_view.selected_weeks) != self.parent_view.original_exemptions
        reason_changed = self.parent_view.reason != self.parent_view.original_reason
        has_changes = selection_changed or reason_changed
        # Don't allow reason without weeks
        invalid_state = self.parent_view.reason and not self.parent_view.selected_weeks
        self.parent_view.confirm_btn.disabled = not has_changes or invalid_state
        
        # Rebuild the embed to show reason
        await self.parent_view._update_embed(interaction)


class ExemptWeekSelectView(discord.ui.View):
    """View for selecting weeks to exempt a user from inactivity checks"""
    def __init__(self, author_id: int, target_user: discord.Member, bot):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.target_user = target_user
        self.bot = bot
        
        # Load existing exemptions for this user
        exemptions = load_exemptions()
        user_key = str(target_user.id)
        existing_weeks, existing_reason = get_user_exemption_data(target_user.id)
        
        # Get future weeks only
        future_weeks = get_future_weeks(15, second_check=True)
        
        # Create options
        options = []
        
        # Add permanent option first
        options.append(discord.SelectOption(
            label="🔒 Permanent Exemption",
            value="permanent",
            description="Exempt from all future inactivity checks",
            default="permanent" in existing_weeks
        ))
        
        for i, (label, start_date, end_date) in enumerate(future_weeks):
            value = f"{start_date.isoformat()}_{end_date.isoformat()}"
            options.append(discord.SelectOption(
                label=f"{label}",
                value=value,
                description="Current/Next check" if i == 0 else "Future week",
                default=value in existing_weeks
            ))
        
        options = options[:25]  # Discord limit
        
        # Pre-populate selected_weeks with existing exemptions that are in options
        self.selected_weeks = [opt.value for opt in options if opt.default]
        
        # Store original exemptions to detect changes
        self.original_exemptions = set(self.selected_weeks)
        self.original_reason = existing_reason
        
        # Track if permanent was selected before this interaction
        self.had_permanent = "permanent" in self.selected_weeks
        
        # Load existing reason
        self.reason = existing_reason
        
        self.week_select = discord.ui.Select(
            placeholder="Select weeks to exempt...",
            min_values=0,
            max_values=len(options),
            options=options
        )
        self.week_select.callback = self.select_callback
        self.add_item(self.week_select)
        
        # Only reset if not pre-populated
        if not hasattr(self, 'selected_weeks'):
            self.selected_weeks = []
        
        # Add Reason button
        self.reason_btn = discord.ui.Button(
            label="✏️ Edit Reason" if self.reason else "📝 Add Reason",
            style=discord.ButtonStyle.primary if self.reason else discord.ButtonStyle.secondary,
            row=1
        )
        self.reason_btn.callback = self.reason_callback
        self.add_item(self.reason_btn)
        
        # Add Clear All button (only show if there are existing exemptions)
        has_existing = len(self.original_exemptions) > 0 or self.original_reason
        self.clear_btn = discord.ui.Button(
            label="🗑️ Clear All",
            style=discord.ButtonStyle.danger,
            row=1,
            disabled=not has_existing
        )
        self.clear_btn.callback = self.clear_callback
        self.add_item(self.clear_btn)
        
        # Add Confirm button (disabled until changes are made)
        self.confirm_btn = discord.ui.Button(
            label="Confirm",
            style=discord.ButtonStyle.success,
            row=2,
            disabled=True
        )
        self.confirm_btn.callback = self.confirm_callback
        self.add_item(self.confirm_btn)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who used the command can interact with this.",
                ephemeral=True
            )
            return False
        return True
    
    async def reason_callback(self, interaction: discord.Interaction):
        modal = ReasonModal(self)
        await interaction.response.send_modal(modal)
    
    async def clear_callback(self, interaction: discord.Interaction):
        # Clear all selections and reason
        self.selected_weeks = []
        self.reason = None
        self.had_permanent = False
        
        # Update select options
        for option in self.week_select.options:
            option.default = False
        
        # Update reason button
        self.reason_btn.label = "📝 Add Reason"
        self.reason_btn.style = discord.ButtonStyle.secondary
        
        # Enable confirm if this is a change and state is valid
        selection_changed = set(self.selected_weeks) != self.original_exemptions
        reason_changed = self.reason != self.original_reason
        has_changes = selection_changed or reason_changed
        # Don't allow reason without weeks
        invalid_state = self.reason and not self.selected_weeks
        self.confirm_btn.disabled = not has_changes or invalid_state
        
        await self._update_embed(interaction)
    
    async def _update_embed(self, interaction: discord.Interaction):
        """Update the embed with current selection and reason"""
        # Format selected weeks for preview
        week_labels = []
        for week_value in self.selected_weeks:
            if week_value == "permanent":
                week_labels.append("🔒 **Permanent**")
            else:
                start_str, end_str = week_value.split("_")
                start_date = datetime.fromisoformat(start_str).date()
                end_date = datetime.fromisoformat(end_str).date()
                if start_date.month == end_date.month:
                    label = f"{start_date.strftime('%b %d')} - {end_date.strftime('%d')}, {end_date.year}"
                else:
                    label = f"{start_date.strftime('%b %d')} - {end_date.strftime('%b %d')}, {end_date.year}"
                week_labels.append(label)
        
        if week_labels:
            description = f"Exempting **{self.target_user.mention}** for:\n\n" + "\n".join([f"• {label}" for label in week_labels])
        else:
            description = f"No weeks selected for **{self.target_user.mention}**.\n\n**Click Confirm to remove all exemptions.**"
        
        if self.reason:
            description += f"\n\n**Reason:** {self.reason}"
        
        if week_labels:
            description += "\n\n**Click Confirm to save.**"
        
        embed = discord.Embed(
            title="🛡️ Inactivity Exemption",
            description=description,
            color=0xFFA500 if not week_labels else 0x5865f2,
            timestamp=datetime.now(timezone.utc)
        )
        
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def select_callback(self, interaction: discord.Interaction):
        self.selected_weeks = list(self.week_select.values)
        
        # Handle permanent option toggle
        has_permanent_now = "permanent" in self.selected_weeks
        has_other_weeks = any(w != "permanent" for w in self.selected_weeks)
        
        if has_permanent_now and has_other_weeks:
            if self.had_permanent:
                # Permanent was already selected, user added other weeks -> remove permanent
                self.selected_weeks = [w for w in self.selected_weeks if w != "permanent"]
            else:
                # User just selected permanent -> keep only permanent
                self.selected_weeks = ["permanent"]
        
        # Update tracking for next interaction
        self.had_permanent = "permanent" in self.selected_weeks
        
        # Update the select options to reflect current selection
        for option in self.week_select.options:
            option.default = option.value in self.selected_weeks
        
        # Only enable confirm if selection or reason has changed and state is valid
        selection_changed = set(self.selected_weeks) != self.original_exemptions
        reason_changed = self.reason != self.original_reason
        has_changes = selection_changed or reason_changed
        # Don't allow reason without weeks
        invalid_state = self.reason and not self.selected_weeks
        self.confirm_btn.disabled = not has_changes or invalid_state
        
        await self._update_embed(interaction)
    
    async def confirm_callback(self, interaction: discord.Interaction):
        # Load current exemptions
        exemptions = load_exemptions()
        user_key = str(self.target_user.id)
        
        old_weeks, _ = get_user_exemption_data(self.target_user.id)
        old_exemptions = set(old_weeks)
        new_exemptions = set(self.selected_weeks)
        
        # Calculate what was added and removed
        added = new_exemptions - old_exemptions
        removed = old_exemptions - new_exemptions
        
        # Update exemptions to exactly match selection (new format with reason)
        if new_exemptions or self.reason:
            exemptions[user_key] = {
                "weeks": list(new_exemptions),
                "reason": self.reason
            }
        elif user_key in exemptions:
            del exemptions[user_key]
        
        # Save exemptions
        if save_exemptions(exemptions):
            # Check if permanent was selected
            is_permanent = "permanent" in new_exemptions
            
            # Format week labels for display
            def format_weeks(weeks):
                labels = []
                for week_value in weeks:
                    if week_value == "permanent":
                        labels.append("🔒 **Permanent**")
                    else:
                        start_str, end_str = week_value.split("_")
                        start_date = datetime.fromisoformat(start_str).date()
                        end_date = datetime.fromisoformat(end_str).date()
                        if start_date.month == end_date.month:
                            label = f"{start_date.strftime('%b %d')} - {end_date.strftime('%d')}, {end_date.year}"
                        else:
                            label = f"{start_date.strftime('%b %d')} - {end_date.strftime('%b %d')}, {end_date.year}"
                        labels.append(label)
                return labels
            
            if not new_exemptions:
                embed = discord.Embed(
                    title="✅ Exemptions Cleared",
                    description=f"All exemptions removed for **{self.target_user.mention}**.",
                    color=0x00FF00,
                    timestamp=datetime.now(timezone.utc)
                )
            elif is_permanent:
                description = f"**{self.target_user.mention}** is now **permanently exempt** from all inactivity checks."
                if self.reason:
                    description += f"\n\n**Reason:** {self.reason}"
                embed = discord.Embed(
                    title="✅ Exemptions Updated",
                    description=description,
                    color=0x00FF00,
                    timestamp=datetime.now(timezone.utc)
                )
            else:
                week_labels = format_weeks(new_exemptions)
                description = f"**{self.target_user.mention}** is now exempt for:\n\n" + "\n".join([f"• {label}" for label in week_labels])
                if self.reason:
                    description += f"\n\n**Reason:** {self.reason}"
                embed = discord.Embed(
                    title="✅ Exemptions Updated",
                    description=description,
                    color=0x00FF00,
                    timestamp=datetime.now(timezone.utc)
                )
            
            # Show what changed
            changes = []
            if added:
                changes.append(f"Added: {len(added)}")
            if removed:
                changes.append(f"Removed: {len(removed)}")
            if changes:
                embed.set_footer(text=" | ".join(changes))
        else:
            embed = discord.Embed(
                title="❌ Error",
                description="Failed to save exemptions.",
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc)
            )
        
        await interaction.response.edit_message(embed=embed, view=None)


class SetupView(discord.ui.View):
    """Setup view for configuring inactivity check parameters"""
    def __init__(self, author_id: int, start_date, end_date, is_second_check: bool, bot):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.start_date = start_date
        self.end_date = end_date
        self.is_second_check = is_second_check
        self.bot = bot
        self.min_playtime_hours = DEFAULT_MIN_PLAYTIME_HOURS
        
        # Add playtime adjustment buttons
        self.decrease_10_btn = discord.ui.Button(label="-10h", style=discord.ButtonStyle.secondary, row=0)
        self.decrease_10_btn.callback = self.decrease_10_callback
        self.add_item(self.decrease_10_btn)
        
        self.decrease_1_btn = discord.ui.Button(label="-1h", style=discord.ButtonStyle.secondary, row=0)
        self.decrease_1_btn.callback = self.decrease_1_callback
        self.add_item(self.decrease_1_btn)
        
        self.increase_1_btn = discord.ui.Button(label="+1h", style=discord.ButtonStyle.secondary, row=0)
        self.increase_1_btn.callback = self.increase_1_callback
        self.add_item(self.increase_1_btn)
        
        self.increase_10_btn = discord.ui.Button(label="+10h", style=discord.ButtonStyle.secondary, row=0)
        self.increase_10_btn.callback = self.increase_10_callback
        self.add_item(self.increase_10_btn)
        
        # Add Continue and Cancel buttons
        self.continue_btn = discord.ui.Button(label="Continue", style=discord.ButtonStyle.success, row=1)
        self.continue_btn.callback = self.continue_callback
        self.add_item(self.continue_btn)
        
        self.cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
        self.cancel_btn.callback = self.cancel_callback
        self.add_item(self.cancel_btn)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who used the command can interact with this.",
                ephemeral=True
            )
            return False
        return True
    
    def _get_setup_embed(self):
        """Generate the setup embed with current settings"""
        if self.start_date.month == self.end_date.month:
            week_display = f"{self.start_date.strftime('%b %d')} - {self.end_date.strftime('%d')}, {self.end_date.year}"
        elif self.start_date.year == self.end_date.year:
            week_display = f"{self.start_date.strftime('%b %d')} - {self.end_date.strftime('%b %d')}, {self.end_date.year}"
        else:
            week_display = f"{self.start_date.strftime('%b %d, %Y')} - {self.end_date.strftime('%b %d, %Y')}"
        
        check_type = "Second Check" if self.is_second_check else "First Check"
        
        embed = discord.Embed(
            title="⚙️ Inactivity Check Setup",
            description=f"**{check_type}**\n{week_display}\n\nConfigure the settings below:",
            color=0x5865f2,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Minimum Playtime", value=f"**{self.min_playtime_hours}** hours", inline=True)
        return embed
    
    async def _update_message(self, interaction: discord.Interaction):
        embed = self._get_setup_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def decrease_10_callback(self, interaction: discord.Interaction):
        self.min_playtime_hours = max(0, self.min_playtime_hours - 10)
        await self._update_message(interaction)
    
    async def decrease_1_callback(self, interaction: discord.Interaction):
        self.min_playtime_hours = max(0, self.min_playtime_hours - 1)
        await self._update_message(interaction)
    
    async def increase_1_callback(self, interaction: discord.Interaction):
        self.min_playtime_hours = self.min_playtime_hours + 1
        await self._update_message(interaction)
    
    async def increase_10_callback(self, interaction: discord.Interaction):
        self.min_playtime_hours = self.min_playtime_hours + 10
        await self._update_message(interaction)
    
    async def cancel_callback(self, interaction: discord.Interaction):
        await interaction.message.delete()
    
    async def continue_callback(self, interaction: discord.Interaction):
        await run_inactivity_check(
            interaction,
            self.start_date,
            self.end_date,
            self.is_second_check,
            self.min_playtime_hours
        )


class WeekSelectView(discord.ui.View):
    def __init__(self, author_id: int, second_check: bool = False, bot=None):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.selected_week = None
        self.is_second_check = second_check
        self.bot = bot
        
        # Get previous weeks based on check type
        weeks = get_previous_weeks(5, second_check=second_check)
        
        # Create options
        options = []
        for i, (label, start_date, end_date) in enumerate(weeks):
            # Value format: "YYYY-MM-DD_YYYY-MM-DD" (start_end)
            value = f"{start_date.isoformat()}_{end_date.isoformat()}"
            description = "Most recent" if i == 0 else f"{i + 1} weeks ago"
            options.append(discord.SelectOption(
                label=label,
                value=value,
                description=description
            ))
        
        # Create and add the select menu
        self.week_select = discord.ui.Select(
            placeholder="Select a week to check...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.week_select.callback = self.select_callback
        self.add_item(self.week_select)
        
        # Add First Check button (disabled by default)
        self.first_check_btn = discord.ui.Button(
            label="First Check",
            style=discord.ButtonStyle.primary,
            disabled=not second_check,  # Disabled when in first check mode
            row=1
        )
        self.first_check_btn.callback = self.first_check_callback
        self.add_item(self.first_check_btn)
        
        # Add Second Check button (enabled by default)
        self.second_check_btn = discord.ui.Button(
            label="Second Check",
            style=discord.ButtonStyle.primary,
            disabled=second_check,  # Disabled when in second check mode
            row=1
        )
        self.second_check_btn.callback = self.second_check_callback
        self.add_item(self.second_check_btn)
        
        # Add Cancel button
        self.cancel_btn = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            row=1
        )
        self.cancel_btn.callback = self.cancel_callback
        self.add_item(self.cancel_btn)
    
    async def cancel_callback(self, interaction: discord.Interaction):
        """Cancel and delete the message"""
        await interaction.message.delete()
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only allow the original author to interact"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who used the command can interact with this.",
                ephemeral=True
            )
            return False
        return True
    
    async def first_check_callback(self, interaction: discord.Interaction):
        """Switch to First Check mode (Monday to Sunday)"""
        # Create new embed for first check
        embed = discord.Embed(
            title="📋 Inactivity Check - First Check",
            description="Select a week to check for player inactivity.\n\n**Mode:** Monday to Sunday (full week)",
            color=0x5865f2,
            timestamp=datetime.now(timezone.utc)
        )
        
        weeks = get_previous_weeks(5, second_check=False)
        weeks_info = "\n".join([f"• {label}" for label, _, _ in weeks])
        embed.add_field(
            name="Available Weeks",
            value=weeks_info,
            inline=False
        )
        
        # Create new view with first check mode
        new_view = WeekSelectView(author_id=self.author_id, second_check=False, bot=self.bot)
        await interaction.response.edit_message(embed=embed, view=new_view)
    
    async def second_check_callback(self, interaction: discord.Interaction):
        """Switch to Second Check mode (Monday to Tuesday)"""
        # Create new embed for second check
        embed = discord.Embed(
            title="📋 Inactivity Check - Second Check",
            description="Select a week to check for player inactivity.\n\n**Mode:** Monday to Tuesday",
            color=0xFFA500,
            timestamp=datetime.now(timezone.utc)
        )
        
        weeks = get_previous_weeks(5, second_check=True)
        weeks_info = "\n".join([f"• {label}" for label, _, _ in weeks])
        embed.add_field(
            name="Available Weeks",
            value=weeks_info,
            inline=False
        )
        
        # Create new view with second check mode
        new_view = WeekSelectView(author_id=self.author_id, second_check=True, bot=self.bot)
        await interaction.response.edit_message(embed=embed, view=new_view)
    
    async def select_callback(self, interaction: discord.Interaction):
        selected_value = self.week_select.values[0]
        start_str, end_str = selected_value.split("_")
        
        start_date = datetime.fromisoformat(start_str).date()
        end_date = datetime.fromisoformat(end_str).date()
        
        # Show setup view
        setup_view = SetupView(
            author_id=self.author_id,
            start_date=start_date,
            end_date=end_date,
            is_second_check=self.is_second_check,
            bot=self.bot
        )
        embed = setup_view._get_setup_embed()
        await interaction.response.edit_message(embed=embed, view=setup_view)


async def run_inactivity_check(interaction: discord.Interaction, start_date, end_date, is_second_check: bool, min_playtime_hours: int):
    """Run the actual inactivity check with the configured settings"""
    # Format the selected week nicely
    if start_date.month == end_date.month:
        week_display = f"{start_date.strftime('%b %d')} - {end_date.strftime('%d')}, {end_date.year}"
    elif start_date.year == end_date.year:
        week_display = f"{start_date.strftime('%b %d')} - {end_date.strftime('%b %d')}, {end_date.year}"
    else:
        week_display = f"{start_date.strftime('%b %d, %Y')} - {end_date.strftime('%b %d, %Y')}"
    
    check_type = "Second Check (Mon-Tue)" if is_second_check else "First Check (Mon-Sun)"
    
    # Show processing embed
    processing_embed = discord.Embed(
        title="📋 Inactivity Check",
        description=f"**Check Type:** {check_type}\n**Selected Period:** {week_display}\n**Min Playtime:** {min_playtime_hours}h\n\n⏳ Processing player data...",
        color=0xFFA500 if is_second_check else 0x5865f2,
        timestamp=datetime.now(timezone.utc)
    )
    await interaction.response.edit_message(embed=processing_embed, view=None)
    
    # Get the guild database closest to end date (preferring after)
    end_guild_db = get_guild_db_for_date(end_date)
    start_guild_db = get_guild_db_for_date(start_date)
    print(f"[INAC_CHECK] End guild db: {end_guild_db}, Start guild db: {start_guild_db}")
    
    if not end_guild_db or not start_guild_db:
        error_embed = discord.Embed(
            title="❌ Error",
            description="No guild database found for the selected period.",
            color=0xFF0000,
            timestamp=datetime.now(timezone.utc)
        )
        await interaction.edit_original_response(embed=error_embed)
        return
    
    end_players = set(get_players_from_guild_db(end_guild_db))
    start_players = set(get_players_from_guild_db(start_guild_db))
    
    if not end_players and not start_players:
        error_embed = discord.Embed(
            title="❌ Error",
            description="No players found in the guild databases.",
            color=0xFF0000,
            timestamp=datetime.now(timezone.utc)
        )
        await interaction.edit_original_response(embed=error_embed)
        return
    
    players_in_both = end_players & start_players
    players_left = start_players - end_players
    players_new = end_players - start_players
    
    days_with_data, total_days, missing_dates = count_available_days_in_period(start_date, end_date)
    print(f"[INAC_CHECK] Days with data: {days_with_data}, Total days: {total_days}, Missing dates: {missing_dates}")
    
    if days_with_data == 0 and SKIP_IF_NO_DATA:
        error_embed = discord.Embed(
            title="❌ No Playtime Data",
            description=f"No playtime tracking data found for the selected period.\n\n**Expected folders:**\n" + 
                       "\n".join([f"• `playtime_{d}`" for d in missing_dates[:5]]) +
                       (f"\n• ...and {len(missing_dates) - 5} more" if len(missing_dates) > 5 else ""),
            color=0xFF0000,
            timestamp=datetime.now(timezone.utc)
        )
        error_embed.add_field(
            name="Playtime Folder Path",
            value=f"`{PLAYTIME_TRACKING_FOLDER}`",
            inline=False
        )
        await interaction.edit_original_response(embed=error_embed)
        return
    
    player_playtimes = []
    for player in players_in_both:
        total_playtime = get_total_playtime_for_period(player, start_date, end_date)
        player_playtimes.append((player, total_playtime))
    
    player_playtimes.sort(key=lambda x: x[1])
    
    # Calculate stats
    min_playtime_seconds = min_playtime_hours * 3600
    total_playtime_all = sum(p[1] for p in player_playtimes)
    avg_playtime = total_playtime_all // len(player_playtimes) if player_playtimes else 0
    zero_playtime_count = sum(1 for p in player_playtimes if p[1] == 0)
    below_minimum_count = sum(1 for p in player_playtimes if p[1] < min_playtime_seconds)
    
    # Split players into active and inactive
    inactive_players = [(u, p) for u, p in player_playtimes if p < min_playtime_seconds]
    active_players = [(u, p) for u, p in player_playtimes if p >= min_playtime_seconds]
    # Sort active by playtime descending
    active_players.sort(key=lambda x: x[1], reverse=True)
    
    # Match inactive players to Discord users (needed for report)
    username_matches = load_username_matches()
    matched_inactive = []
    unmatched_inactive = []
    exempt_inactive = []  # (username, playtime_secs, discord_id, reason)
    
    for username, playtime_secs in inactive_players:
        discord_id = username_matches.get(username.lower())
        if discord_id:
            # Check if user is exempt for this week
            if is_user_exempt(discord_id, start_date, end_date):
                _, reason = get_user_exemption_data(discord_id)
                exempt_inactive.append((username, playtime_secs, discord_id, reason))
            else:
                matched_inactive.append((username, playtime_secs, discord_id))
        else:
            unmatched_inactive.append((username, playtime_secs))
    
    # Calculate percentages
    total_members = len(player_playtimes) + len(players_new)
    inactive_pct = (len(inactive_players) / total_members * 100) if total_members > 0 else 0
    active_pct = (len(active_players) / total_members * 100) if total_members > 0 else 0
    
    # Create the report file
    report_lines = []
    report_lines.append("INACTIVITY CHECK REPORT")
    report_lines.append("=" * 80)
    report_lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    report_lines.append(f"Period: {week_display} ({check_type})")
    report_lines.append(f"Threshold: {min_playtime_hours} hours in {total_days} days")
    report_lines.append(f"Data Coverage: {days_with_data}/{total_days} days")
    if missing_dates:
        report_lines.append(f"Missing dates: {', '.join(missing_dates)}")
    report_lines.append("")
    report_lines.append("STATISTICS")
    report_lines.append("-" * 80)
    report_lines.append(f"Total Members: {total_members}")
    report_lines.append(f"Inactive Members (Matched): {len(matched_inactive)}")
    report_lines.append(f"Inactive Members (Unmatched): {len(unmatched_inactive)}")
    report_lines.append(f"Exempt Members: {len(exempt_inactive)}")
    report_lines.append(f"Active Members: {len(active_players)} ({active_pct:.1f}%)")
    report_lines.append(f"New Members: {len(players_new)}")
    report_lines.append(f"Left Guild: {len(players_left)}")
    report_lines.append("")
    
    max_username_len = max((len(p[0]) for p in player_playtimes), default=8)
    username_width = max(max_username_len, 20)
    
    # Inactive members (matched) section
    report_lines.append(f"INACTIVE MEMBERS - MATCHED (< {min_playtime_hours} hours)")
    report_lines.append("=" * 80)
    
    header = f"{'Rank':<6} {'Username':<{username_width}} {'Playtime':<20}"
    report_lines.append(header)
    report_lines.append("-" * 80)
    
    if matched_inactive:
        for idx, (username, playtime_secs, _) in enumerate(matched_inactive, 1):
            playtime_hrs = playtime_secs / 3600
            line = f"{idx:<6} {username:<{username_width}} {playtime_hrs:>8.1f} hrs"
            report_lines.append(line)
    else:
        report_lines.append("No matched inactive members!")
    
    report_lines.append("")
    
    # Inactive members (unmatched) section
    report_lines.append(f"INACTIVE MEMBERS - UNMATCHED (< {min_playtime_hours} hours)")
    report_lines.append("=" * 80)
    
    header = f"{'Rank':<6} {'Username':<{username_width}} {'Playtime':<20}"
    report_lines.append(header)
    report_lines.append("-" * 80)
    
    if unmatched_inactive:
        for idx, (username, playtime_secs) in enumerate(unmatched_inactive, 1):
            playtime_hrs = playtime_secs / 3600
            line = f"{idx:<6} {username:<{username_width}} {playtime_hrs:>8.1f} hrs"
            report_lines.append(line)
    else:
        report_lines.append("No unmatched inactive members!")
    
    report_lines.append("")
    
    # Exempt members section
    if exempt_inactive:
        report_lines.append(f"EXEMPT MEMBERS (< {min_playtime_hours} hours but exempted)")
        report_lines.append("=" * 80)
        
        header = f"{'Rank':<6} {'Username':<{username_width}} {'Playtime':<20}"
        report_lines.append(header)
        report_lines.append("-" * 80)
        
        for idx, (username, playtime_secs, _, reason) in enumerate(exempt_inactive, 1):
            playtime_hrs = playtime_secs / 3600
            reason_text = f" - {reason}" if reason else ""
            line = f"{idx:<6} {username:<{username_width}} {playtime_hrs:>8.1f} hrs{reason_text}"
            report_lines.append(line)
    
        report_lines.append("")
    
    # New members section
    if players_new:
        report_lines.append(f"NEW MEMBERS (joined during period)")
        report_lines.append("=" * 80)
        header = f"{'Username':<{username_width}} {'Playtime':<30}"
        report_lines.append(header)
        report_lines.append("-" * 80)
        
        new_player_data = []
        for player in players_new:
            total_pt = get_total_playtime_for_period(player, start_date, end_date)
            new_player_data.append((player, total_pt))
        new_player_data.sort(key=lambda x: x[0].lower())  # Sort alphabetically
        
        for username, playtime_secs in new_player_data:
            playtime_hrs = playtime_secs / 3600
            playtime_days = playtime_hrs / 24
            line = f"{username:<{username_width}} {playtime_hrs:>8.1f} hrs ({playtime_days:.1f} days)"
            report_lines.append(line)
        report_lines.append("")
    
    # Active members section
    if active_players:
        report_lines.append(f"ACTIVE MEMBERS (>= {min_playtime_hours} hours)")
        report_lines.append("=" * 80)
        header = f"{'Rank':<6} {'Username':<{username_width}} {'Playtime':<20}"
        report_lines.append(header)
        report_lines.append("-" * 80)
        
        for idx, (username, playtime_secs) in enumerate(active_players, 1):
            playtime_hrs = playtime_secs / 3600
            line = f"{idx:<6} {username:<{username_width}} {playtime_hrs:>8.1f} hrs"
            report_lines.append(line)
        
        report_lines.append("")
        active_total = sum(p[1] for p in active_players)
        active_avg = active_total / len(active_players) if active_players else 0
        report_lines.append(f"Total: {len(active_players)} members")
        report_lines.append(f"Average playtime: {active_avg / 3600:.1f} hours")
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
        f.write("\n".join(report_lines))
        temp_file_path = f.name
    
    file_attachment = discord.File(
        temp_file_path, 
        filename=f"inactivity_check_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.txt"
    )
    
    
    # Generate warning text or kick commands depending on check type
    warning_text = None
    kick_commands = None
    if matched_inactive:
        if is_second_check:
            # Second check: generate /gu kick commands
            kick_commands = [f"/gu kick {username}" for username, _, _ in matched_inactive]
        else:
            # First check: generate warning message
            inactive_mentions = [f"<@{discord_id}>" for _, _, discord_id in matched_inactive]
            hours_display = int(min_playtime_hours) if min_playtime_hours % 1 == 0 else min_playtime_hours
            
            warning_suffix = (
                f" you have been warned because you haven't reached the playtime requirement of "
                f"**{hours_display} hours** this week, without giving notice.\n\n"
                f"If you wish to stay in the guild or you think this is an error, you have 48 hours to either reach the required playtime or "
                f"state the reason of your inactivity in <#629912948948598825>/DM a "
                f"[Recruitment Manager](https://discord.com/channels/554418045397762048/1381292106928095312/1381292106928095312).\n\n"
                f"⚠️ Being active on the Hero beta does not count towards activity ⚠️"
            )
            
            # Combine all mentions into one warning text
            all_mentions = " ".join(inactive_mentions)
            warning_text = f"{all_mentions}{warning_suffix}"
    
    
    result_embed = discord.Embed(
        title="📋 Inactivity Check Complete",
        description=f"**{check_type}**\n{week_display}\n**Threshold:** {min_playtime_hours}h in {total_days} days",
        color=0x00FF00,
        timestamp=datetime.now(timezone.utc)
    )
    result_embed.add_field(name="Total Members", value=f"**{total_members}**", inline=True)
    result_embed.add_field(name="Inactive (Matched)", value=f"**{len(matched_inactive)}**", inline=True)
    result_embed.add_field(name="Inactive (Unmatched)", value=f"**{len(unmatched_inactive)}**", inline=True)
    result_embed.add_field(name="Exempt", value=f"**{len(exempt_inactive)}**", inline=True)
    result_embed.add_field(name="Active", value=f"**{len(active_players)}** ({active_pct:.1f}%)", inline=True)
    result_embed.add_field(name="New Members", value=f"**{len(players_new)}**", inline=True)
    result_embed.add_field(name="Data Coverage", value=f"**{days_with_data}/{total_days}** days", inline=True)
    
    # Add warning text or kick commands as embed field, or as txt attachment if too long
    attachments = [file_attachment]
    extra_file_path = None
    
    if kick_commands:
        # Second check: show /gu kick commands in code blocks
        kick_code_blocks = "".join([f"```\n{cmd}\n```" for cmd in kick_commands])
        if len(kick_code_blocks) <= 1024:
            result_embed.add_field(name="🔨 Kick Commands to Copy", value=kick_code_blocks, inline=False)
        else:
            # Too long for embed field, create a separate txt file
            kick_commands_text = "\n".join(kick_commands)
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
                f.write(kick_commands_text)
                extra_file_path = f.name
            kick_attachment = discord.File(extra_file_path, filename="kick_commands.txt")
            attachments.append(kick_attachment)
            result_embed.add_field(name="🔨 Kick Commands", value="Too many commands for embed - see attached `kick_commands.txt`", inline=False)
    elif warning_text:
        # First check: show warning message
        code_block = f"```\n{warning_text}\n```"
        if len(code_block) <= 1024:
            result_embed.add_field(name="⚠️ Warning Message to Copy", value=code_block, inline=False)
        else:
            # Too long for embed field, create a separate txt file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
                f.write(warning_text)
                extra_file_path = f.name
            warning_attachment = discord.File(extra_file_path, filename="warning_message_to_copy.txt")
            attachments.append(warning_attachment)
            result_embed.add_field(name="⚠️ Warning Message", value="Too long for embed - see attached `warning_message_to_copy.txt`", inline=False)
    
    await interaction.edit_original_response(embed=result_embed, attachments=attachments)
    
    try:
        os.unlink(temp_file_path)
        if extra_file_path:
            os.unlink(extra_file_path)
    except:
        pass


def setup(bot, has_required_role, config):
    """Setup function for bot integration"""

    @bot.tree.command(
        name="inactivity_check",
        description="Check player inactivity for a specific week"
    )
    async def inactivity_check(interaction: discord.Interaction):
        """Check player inactivity for a selected week"""
        
        # Cleanup expired exemptions
        cleanup_expired_exemptions()
        
        # Check permissions
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc)
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        # Create embed with week selector (default to First Check mode)
        embed = discord.Embed(
            title="📋 Inactivity Check - First Check",
            description="Select a week to check for player inactivity.\n\n**Mode:** Monday to Sunday (full week)",
            color=0x5865f2,
            timestamp=datetime.now(timezone.utc)
        )
        
        # Add info about available weeks
        weeks = get_previous_weeks(5, second_check=False)
        weeks_info = "\n".join([f"• {label}" for label, _, _ in weeks])
        embed.add_field(
            name="Available Weeks",
            value=weeks_info,
            inline=False
        )
        
        view = WeekSelectView(author_id=interaction.user.id, second_check=False, bot=bot)
        await interaction.response.send_message(embed=embed, view=view)
    
    print("[OK] Loaded inactivity_check command")
    
    @bot.tree.command(
        name="inactivity_manage",
        description="Manage inactivity exemptions for a user"
    )
    @app_commands.describe(user="The user to manage exemptions for")
    async def inactivity_manage(interaction: discord.Interaction, user: discord.Member):
        """Manage inactivity exemptions for a user"""
        
        # Cleanup expired exemptions
        cleanup_expired_exemptions()
        
        # Check permissions
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc)
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        # Check if user is restricted (Juror) - show read-only view
        if is_restricted_user(interaction.user):
            existing_weeks, existing_reason = get_user_exemption_data(user.id)
            
            embed = discord.Embed(
                title="🛡️ Inactivity Exemption",
                description=f"Exemption info for **{user.mention}**",
                color=0x5865f2,
                timestamp=datetime.now(timezone.utc)
            )
            
            if existing_weeks:
                current_exempts = []
                for week_value in existing_weeks:
                    if week_value == "permanent":
                        current_exempts.append("🔒 **Permanent**")
                    else:
                        try:
                            start_str, end_str = week_value.split("_")
                            start_date = datetime.fromisoformat(start_str).date()
                            end_date = datetime.fromisoformat(end_str).date()
                            if start_date.month == end_date.month:
                                label = f"{start_date.strftime('%b %d')} - {end_date.strftime('%d')}, {end_date.year}"
                            else:
                                label = f"{start_date.strftime('%b %d')} - {end_date.strftime('%b %d')}, {end_date.year}"
                            current_exempts.append(label)
                        except (ValueError, AttributeError):
                            continue
                
                exemption_text = "\n".join([f"• {label}" for label in current_exempts])
                embed.add_field(
                    name="Exempt Weeks",
                    value=exemption_text,
                    inline=False
                )
            else:
                embed.add_field(
                    name="Exempt Weeks",
                    value="*No exemptions set*",
                    inline=False
                )
            
            embed.add_field(
                name="Reason",
                value=existing_reason if existing_reason else "*No reason provided*",
                inline=False
            )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        # Full access - show management view
        # Create embed with week selector
        embed = discord.Embed(
            title="🛡️ Inactivity Exemption",
            description=f"Select weeks to exempt **{user.mention}** from inactivity checks.\n\nYou can select multiple weeks.",
            color=0x5865f2,
            timestamp=datetime.now(timezone.utc)
        )
        
        # Show current exemptions for this user
        existing_weeks, existing_reason = get_user_exemption_data(user.id)
        if existing_weeks:
            current_exempts = []
            for week_value in existing_weeks:
                if week_value == "permanent":
                    current_exempts.append("🔒 **Permanent**")
                else:
                    start_str, end_str = week_value.split("_")
                    start_date = datetime.fromisoformat(start_str).date()
                    end_date = datetime.fromisoformat(end_str).date()
                    if start_date.month == end_date.month:
                        label = f"{start_date.strftime('%b %d')} - {end_date.strftime('%d')}, {end_date.year}"
                    else:
                        label = f"{start_date.strftime('%b %d')} - {end_date.strftime('%b %d')}, {end_date.year}"
                    current_exempts.append(label)
            
            exemption_text = "\n".join([f"• {label}" for label in current_exempts[-10:]])
            if existing_reason:
                exemption_text += f"\n\n**Reason:** {existing_reason}"
            
            embed.add_field(
                name="Current Exemptions",
                value=exemption_text,
                inline=False
            )
        
        view = ExemptWeekSelectView(author_id=interaction.user.id, target_user=user, bot=bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    print("[OK] Loaded inactivity_manage command")
