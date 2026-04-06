import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import json
import subprocess
import asyncio
import signal
import atexit
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, time
import sys
import importlib.util
import inspect

# Import diagnostics
try:
    from bot_diagnostics import setup_logging
except ImportError:
    def setup_logging():
        pass  # Fallback if diagnostics not available

# Import player stats scheduler
try:
    from player_stats_scheduler import PlayerStatsScheduler, PlayerStatsConfig
except ImportError:
    PlayerStatsScheduler = None
    PlayerStatsConfig = None
    print("[WARNING] Player stats scheduler not available")

# ============================================================================
# SIGNAL HANDLING & GRACEFUL SHUTDOWN
# ============================================================================

_shutdown_in_progress = False

def _signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    global _shutdown_in_progress
    
    if _shutdown_in_progress:
        print("[SHUTDOWN] Force quit requested")
        sys.exit(1)
    
    _shutdown_in_progress = True
    print(f"[SHUTDOWN] Received signal {signum}, shutting down gracefully...")

# Register signal handlers
signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

def _cleanup_on_exit():
    """Cleanup when process exits"""
    if not _shutdown_in_progress:
        print("[SHUTDOWN] Process exiting, cleaning up...")

atexit.register(_cleanup_on_exit)

# Load environment variables from .env file
load_dotenv()

# Bot configuration
TOKEN = os.getenv('DISCORD_TOKEN')
OWNER_ID = int(os.getenv('OWNER_ID', 0))
ALWAYS_ALLOWED_USER_ID = OWNER_ID
PARLIAMENT_GUILD_ID = 802999599060221992  # No permission requirements except admin-only commands

# Wynncraft API Keys (3 keys for rate limit rotation)
WYNNCRAFT_KEYS = [
    os.getenv('WYNNCRAFT_KEY_1'),
    os.getenv('WYNNCRAFT_KEY_2'),
    os.getenv('WYNNCRAFT_KEY_3')
]
# Filter out None values if keys aren't set
WYNNCRAFT_KEYS = [key for key in WYNNCRAFT_KEYS if key]

if not TOKEN:
    raise ValueError("DISCORD_TOKEN not found in .env file!")

if not WYNNCRAFT_KEYS:
    print("[WARNING] No Wynncraft API keys found in .env file!")
    print("          Add WYNNCRAFT_KEY_1, WYNNCRAFT_KEY_2, and WYNNCRAFT_KEY_3 to use Wynncraft API")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # Required to see all guild members
intents.presences = True  # Optional: allows seeing member status

# Create command folders if they don't exist
# Use absolute path based on this file's location to avoid CWD issues
_BOT_DIR = Path(__file__).resolve().parent
PYTHON_COMMANDS_DIR = _BOT_DIR / 'commands'
PYTHON_COMMANDS_DIR.mkdir(exist_ok=True)

print(f"[OK] Commands directory: {PYTHON_COMMANDS_DIR} ({len(list(PYTHON_COMMANDS_DIR.rglob('*.py')))} .py files)")

class WynncraftAPI:
    """Helper class for Wynncraft API requests with key rotation"""
    BASE_URL = "https://api.wynncraft.com/v3"
    current_key_index = 0
    _session = None
    _session_lock = asyncio.Lock()
    
    @classmethod
    async def get_session(cls):
        """Get or create aiohttp session (thread-safe)"""
        import aiohttp
        async with cls._session_lock:
            if cls._session is None or cls._session.closed:
                cls._session = aiohttp.ClientSession()
            return cls._session
    
    @classmethod
    def get_next_key(cls):
        """Rotate through available API keys"""
        if not WYNNCRAFT_KEYS:
            return None
        key = WYNNCRAFT_KEYS[cls.current_key_index]
        cls.current_key_index = (cls.current_key_index + 1) % len(WYNNCRAFT_KEYS)
        return key
    
    @classmethod
    async def request(cls, endpoint, params=None):
        """Make a request to Wynncraft API with automatic key rotation"""
        key = cls.get_next_key()
        if not key:
            return {"error": "No Wynncraft API keys configured"}
        
        headers = {"apikey": key}
        url = f"{cls.BASE_URL}/{endpoint}"
        
        try:
            session = await cls.get_session()
            async with session.get(url, headers=headers, params=params, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    return {"error": f"API returned status {response.status}", "status": response.status}
        except asyncio.TimeoutError:
            return {"error": "Request timed out"}
        except asyncio.CancelledError:
            raise  # Re-raise cancellation errors
        except Exception as e:
            return {"error": str(e)}
    
    @classmethod
    async def close(cls):
        """Close the session properly"""
        async with cls._session_lock:
            if cls._session and not cls._session.closed:
                await cls._session.close()
                print("[API] Session closed successfully")

class MultiLangBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.wynncraft_api = WynncraftAPI
        self.should_restart = False
        
        # Stability tracking
        self._ready = False
        self._closing = False
        self._background_tasks = []
        
        # Player stats scheduler
        self.stats_scheduler = None
        
        # Add global interaction check for ban system
        self.tree.interaction_check = self.global_interaction_check
        
    async def global_interaction_check(self, interaction: discord.Interaction) -> bool:
        """Global check for all interactions - checks if user is banned from command"""
        try:
            # Import ban checker from bot root directory
            from utils.bans import is_user_banned
            
            # Get command name from interaction
            if not interaction.command:
                return True  # Not a command interaction, allow it
            
            command_name = interaction.command.name
            
            # Check if user is banned from this command
            ban_info = is_user_banned(interaction.user.id, command_name)
            
            if ban_info:
                # User is banned, send ban message
                from datetime import datetime, timezone
                
                reason = ban_info.get("reason", "")
                
                # All bans are permanent
                ban_message = "You are **permanently banned** from using this command."
                
                if reason:
                    ban_message += f"\n**Reason:** {reason}"
                
                ban_message += "\n\nIf you think this is a mistake, please contact a staff member or use `/contact_support` to get in touch with the bot owner."
                
                embed = discord.Embed(
                    title="🚫 Command Banned",
                    description=ban_message,
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc)
                )
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return False  # Block command execution
            
            return True  # User is not banned, allow command
            
        except Exception as e:
            print(f"[BAN_CHECK] Error in global interaction check: {e}")
            import traceback
            traceback.print_exc()
            return True  # On error, allow command to proceed
    
    async def setup_hook(self):
        """Called when bot is starting up"""
        try:
            await self.load_commands()
            
            await self.tree.sync()
            print("[OK] Slash commands synced!")
            
            # Start the daily restart task
            self.daily_restart.start()
            print("[OK] Daily restart scheduled for 00:00")
            
            # Initialize player stats scheduler
            if PlayerStatsScheduler and PlayerStatsConfig:
                try:
                    # Hardcoded to track Empire of Sindria guild
                    TRACKED_GUILD = "Empire of Sindria"
                    
                    self.stats_scheduler = PlayerStatsScheduler(
                        self.wynncraft_api,
                        player_list=[],
                        guild_list=[TRACKED_GUILD]
                    )
                    # Start the scheduler
                    self.stats_scheduler.start()
                    print(f"[OK] Player stats scheduler started (tracking guild: {TRACKED_GUILD})")
                except Exception as e:
                    print(f"[WARNING] Failed to start player stats scheduler: {e}")
            
        except Exception as e:
            print(f"[ERROR] Failed during setup_hook: {e}")
            import traceback
            traceback.print_exc()
    
    @tasks.loop(time=time(hour=0, minute=0))
    async def daily_restart(self):
        """Restart the bot every day at 00:00"""
        try:
            print(f"[RESTART] Daily restart triggered at {datetime.now()}")
            self.should_restart = True
            await self.close()
        except Exception as e:
            print(f"[ERROR] Error during daily restart: {e}")
    
    @daily_restart.before_loop
    async def before_daily_restart(self):
        """Wait until the bot is ready before starting the task"""
        await self.wait_until_ready()
    
    def has_required_role(self, user, role_ids=None):
        """Helper function to check if user has required roles"""
        if not role_ids:
            return True
        user_role_ids = [role.id for role in user.roles]
        return any(role_id in user_role_ids for role_id in role_ids)
    
    def get_server_config(self, guild_id):
        """Helper function to get server config"""
        return {}
    
    def get_command_names_from_cog(self, cog):
        """Extract command names from a cog"""
        command_names = []
        
        # Get app commands (slash commands)
        if hasattr(cog, '__cog_app_commands__'):
            for cmd in cog.__cog_app_commands__:
                command_names.append(cmd.name)
        
        # Get regular commands
        if hasattr(cog, '__cog_commands__'):
            for cmd in cog.__cog_commands__:
                command_names.append(cmd.name)
        
        return command_names
    
    async def load_python_commands(self):
        """Load Python cogs from files and subfolders"""
        loaded_by_folder = {}
        total_files = 0
        loaded_files = 0
        failed_commands = []  # Track failed commands with errors
        
        # Get all Python files recursively
        python_files = list(PYTHON_COMMANDS_DIR.rglob('*.py'))
        
        for file in python_files:
            # Skip files starting with underscore
            if file.stem.startswith('_'):
                continue
            
            total_files += 1
            
            # Get the relative path from PYTHON_COMMANDS_DIR
            rel_path = file.relative_to(PYTHON_COMMANDS_DIR)
            
            # Determine the folder
            if rel_path.parent == Path('.'):
                folder_name = "root"
            else:
                folder_name = str(rel_path.parent)
            
            try:
                # Load module directly
                module_name = f"commands.{rel_path.stem}"
                spec = importlib.util.spec_from_file_location(module_name, file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                
                # Patch has_roles to always allow the designated user and Discord admins
                if hasattr(module, 'has_roles') and callable(module.has_roles):
                    def _make_patched(orig):
                        def patched(user, *args, **kwargs):
                            if user.id == ALWAYS_ALLOWED_USER_ID:
                                return True
                            if hasattr(user, 'guild_permissions') and user.guild_permissions.administrator:
                                return True
                            # Parliament server: bypass permissions for non-admin-only commands
                            if hasattr(user, 'guild') and user.guild and user.guild.id == PARLIAMENT_GUILD_ID:
                                role_ids = args[0] if args else kwargs.get('required_roles', kwargs.get('role_ids', []))
                                try:
                                    is_admin_only = role_ids and all(
                                        int(r) == ALWAYS_ALLOWED_USER_ID for r in role_ids if r
                                    )
                                except (ValueError, TypeError):
                                    is_admin_only = False
                                if not is_admin_only:
                                    return True
                            return orig(user, *args, **kwargs)
                        return patched
                    module.has_roles = _make_patched(module.has_roles)
                    print(f"[PATCH] Patched has_roles in {rel_path}")
                
                command_names = []
                
                # Check if module has a setup() function
                if hasattr(module, 'setup'):
                    print(f"[INFO] Found setup() in {rel_path}")
                    
                    # Inspect the setup function
                    sig = inspect.signature(module.setup)
                    param_count = len(sig.parameters)
                    
                    # Track cogs before
                    cogs_before = set(self.cogs.keys())
                    
                    # Call setup with appropriate arguments
                    if param_count == 1:
                        result = module.setup(self)
                    elif param_count == 2:
                        result = module.setup(self, self.has_required_role)
                    elif param_count >= 3:
                        result = module.setup(self, self.has_required_role, self.get_server_config)
                    else:
                        result = module.setup()
                    
                    # Await if coroutine
                    if asyncio.iscoroutine(result):
                        await result
                    
                    # Check for new cogs
                    cogs_after = set(self.cogs.keys())
                    new_cogs = cogs_after - cogs_before
                    
                    # Extract command names from new cogs
                    for cog_name in new_cogs:
                        cog = self.cogs[cog_name]
                        cmd_names = self.get_command_names_from_cog(cog)
                        command_names.extend(cmd_names)
                    
                    if not command_names:
                        command_names = [f"<{file.stem}>"]
                    
                    print(f"[OK] Loaded {rel_path} - Commands: {', '.join(command_names)}")
                
                # Look for Cog classes (alternative method)
                else:
                    for item_name in dir(module):
                        item = getattr(module, item_name)
                        if (isinstance(item, type) and 
                            issubclass(item, commands.Cog) and 
                            item is not commands.Cog):
                            
                            cog_instance = item(self)
                            await self.add_cog(cog_instance)
                            command_names = self.get_command_names_from_cog(cog_instance)
                            
                            if not command_names:
                                command_names = [f"<{item_name}>"]
                            
                            print(f"[OK] Loaded cog {rel_path} ({item_name}) - Commands: {', '.join(command_names)}")
                            break
                
                # Store by folder
                if folder_name not in loaded_by_folder:
                    loaded_by_folder[folder_name] = []
                
                loaded_by_folder[folder_name].extend(command_names)
                loaded_files += 1
                
            except Exception as e:
                command_identifier = f"{rel_path}" if rel_path else "unknown command"
                error_msg = str(e)
                
                # Store failed command info
                failed_commands.append({
                    'file': command_identifier,
                    'error': error_msg
                })
                
                print(f"[ERROR] Failed to load command: {command_identifier}")
                print(f"        Error: {e}")
                import traceback
                traceback.print_exc()
        
        # Store stats for reporting
        loaded_by_folder['_stats'] = {
            'loaded': loaded_files,
            'total': total_files,
            'failed': failed_commands
        }
        
        return loaded_by_folder

    async def load_commands(self):
        """Setup function called during bot startup"""
        python_cmds_by_folder = await self.load_python_commands()
        
        # Extract stats
        stats = python_cmds_by_folder.pop('_stats', {'loaded': 0, 'total': 0})
        
        print('=' * 60)
        print('LOADED PYTHON COMMANDS:')
        print('-' * 60)
        
        total_python = 0
        if python_cmds_by_folder:
            for folder, commands in sorted(python_cmds_by_folder.items()):
                print(f"[{folder}]:")
                if commands:
                    for cmd in commands:
                        print(f"  - {cmd}")
                    total_python += len(commands)
                else:
                    print(f"  (no commands)")
        else:
            print("  No Python commands loaded")
        
        print('-' * 60)
        print(f'Total Python commands: {total_python}')
        print(f'Loaded {stats["loaded"]}/{stats["total"]} command files successfully')
        
        # Show failed commands summary if any
        failed = stats.get('failed', [])
        if failed:
            print('\n' + '!' * 60)
            print(f'FAILED TO LOAD {len(failed)} COMMAND FILE(S):')
            print('!' * 60)
            for fail_info in failed:
                print(f"  ❌ {fail_info['file']}")
                print(f"     Error: {fail_info['error']}")
            print('!' * 60)
        
        print('=' * 60)
        
    async def close(self):
        """Override close to cancel tasks properly and prevent crashes"""
        global _shutdown_in_progress
        _shutdown_in_progress = True
        
        if self._closing:
            print("[WARNING] Close already in progress, skipping")
            return
        
        self._closing = True
        print("[SHUTDOWN] Initiating graceful shutdown sequence...")
        
        try:
            # Activity tracker removed - no cleanup needed
            
            # Step 2: Stop player stats scheduler
            if self.stats_scheduler:
                try:
                    print("[SHUTDOWN] Stopping player stats scheduler...")
                    self.stats_scheduler.stop()
                    print("[SHUTDOWN] Player stats scheduler stopped")
                except Exception as e:
                    print(f"[SHUTDOWN WARNING] Error stopping stats scheduler: {e}")
            
            # Step 3: Stop daily restart task (use stop() not cancel())
            if self.daily_restart.is_running():
                try:
                    print("[SHUTDOWN] Stopping daily restart task...")
                    self.daily_restart.stop()
                    print("[SHUTDOWN] Daily restart task stopped")
                except Exception as e:
                    print(f"[SHUTDOWN WARNING] Error stopping restart task: {e}")
            
            # Step 4: Cancel background tasks with better error handling
            if self._background_tasks:
                print(f"[SHUTDOWN] Cancelling {len(self._background_tasks)} background tasks...")
                # Create a list of tasks to cancel
                tasks_to_cancel = [t for t in self._background_tasks if not t.done()]
                
                # Cancel all at once
                for task in tasks_to_cancel:
                    task.cancel()
                
                # Wait for them to finish cancelling (with timeout)
                if tasks_to_cancel:
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                            timeout=2
                        )
                    except asyncio.TimeoutError:
                        print("[SHUTDOWN WARNING] Some background tasks didn't cancel in time")
                    except Exception as e:
                        print(f"[SHUTDOWN WARNING] Error cancelling background tasks: {e}")
            
            # Step 5: Close API session with proper timeout
            try:
                print("[SHUTDOWN] Closing Wynncraft API session...")
                await asyncio.wait_for(self.wynncraft_api.close(), timeout=5)
                print("[SHUTDOWN] API session closed")
            except asyncio.TimeoutError:
                print("[SHUTDOWN WARNING] API session close timed out")
            except asyncio.CancelledError:
                print("[SHUTDOWN WARNING] API session close was cancelled")
            except Exception as e:
                print(f"[SHUTDOWN WARNING] Error closing API session: {e}")
            
            # Step 6: Call parent close with error suppression
            try:
                print("[SHUTDOWN] Closing Discord connection...")
                await asyncio.wait_for(super().close(), timeout=10)
                print("[SHUTDOWN] Discord connection closed")
            except asyncio.TimeoutError:
                print("[SHUTDOWN WARNING] Discord close timed out")
            except asyncio.CancelledError:
                print("[SHUTDOWN WARNING] Discord close was cancelled")
            except Exception as e:
                print(f"[SHUTDOWN WARNING] Error closing Discord: {e}")
        
        except Exception as e:
            print(f"[SHUTDOWN ERROR] Unexpected error during shutdown: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            print("[SHUTDOWN] Shutdown sequence complete")

def create_bot():
    """Factory function to create a new bot instance"""
    bot = MultiLangBot()
    
    @bot.event
    async def on_ready():
        """Bot connected to Discord"""
        # Always check and fetch members, even on reconnect
        if not bot._ready:
            print(f'\n{bot.user} is now online!')
            print(f'Bot ID: {bot.user.id}')
            print(f'Guilds: {len(bot.guilds)}')
            print(f'Intents: members={bot.intents.members}, presences={bot.intents.presences}')
        else:
            print(f'[RECONNECT] {bot.user} reconnected to Discord')
        
        # Force fetch all members for each guild (run every time)
        for guild in bot.guilds:
            cached = len(guild.members)
            total = guild.member_count
            
            if not bot._ready:
                print(f'Guild "{guild.name}": {total} total members, cached: {cached} members')
            
            if cached < total:
                print(f'  Fetching all {total} members for "{guild.name}"...')
                try:
                    await guild.chunk()
                    print(f'  ✓ Successfully fetched {len(guild.members)} members')
                except Exception as e:
                    print(f'  ✗ Failed to fetch members: {e}')
                    print(f'  ⚠️  CRITICAL: Enable "Server Members Intent" in Discord Developer Portal!')
        
        if not bot._ready:
            print('-' * 50)
            bot._ready = True
            
            # Wait a moment for all modules to finish registering their on_ready handlers
            print("[STARTUP] Waiting for ticket handler to initialize...")
            await asyncio.sleep(2)  

        # Manually trigger refresh after startup (always run, not just first time)
        print("[STARTUP] Refreshing ticket panels, buttons, and vote buttons...")
        try:
            from commands.tickets.ticket_handler import refresh_all_panels_and_buttons
            await refresh_all_panels_and_buttons(bot)
        except Exception as e:
            print(f"[STARTUP WARNING] Failed to refresh ticket panels: {e}")
        
        # Restore support ticket views (always run, not just first time)
        print("[STARTUP] Restoring support ticket views...")
        try:
            if hasattr(bot, '_restore_support_ticket_views'):
                restored, failed = await bot._restore_support_ticket_views()
                print(f"[STARTUP] ✅ Support tickets restored: {restored} restored, {failed} failed")
            else:
                print("[STARTUP] ⚠️ Support ticket restore function not found")
        except Exception as e:
            print(f"[STARTUP WARNING] Failed to restore support tickets: {e}")
            import traceback
            traceback.print_exc()
                
    @bot.event
    async def on_error(event, *args, **kwargs):
        """Global error handler - prevents crashes from unhandled errors"""
        print(f"\n[ERROR] Unhandled error in {event}:")
        import traceback
        traceback.print_exc()
        # Bot continues running instead of crashing
    
    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        """Handle slash command errors"""
        print(f"[ERROR] Slash command error: {error}")
        
        # Prevent duplicate responses
        if interaction.response.is_done():
            try:
                await interaction.followup.send(
                    f"❌ Error: {str(error)[:100]}",
                    ephemeral=True
                )
            except:
                pass
        else:
            try:
                await interaction.response.send_message(
                    f"❌ Error: {str(error)[:100]}",
                    ephemeral=True
                )
            except:
                print("[ERROR] Could not send error message to user")
    
    @bot.tree.command(name='reload', description='Reload all commands (Owner only)')
    async def reload_commands(interaction: discord.Interaction):
        """Reload all commands (Owner only)"""
        if OWNER_ID and interaction.user.id != OWNER_ID:
            await interaction.response.send_message("[X] Only the bot owner can use this command!", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            print("\n" + "=" * 60)
            print("[RELOAD] Starting command reload...")
            print("=" * 60)
            
            # CRITICAL: Clear Python's module cache for command files
            print("[RELOAD] Clearing Python module cache...")
            modules_to_remove = []
            for module_name in list(sys.modules.keys()):
                # Remove any modules from commands directory
                if 'commands' in module_name or module_name.startswith('commands.'):
                    modules_to_remove.append(module_name)
                # Also remove related modules (ticket_handler, etc.)
                elif any(x in module_name for x in ['ticket_handler', 'fetch_api', 'player_stats']):
                    modules_to_remove.append(module_name)
            
            # Call teardown() on modules that have it before removing
            for module_name in modules_to_remove:
                module = sys.modules.get(module_name)
                if module and hasattr(module, 'teardown'):
                    try:
                        print(f"[RELOAD] Calling teardown() for {module_name}")
                        module.teardown(bot)
                    except Exception as teardown_error:
                        print(f"[RELOAD] Warning: teardown() failed for {module_name}: {teardown_error}")
            
            for module_name in modules_to_remove:
                del sys.modules[module_name]
                print(f"[RELOAD] Removed cached module: {module_name}")
            
            print(f"[RELOAD] Cleared {len(modules_to_remove)} cached modules")
            
            # Remove all cogs
            print("[RELOAD] Removing all cogs...")
            cog_count = len(bot.cogs)
            for cog_name in list(bot.cogs.keys()):
                await bot.remove_cog(cog_name)
            print(f"[RELOAD] Removed {cog_count} cogs")
            
            # Clear and reload
            print("[RELOAD] Clearing command tree...")
            bot.tree.clear_commands(guild=None)
            
            print("[RELOAD] Loading Python commands...")
            python_cmds_by_folder = await bot.load_python_commands()
            
            # Extract stats
            stats = python_cmds_by_folder.pop('_stats', {'loaded': 0, 'total': 0})
            
            # Re-add built-in commands
            print("[RELOAD] Re-adding built-in commands...")
            bot.tree.add_command(reload_commands)
            bot.tree.add_command(ping)
            bot.tree.add_command(shutdown)
            
            print("[RELOAD] Syncing command tree with Discord...")
            await bot.tree.sync()
            
            # Refresh ticket panels and buttons after reload
            print("[RELOAD] Refreshing ticket panels and buttons...")
            try:
                # Import refresh function
                from commands.tickets.ticket_handler import refresh_all_panels_and_buttons
                await refresh_all_panels_and_buttons(bot)
                print("[RELOAD] ✅ Ticket panels and buttons refreshed")
            except Exception as refresh_error:
                print(f"[RELOAD] ⚠️ Warning: Could not refresh ticket panels/buttons: {refresh_error}")
            
            # Restore support ticket views
            print("[RELOAD] Restoring support ticket views...")
            support_restored = 0
            support_failed = 0
            try:
                if hasattr(bot, '_restore_support_ticket_views'):
                    print("[RELOAD] Found _restore_support_ticket_views, calling it...")
                    support_restored, support_failed = await bot._restore_support_ticket_views()
                    print(f"[RELOAD] ✅ Support ticket views restored: {support_restored} restored, {support_failed} failed")
                else:
                    print("[RELOAD] ⚠️ Warning: Support ticket restore function not found")
            except Exception as restore_error:
                print(f"[RELOAD] ⚠️ Warning: Could not restore support ticket views: {restore_error}")
                import traceback
                traceback.print_exc()
            
            total_python = sum(len(cmds) for folder, cmds in python_cmds_by_folder.items() if folder != '_stats')
            
            print("=" * 60)
            print(f"[RELOAD] ✅ Successfully reloaded {total_python} commands!")
            print(f"[RELOAD] Loaded {stats['loaded']}/{stats['total']} command files")
            print("=" * 60 + "\n")
            
            # Build reload summary message
            reload_msg = f"✅ Successfully reloaded {total_python} commands!\n"
            reload_msg += f"Loaded {stats['loaded']}/{stats['total']} files\n"
            reload_msg += f"Cleared {len(modules_to_remove)} cached modules\n"
            if support_restored > 0 or support_failed > 0:
                reload_msg += f"Support tickets: {support_restored} restored, {support_failed} failed"
            
            await interaction.followup.send(reload_msg, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"[ERROR] Error reloading: {e}", ephemeral=True)
            import traceback
            traceback.print_exc()
    
    @bot.tree.command(name='ping', description='Check bot latency')
    async def ping(interaction: discord.Interaction):
        """Test command to check if bot is responsive"""
        latency = round(bot.latency * 1000)
        await interaction.response.send_message(f'Pong! Latency: {latency}ms', ephemeral=True)
    
    @bot.tree.command(name='shutdown', description='Shutdown the bot (Owner only)')
    async def shutdown(interaction: discord.Interaction):
        """Shutdown the bot completely (Owner only)"""
        if OWNER_ID and interaction.user.id != OWNER_ID:
            await interaction.response.send_message("[X] Only the bot owner can use this command!", ephemeral=True)
            return
        
        await interaction.response.send_message("[SHUTDOWN] Shutting down bot...", ephemeral=True)
        bot.should_restart = False
        await bot.close()
    
    return bot

if __name__ == '__main__':
    # Setup logging once (before restart loop to avoid duplicate handlers)
    setup_logging()
    discord.utils.setup_logging()
    
    print("=" * 60)
    print("DISCORD BOT STARTUP")
    print("=" * 60)
    print("Starting Discord Bot with Slash Commands...")
    print("Loading configuration from .env file...")
    print("Note: Slash commands may take up to 1 hour to appear globally")
    print("=" * 60 + "\n")
    
    restart_count = 0
    max_consecutive_crashes = 5
    last_crash_time = None
    
    while True:
        try:
            # Safety check: don't allow infinite restart loops
            now = datetime.now()
            
            if last_crash_time and (now - last_crash_time).total_seconds() < 10:
                restart_count += 1
                if restart_count > max_consecutive_crashes:
                    print("[ERROR] Too many consecutive crashes! Exiting to prevent spam.")
                    break
            else:
                restart_count = 0  # Reset if more than 10 seconds between crashes
            
            last_crash_time = now
            
            # Clear discord log handlers to prevent duplicates on restart
            import logging
            logging.getLogger('discord').handlers.clear()
            
            bot = create_bot()
            bot.run(TOKEN, log_handler=None)
        except KeyboardInterrupt:
            print("\n[SHUTDOWN] Bot stopped by user (Ctrl+C)")
            break
        except Exception as e:
            print(f"[ERROR] Bot crashed: {e}")
            import traceback
            traceback.print_exc()
        
        # Check if we should restart
        if hasattr(bot, 'should_restart') and not bot.should_restart:
            print("[SHUTDOWN] Bot closed without restart flag - exiting")
            break
        
        # Don't spam restarts
        if restart_count >= max_consecutive_crashes:
            print("[ERROR] Maximum restart attempts exceeded - exiting")
            break
        
        print(f"[RESTART] Restarting bot in 5 seconds... (Restart count: {restart_count})")
        import time
        time.sleep(5)
        print("[RESTART] Creating new bot instance...")
    
    print("[EXIT] Bot has fully shut down")