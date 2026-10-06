# ESI-Bot

A Discord bot for the **Empire of Sindria (ESI)** guild in Wynncraft. It handles guild administration (applications, tickets, recruitment, ranks, inactivity, blacklist) alongside Wynncraft data tracking (playtime, wars, guild raids, territories, ESI points) and a set of community features.

For end-user command documentation, see the **[Sindrian Bot Handbook](https://docs.google.com/document/d/11HXnJ4-4Pyh_auHa3kesnbHKGUtG4hYr/edit?usp=sharing)**.

---

## Features

- **Applications & recruitment** — ticket panels, forwarded applications, voting buttons, recruitment profiles, and a guild waiting queue
- **Alt checking** — cross-references Mojang, Ashcon, GeyserMC, vrc.lol and Laby.net into a scored verdict
- **Member management** — rank changes, demotions, Duke onboarding, Discord↔IGN linking, inactivity exemptions and a public hub
- **Tracking** — playtime, wars, guild raids, quest points, aspects, territories/claim snipes, recruitment and event points
- **ESI points** — rolling two-week EP cycles, awarded automatically from tracked deltas
- **Moderation** — spam detection, per-command bans, blacklist management, venting cleanup
- **Community** — birthdays, auto-reactions, ship scores, `@grok` replies, temporary voice channels, welcome messages
- **Operations** — `/reload` hot-reload, daily restart at 00:00 UTC, crash-restart loop, graceful shutdown, usage analytics

---

## Requirements

- **Python 3.13+** (developed and tested on 3.13)
- **`screen`** on Linux, for the management scripts (`sudo apt-get install screen`)
- A **Discord bot token** and one or more **Wynncraft API keys**

The bot requires the **Server Members**, **Message Content**, and **Presence** privileged intents enabled in the Discord Developer Portal.

---

## Dependencies

Third-party packages used by the bot, trackers, and scripts:

- `discord.py` (2.4+, for `tasks.loop(time=...)` and app commands)
- `python-dotenv`
- `aiohttp`
- `Pillow` — image rendering for `/player`, `/sus`, `/get_uniform`, `/ship`
- `matplotlib` — charts for `/playtime`, `/warcount`, `/graidcount`, `/recruitment`
- `requests` / `urllib3` — skin and texture downloads

Install them with:

```bash
python -m pip install "discord.py>=2.4" python-dotenv aiohttp Pillow matplotlib requests
```

Or, if a `requirements.txt` is present:

```bash
bash scripts/install_dependencies.sh
```

> `scripts/install_dependencies.sh` installs from `requirements.txt` at the repository root and exits with an error if that file is missing.

---

## Setup

**1. Clone the repository and enter it:**

```bash
git clone <repo-url>
cd ESI-Bot
```

**2. Create a `.env` file in the repository root:**

```env
DISCORD_TOKEN=your_discord_bot_token
OWNER_ID=your_discord_user_id

WYNNCRAFT_KEY_1=your_wynncraft_api_key
WYNNCRAFT_KEY_2=your_wynncraft_api_key
WYNNCRAFT_KEY_3=your_wynncraft_api_key
```

**3. Install dependencies** (see [Dependencies](#dependencies)).

**4. Start the bot** (see [Running the Bot](#running-the-bot)).

### Environment variables

Different parts of the project read different key slots, so it is worth filling in the slots you actually use:

| Variable | Required | Read by | Purpose |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | Yes | `bot.py`, `scripts/`, `tests/` | Discord bot token |
| `OWNER_ID` | Yes | `bot.py` and most commands | Bot owner's Discord user ID. Bypasses all role checks and is the only user allowed to run `/reload` and `/shutdown` |
| `WYNNCRAFT_KEY_1` … `WYNNCRAFT_KEY_3` | Recommended | `bot.py` | Rotated round-robin by the bot's own Wynncraft API client |
| `WYNNCRAFT_KEY_1` … `WYNNCRAFT_KEY_6` | Recommended | `trackers/api_tracker.py` | Member stat tracking (also rotates through the keys it finds) |
| `WYNNCRAFT_KEY_7` | Recommended | `trackers/guild_tracker.py`, `trackers/claim_tracker.py`, `/guild_tracker`, `/claim_tracker` | Guild membership and territory tracking |
| `WYNNCRAFT_KEY_11` | Recommended | `trackers/playtime_tracker.py`, `/playtime`, `/fetch_playtime`, `/get_uniform` | Playtime tracking |
| `RUN_LIVE_TESTS` | No | `tests/` | Set to `1` to run tests that hit live Minecraft APIs |

Notes:

- The bot starts without any Wynncraft keys, but prints a warning and all Wynncraft-backed features will fail.
- Key slots are deliberately spread across components so that heavy trackers do not exhaust the same rate limit as interactive commands.

---

## Running the Bot

The bot and the background trackers are separate processes, each managed in its own `screen` session (`esi-bot` and `esi-trackers`).

| Action | Command |
| --- | --- |
| Start the bot | `bash scripts/start_bot.sh` |
| Start the trackers | `bash scripts/start_trackers.sh` |
| Stop the bot | `bash scripts/stop_bot.sh` |
| Stop the trackers | `bash scripts/stop_trackers.sh` |
| Restart the bot | `bash scripts/restart_bot.sh` |
| Restart the trackers | `bash scripts/restart_trackers.sh` |
| View live bot logs | `bash scripts/view_bot_logs.sh` |
| View live tracker logs | `bash scripts/view_tracker_logs.sh` |

To detach from a session without stopping it, press `Ctrl+A` then `D`.

The bot also restarts itself every day at **00:00** and will re-launch after a crash (up to 5 consecutive crashes within 10 seconds, then it gives up to avoid a restart loop).

---

## Project Structure

```
ESI-Bot/
├── bot.py                      # Entry point: bot class, loader, built-in commands
├── .env                        # Environment variables (not committed)
├── commands/                   # Slash commands and event listeners, by category
│   ├── badges/                 # Quest/recruitment/war stat badges
│   ├── fun/                    # Birthdays, auto-reactions, ship, @grok, crab
│   ├── guild/                  # Guild API fetch, tracking, points, role export
│   ├── members/                # Ranks, onboarding, queue, linking, inactivity
│   ├── moderation/             # Bans, spam detection, venting cleanup
│   ├── server/                 # Support tickets, welcome messages
│   ├── tickets/                # Applications, panels, alt check, blacklist
│   ├── tracking/               # Playtime, wars, graids, aspects, claims, points
│   └── vc_generator/           # Temporary voice channel generator
├── trackers/                   # Standalone background processes
│   ├── main.py                 # Runs all trackers concurrently
│   ├── api_tracker.py          # Guild member stats snapshots + EP awards
│   ├── playtime_tracker.py     # Online playtime sampling
│   ├── guild_tracker.py        # Guild membership/rank change detection
│   ├── claim_tracker.py        # Territory/claim change detection
│   └── recover_baseline.py     # Repair tool for points_baseline.db
├── utils/                      # Shared helpers (paths, permissions, API, points…)
├── scripts/                    # Shell management scripts + standalone Python tools
├── config/                     # JSON configuration (welcome, activity logger)
├── data/                       # Persistent JSON state (queues, tickets, birthdays…)
├── databases/                  # SQLite databases and daily snapshots
├── images/                     # Uniforms, icons and other static assets
├── exports/                    # Output of the channel history exporter (gitignored)
└── tests/                      # Local-only dev/test scripts (gitignored)
```

Most paths are resolved from the repository root via `utils/paths.py` (or `Path(__file__).parents[2]`), but a few modules still use working-directory-relative paths such as `images/pink_heart.png` and `databases/recruited_data.db`. Start the bot from the repository root — the provided scripts already `cd` there.

---

## Commands

Commands are discovered by scanning `commands/**/*.py` at startup — every file that is not prefixed with `_` is loaded, and any command it registers is synced to Discord.

### Built-in (`bot.py`)

| Command | Description |
| --- | --- |
| `/ping` | Check bot latency |
| `/reload` | Tear down and reload every command module (owner only) |
| `/shutdown` | Shut the bot down (owner only) |

### Badges

| Command | Description |
| --- | --- |
| `/badges` | Display quest, recruitment, and war statistics for all players |
| `/update_badges` | Check all guild members for missing or wrong badge roles |

### Fun

| Command | Description |
| --- | --- |
| `/birthday_channel` | Set the channel for birthday announcements |
| `/birthday_add` | Add a birthday for a user |
| `/birthday_remove` | Remove a birthday for a user |
| `/birthday_list` | List all birthdays |
| `/birthday_test` | Trigger the birthday announcement immediately |
| `/auto_react_manage` | View, add, and remove auto-reactions |
| `/ship` | Ship two users and see their compatibility |
| `/ship_force` | Force a ship score between two users |
| `/ship_remove` | Remove a forced ship score |
| `/ship_list` | List all ship scores for today |

### Guild

| Command | Description |
| --- | --- |
| `/fetch_api` | Fetch guild information from the Wynncraft API |
| `/guild_tracker` | Toggle guild member tracking notifications |
| `/exportroles` | Export all server roles to a JSON file |
| `/view_points` | View the ESI points leaderboard or a specific player's points |

### Members

| Command | Description |
| --- | --- |
| `/demote` | Demote a user to a lower rank |
| *Demote User* | Context menu — demote a user |
| `/get_uniform` | Render your Minecraft character wearing an ESI uniform |
| `/inactivity_check` | Check player inactivity for a specific week |
| `/inactivity_manage` | Manage inactivity exemptions for a user |
| `/inactivity_hub_setup` | Set up the Inactivity Exemption Hub in a channel |
| `/refresh_inactivity_requests` | Refresh open inactivity request views and the public roster |
| `/manage_queue` | Manage the guild member waiting queue |
| `/link_user` | Link a Discord user to their Minecraft IGN |
| `/linked_users` | List all stored username matches |

### Moderation

| Command | Description |
| --- | --- |
| `/ban` | Ban a user from the bot or from specific commands |
| `/ban_remove` | Remove a ban from a user |
| `/ban_check` | Check whether a user is banned |
| `/nuke_venting` | Remove all messages from a channel except protected ones |

### Server

| Command | Description |
| --- | --- |
| `/contact_support` | Send a message to the bot owner |
| `/refresh_support_tickets` | Refresh all open support ticket views (owner only) |
| `/welcome_channel` | Set the channel for welcome messages |

### Tickets

| Command | Description |
| --- | --- |
| `/accept` | Check which roles a user needs for a specific rank |
| *Accept User* | Context menu — check a user's roles |
| *Alt Check* | Context menu — run the alt-check verdict on a user |
| `/blacklist_check` | Check a username, or browse the full blacklist with `%all%` |
| `/blacklist_manage` | Open the blacklist manager (add, retract, list, search) |
| `/app_notifications` | Toggle application notifications on or off |
| `/manage_tickets` | Manage forwarded application tickets |
| `/player` | Generate a visual Wynncraft player card |
| *Recruitment Profile* | Context menu — show a recruitment profile |
| `/setup_applications` | Configure application settings for a ticket panel |
| `/sus` | Generate a visual sus card for a player |
| `/refresh_ticket_buttons` | Refresh all ticket control buttons |
| `/refresh_vote_buttons` | Refresh all application vote buttons |
| `/panel_setup` | Set up the ticket panel |
| `/panel_toggle` | Enable or disable a ticket panel |
| `/panel_move` | Move a ticket panel to a different channel |
| `/panel_debug` | Debug panel registration status |
| `/panel_refresh` | Refresh all panel buttons (fixes broken buttons) |

### Tracking

| Command | Description |
| --- | --- |
| `/playtime` | View a player's daily playtime over a time period |
| `/fetch_playtime` | Force a playtime fetch from the Wynncraft API |
| `/warcount` | Check how many wars a player participated in over a time period |
| `/graidcount` | Check how many guild raids a player participated in over a time period |
| `/quest_points` | Add or remove quest points for a player |
| `/aspects` | View and manage guild raid aspects (2 graids = 1 aspect) |
| `/recruitment` | Add or delete a recruitment record |
| `/event_points` | Add or remove event points for a player |
| `/event_esi_points` | Award ESI points to a player as an event reward |
| `/claim_tracker` | Toggle territory tracking notifications |
| `/claim_snipe` | Plan a claim snipe, managing players interactively |

### Temporary voice channels

| Command | Description |
| --- | --- |
| `/vc_setup` | Configure the temporary VC generator |
| `/vc_presets` | Manage your saved temp VC presets |
| `/vc_manage` | Open the temporary VC management panel |
| `/vc_knock` | Request access to a locked or hidden temporary VC |

### Automatic (no command)

Some modules register event listeners instead of commands:

- **Auto-reactions** and **birthday announcements**
- **Crab hello** and **`@grok` replies** on message content
- **Spam detection** — link/spam heuristics with per-channel role restrictions
- **Welcome messages** for new members
- **Role sync** and **Duke onboarding** on `on_member_update`
- **Rank logging** to `databases/rank_changes.db`
- **Usage analytics** — every completed slash command is counted in `databases/usage.db`

---

## Background Trackers

`trackers/main.py` runs all four trackers concurrently in a single process, staggering API calls one second apart to stay inside rate limits.

| Tracker | Interval | Key slot | What it does |
| --- | --- | --- | --- |
| `api_tracker` | 300s | `WYNNCRAFT_KEY_1`–`6` | Snapshots guild member stats, awards ESI points from deltas, tracks aspects |
| `playtime_tracker` | 300s | `WYNNCRAFT_KEY_11` | Samples online players and accumulates playtime, with daily snapshots |
| `guild_tracker` | 30s | `WYNNCRAFT_KEY_7` | Detects joins, leaves, and rank changes for the tracked guild |
| `claim_tracker` | 3s | `WYNNCRAFT_KEY_7` | Detects territory gain/loss for the tracked guild |

Both `guild_tracker` and `claim_tracker` read their target guild from `data/tracked_guild.json` / `data/guild_territories.json`, which are set up through the bot's `/guild_tracker` and `/claim_tracker` commands. Until a guild is configured they idle.

### `trackers/recover_baseline.py`

A repair tool for `databases/points_baseline.db`. If the tracker was offline long enough for counters to reset (for example a guild raid reset), the baseline can go stale and award spurious ESI points. This script rebuilds the baseline from a known-good `databases/api_tracking/api_*/ESI_*.db` snapshot using the same offset logic as the tracker:

```bash
python trackers/recover_baseline.py --snapshot "databases/api_tracking/api_01-01-2026/ESI_01012026.db"
python trackers/recover_baseline.py --snapshot "<...>" --dry-run
```

Stop the tracker before running it, then start it again afterwards. It never modifies `esi_points.db` or the snapshot itself.

---

## Utility Scripts

| Script | Purpose |
| --- | --- |
| `scripts/export_channel_history.py` | Export full message history (and optionally attachments) from one or more channels. Run with `--help` for the full set of options |
| `scripts/find_missing_jurors.py` | List members holding a nobility rank (Viscount…Archduke) but not the Juror role |
| `scripts/search_temp_vc_messages.py` | Search and summarize the temp VC message metadata database |

All three are read-only or export-only and take their Discord token from the project `.env`.

---

## Storage

- `data/*.json` — persistent state: queues, pending applications, ticket panels, birthdays, tracked guilds, inactivity requests, username matches, command bans, auto-reactions, and more. Written at runtime.
- `databases/*.db` — SQLite databases: `esi_points.db`, `points_baseline.db`, `blacklist.db`, `rank_changes.db`, `recruited_data.db`, `playtime_tracking.db`, `temp_vc_messages.db`, `usage.db`, and others. Created automatically on first use.
- `databases/api_tracking/` and `databases/playtime_tracking/` — per-day snapshot folders, pruned automatically as they age.
- `config/*.json` — committed configuration: `welcome.json` (welcome channel) and `activity_logger.json` (tracked guilds).
- `images/` — uniforms, icons, and other static assets used when rendering cards.
- `exports/` — output directory for the channel history exporter.

Databases, `data/` state files, `.env`, and `exports/` are all gitignored.

---

## Development

### Adding a command

Create a `.py` file anywhere under `commands/` (files starting with `_` are skipped by the loader) and register your command inside a `setup()` function:

```python
import discord
from discord import app_commands

def setup(bot, has_required_role, config):
    @bot.tree.command(name="my_command", description="Does a thing")
    async def my_command(interaction: discord.Interaction):
        await interaction.response.send_message("Hello!", ephemeral=True)
```

The loader inspects `setup()`'s signature and passes `bot` plus the helpers it declares, so `def setup(bot)` and `def setup(bot, has_required_role, config)` both work. Defining a `setup()` is optional — a module that only exposes a `commands.Cog` subclass is added automatically.

To clean up background tasks on reload, expose a module-level `teardown(bot)`.

After adding or editing a command, either restart the bot or run `/reload` (owner only), which tears down and reloads every command module and re-syncs the command tree.

### Permissions

Commands check permissions through `utils/permissions.has_roles(user, role_ids)`. At load time the bot patches this function so that:

- the user matching `OWNER_ID` always passes,
- any Discord administrator always passes,
- in the Parliament server (`802999599060221992`), non-admin-only commands bypass role checks entirely.

`bot.py` also installs a global interaction check that blocks banned users from any command they have been banned from, using `data/user_bans.json`.

### Testing

`tests/` holds local development scripts and is gitignored, so a fresh clone will not have it. Tests are plain scripts unless noted:

```bash
python tests/test_grok_reply_listener.py
python tests/test_send_war_camp_announcement.py   # dry run, never sends
python -m unittest tests.test_alt_check_minecraft
```

The Minecraft tests are offline by default; set `RUN_LIVE_TESTS=1` to hit the real APIs.

---

## License

See [LICENSE](LICENSE) for details.
