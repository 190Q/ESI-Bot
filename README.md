# ESI-Bot

A Discord bot built for the Sindrian Isles guild in Wynncraft. It integrates with the Wynncraft API and handles recruitment, member tracking, ticket management, guild statistics, and more.

For full command documentation, see the **[Sindrian Bot Handbook](https://docs.google.com/document/d/11HXnJ4-4Pyh_auHa3kesnbHKGUtG4hYr/edit?usp=sharing)**.

---

## Requirements

- Python 3.13+
- `screen` (`sudo apt-get install screen`)
- A Discord bot token and Wynncraft API keys

---

## Setup

**1. Clone the repository and navigate into it:**
```bash
git clone <repo-url>
cd ESI-Bot
```

**2. Configure your environment:**

Copy or create a `.env` file in the root directory:
```env
DISCORD_TOKEN=your_discord_bot_token
OWNER_ID=your_discord_user_id
WYNNCRAFT_KEY_1=your_wynncraft_api_key
WYNNCRAFT_KEY_2=...
# Add more keys as needed (up to 12 supported)
```

**3. Install dependencies:**
```bash
bash scripts/install_dependencies.sh
```

---

## Running the Bot

The bot and its background trackers run as separate processes, each in their own `screen` session.

**Start the bot:**
```bash
bash scripts/start_bot.sh
```

**Start the background trackers:**
```bash
bash scripts/start_trackers.sh
```

**Stop the bot:**
```bash
bash scripts/stop_bot.sh
```

**Stop the trackers:**
```bash
bash scripts/stop_trackers.sh
```

**Restart the bot:**
```bash
bash scripts/restart_bot.sh
```

**Restart the trackers:**
```bash
bash scripts/restart_trackers.sh
```

**View live bot logs:**
```bash
bash scripts/view_bot_logs.sh
```

**View live tracker logs:**
```bash
bash scripts/view_tracker_logs.sh
```

> To detach from a screen session without stopping it, press `Ctrl+A` then `D`.

---

## Project Structure

```
ESI-Bot/
├── bot.py                  # Main bot entry point
├── .env                    # Environment variables (not committed)
├── scripts/                # Shell scripts for managing the bot
├── trackers/               # Background tracking processes
│   ├── main.py
│   ├── api_tracker.py
│   ├── claim_tracker.py
│   ├── guild_tracker.py
│   └── playtime_tracker.py
├── commands/               # Slash commands, organized by category
│   ├── badges/
│   ├── fun/
│   ├── guild/
│   ├── members/
│   ├── moderation/
│   ├── server/
│   ├── tickets/
│   └── tracking/
├── data/                   # Persistent JSON data files
├── config/                 # Configuration JSON files
├── images/                 # Static images and uniforms
└── utils/                  # Shared utilities
```

---

## Commands

For detailed usage of each command, refer to the **[Sindrian Bot Handbook](https://docs.google.com/document/d/11HXnJ4-4Pyh_auHa3kesnbHKGUtG4hYr/edit?usp=sharing)**.

---

## License

See [LICENSE](LICENSE) for details.