import asyncio
import discord
import os
import json
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from time import monotonic
from typing import Optional, Dict, Any, List
from utils.paths import DATA_DIR, DB_DIR

PARLIAMENT_ROLE_ID = 600185623474601995
TTS_BOT_ROLE_ID = 1295411931338899632
PRIVILEGED_ROLE_IDS = [
    rid
    for rid in dict.fromkeys([PARLIAMENT_ROLE_ID, TTS_BOT_ROLE_ID])
    if rid
]
PRIVILEGED_ROLE_ID_SET = set(PRIVILEGED_ROLE_IDS)
OWNER_ID = int(os.getenv("OWNER_ID")) if os.getenv("OWNER_ID") else 0
REQUIRED_ADMIN_ROLES = [rid for rid in dict.fromkeys([OWNER_ID, *PRIVILEGED_ROLE_IDS]) if rid]

MAX_PERMIT_TARGETS_PER_ACTION = 10
MAX_STORED_LOG_ENTRIES = 200
DEFAULT_GENERATOR_CHANNEL_NAME = "Create VC"
MAX_USER_PRESETS_PER_USER = 20
MAX_USER_PRESET_NAME_LENGTH = 40
OVERWRITE_BULK_RETRY_COOLDOWN_SECONDS = 20
OVERWRITE_BULK_RETRY_NOTICE_COOLDOWN_SECONDS = 30
OVERWRITE_TARGET_RETRY_COOLDOWN_SECONDS = 120

TEMP_VC_CONFIG_FILE = DATA_DIR / "temp_vc_config.json"
TEMP_VC_STATE_DIR = DATA_DIR / "temp_vc_channels"
LEGACY_TEMP_VC_STATE_FILE = DATA_DIR / "temp_vc_channels.json"
TEMP_VC_MESSAGES_DB_FILE = DB_DIR / "temp_vc_messages.db"

REGION_OPTIONS = [
    ("Automatic", "auto"),
    ("US East", "us-east"),
    ("US West", "us-west"),
    ("US Central", "us-central"),
    ("US South", "us-south"),
    ("Brazil", "brazil"),
    ("Rotterdam", "rotterdam"),
    ("Singapore", "singapore"),
    ("Sydney", "sydney"),
    ("Japan", "japan"),
    ("Hong Kong", "hongkong"),
    ("India", "india"),
    ("South Africa", "southafrica"),
    ("Russia", "russia"),
]

FALLBACK_TEMPLATE = {
    "display_name": "Default",
    "locked": False,
    "hidden": False,
    "push_to_talk": False,
    "user_limit": 0,
    "bitrate": 64000,
    "region": "auto",
    "name_format": "{owner}'s VC",
    "permitted_roles": [],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return datetime.now(timezone.utc)


def parse_mentioned_ids(raw_text: str) -> List[int]:
    found = re.findall(r"\d{15,22}", raw_text or "")
    unique = []
    seen = set()
    for item in found:
        value = int(item)
        if value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def clean_channel_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        return "Temporary VC"
    return text[:100]


def role_mention(role_id: int) -> str:
    return f"<@&{role_id}>"

class KnockResponseView(discord.ui.View):
    def __init__(self, system, channel_id: int, requester_id: int):
        super().__init__(timeout=900)
        self.system = system
        self.channel_id = channel_id
        self.requester_id = requester_id

    async def _resolve_context(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.channel_id) if interaction.guild else None
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            return None, None
        return channel, entry

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        channel, entry = await self._resolve_context(interaction)
        if not channel or not entry:
            await send_ephemeral(interaction, "This temporary VC is no longer available.")
            return False

        owner_id = int(entry.get("owner_id") or 0)
        if owner_id != interaction.user.id:
            await send_ephemeral(interaction, "Only the VC owner can respond to this knock request.")
            return False
        return True

    async def _delete_knock_message(self, interaction: discord.Interaction):
        message = interaction.message
        if message is None:
            return
        try:
            await message.delete()
        except Exception:
            pass

    def _strip_existing_user_permit(self, entry: Dict[str, Any], requester_id: int) -> bool:
        entry.setdefault("permitted_users", [])
        previous = [int(uid) for uid in entry.get("permitted_users", []) if str(uid).isdigit()]
        sanitized = [uid for uid in previous if int(uid) != int(requester_id)]
        changed = len(sanitized) != len(previous)
        entry["permitted_users"] = list(dict.fromkeys(sanitized))
        return changed

    @discord.ui.button(label="Allow", style=discord.ButtonStyle.success)
    async def allow_user(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        channel, entry = await self._resolve_context(interaction)
        if not channel or not entry:
            await interaction.followup.send("This temporary VC is no longer available.", ephemeral=True)
            return

        requester_id = int(self.requester_id)
        entry.setdefault("banned_users", [])
        entry.setdefault("banned_roles", [])
        banned_user_ids = {int(uid) for uid in entry.get("banned_users", []) if str(uid).isdigit()}
        banned_role_ids = {int(rid) for rid in entry.get("banned_roles", []) if str(rid).isdigit()}

        requester = interaction.guild.get_member(requester_id)
        if requester is None:
            try:
                requester = await interaction.guild.fetch_member(requester_id)
            except Exception:
                requester = None
        requester_role_ids = {role.id for role in requester.roles} if requester else set()

        if requester_id in banned_user_ids or (requester_role_ids & banned_role_ids):
            block_reason = "user is explicitly banned" if requester_id in banned_user_ids else "user has a banned role"
            self.system.add_log(
                entry,
                interaction.user.id,
                "knock_allow_blocked",
                f"Tried allowing banned requester {requester_id} ({block_reason})",
            )
            await self.system.upsert_entry(channel.id, entry)
            await interaction.followup.send(
                "That user is banned from this VC. Unban them from the Ban panel before allowing access.",
                ephemeral=True,
            )
            return
        had_existing_permit = self._strip_existing_user_permit(entry, requester_id)
        entry["permitted_users"].append(requester_id)
        entry["permitted_users"] = list(dict.fromkeys(entry["permitted_users"]))

        entry.setdefault("blocked_knock_users", [])
        entry["blocked_knock_users"] = [uid for uid in entry["blocked_knock_users"] if int(uid) != requester_id]
        details = f"Allowed {requester_id} to enter"
        if had_existing_permit:
            details += " (cleaned stale permit first)"
        self.system.add_log(entry, interaction.user.id, "knock_allowed", details)
        await self.system.sync_channel(channel, entry, reason=f"Knock allowed by {interaction.user}")
        await self.system.upsert_entry(channel.id, entry)
        if requester:
            try:
                await requester.send(
                    f"✅ Your knock request for **{channel.name}** in **{interaction.guild.name}** was accepted."
                )
            except Exception:
                pass

        await self._delete_knock_message(interaction)
        self.stop()

    @discord.ui.button(label="Refuse", style=discord.ButtonStyle.danger)
    async def refuse_user(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        channel, entry = await self._resolve_context(interaction)
        if not channel or not entry:
            await interaction.followup.send("This temporary VC is no longer available.", ephemeral=True)
            return

        requester_id = int(self.requester_id)
        had_existing_permit = self._strip_existing_user_permit(entry, requester_id)
        entry.setdefault("banned_users", [])
        if requester_id not in entry["banned_users"]:
            entry["banned_users"].append(requester_id)
        entry["banned_users"] = list(dict.fromkeys(entry["banned_users"]))
        entry.setdefault("blocked_knock_users", [])
        entry["blocked_knock_users"] = [uid for uid in entry["blocked_knock_users"] if int(uid) != requester_id]
        details = f"Refused knock from {requester_id}"
        if had_existing_permit:
            details += " and removed existing permit"
        details += " (added to banned list)"
        self.system.add_log(entry, interaction.user.id, "knock_refused", details)
        await self.system.sync_channel(channel, entry, reason=f"Knock refused by {interaction.user}")
        await self.system.upsert_entry(channel.id, entry)

        requester = interaction.guild.get_member(requester_id)
        if requester:
            try:
                await requester.send(
                    f"❌ Your knock request for **{channel.name}** in **{interaction.guild.name}** was refused."
                )
            except Exception:
                pass

        await self._delete_knock_message(interaction)
        self.stop()


class TempVCSystem:
    def __init__(self, bot):
        self.bot = bot
        self._config_lock = asyncio.Lock()
        self._state_write_lock = asyncio.Lock()
        self._message_db_lock = asyncio.Lock()
        self._member_creation_locks: Dict[str, asyncio.Lock] = {}
        self._channel_state_locks: Dict[str, asyncio.Lock] = {}
        self._overwrite_bulk_retry_state: Dict[int, Dict[str, Any]] = {}
        self._overwrite_target_retry_state: Dict[tuple[int, str, int], Dict[str, Any]] = {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_files()

    def _get_member_creation_lock(self, guild_id: int, member_id: int) -> asyncio.Lock:
        key = f"{guild_id}:{member_id}"
        lock = self._member_creation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._member_creation_locks[key] = lock
        return lock

    def _get_channel_state_lock(self, channel_id: int) -> asyncio.Lock:
        key = str(channel_id)
        lock = self._channel_state_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_state_locks[key] = lock
        return lock

    def _ensure_files(self):
        if not TEMP_VC_CONFIG_FILE.exists():
            self._write_json(TEMP_VC_CONFIG_FILE, {"guilds": {}})
        TEMP_VC_STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_state_file()

    def _message_table_name(self, channel_id: int) -> str:
        try:
            normalized_channel_id = int(channel_id)
        except (TypeError, ValueError):
            normalized_channel_id = 0
        if normalized_channel_id <= 0:
            raise ValueError("Invalid channel id for temp VC message table.")
        return f"temp_vc_{normalized_channel_id}"

    def _ensure_message_table(self, connection: sqlite3.Connection, table_name: str):
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS "{table_name}" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL UNIQUE,
                author_id INTEGER NOT NULL,
                author_is_bot INTEGER NOT NULL DEFAULT 0,
                message_type INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                mentions_everyone INTEGER NOT NULL DEFAULT 0,
                mention_user_ids TEXT,
                mention_role_ids TEXT,
                attachment_count INTEGER NOT NULL DEFAULT 0,
                attachment_urls TEXT,
                embed_count INTEGER NOT NULL DEFAULT 0,
                sticker_count INTEGER NOT NULL DEFAULT 0,
                referenced_message_id INTEGER
            )
            """
        )

    async def save_temp_vc_message(self, message: discord.Message):
        guild = message.guild
        if guild is None:
            return
        channel_id = int(getattr(message.channel, "id", 0) or 0)
        if channel_id <= 0:
            return
        table_name = self._message_table_name(channel_id)

        mention_user_ids = [int(member.id) for member in message.mentions if int(getattr(member, "id", 0) or 0) > 0]
        mention_role_ids = [int(role.id) for role in message.role_mentions if int(getattr(role, "id", 0) or 0) > 0]
        attachment_urls = [str(attachment.url) for attachment in list(message.attachments or []) if getattr(attachment, "url", None)]
        author = message.author
        created_at = getattr(message, "created_at", None) or datetime.now(timezone.utc)
        reference = getattr(message, "reference", None)
        referenced_message_id = int(reference.message_id) if reference and getattr(reference, "message_id", None) else None
        message_type = int(getattr(message, "type", discord.MessageType.default).value)

        async with self._message_db_lock:
            with sqlite3.connect(TEMP_VC_MESSAGES_DB_FILE) as connection:
                self._ensure_message_table(connection, table_name)
                connection.execute(
                    f"""
                    INSERT OR REPLACE INTO "{table_name}" (
                        message_id,
                        author_id,
                        author_is_bot,
                        message_type,
                        created_at,
                        mentions_everyone,
                        mention_user_ids,
                        mention_role_ids,
                        attachment_count,
                        attachment_urls,
                        embed_count,
                        sticker_count,
                        referenced_message_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(message.id),
                        int(author.id),
                        1 if bool(getattr(author, "bot", False)) else 0,
                        message_type,
                        created_at.astimezone(timezone.utc).isoformat(),
                        1 if bool(getattr(message, "mention_everyone", False)) else 0,
                        json.dumps(mention_user_ids, ensure_ascii=False),
                        json.dumps(mention_role_ids, ensure_ascii=False),
                        len(attachment_urls),
                        json.dumps(attachment_urls, ensure_ascii=False),
                        len(list(message.embeds or [])),
                        len(list(getattr(message, "stickers", []) or [])),
                        referenced_message_id,
                    ),
                )
                connection.commit()

    def _entry_file_path(self, channel_id: int) -> Optional[Path]:
        try:
            normalized_channel_id = int(channel_id)
        except (TypeError, ValueError):
            return None
        if normalized_channel_id <= 0:
            return None
        return TEMP_VC_STATE_DIR / f"{normalized_channel_id}.json"

    def _migrate_legacy_state_file(self):
        if not LEGACY_TEMP_VC_STATE_FILE.exists():
            return

        legacy_payload = self._read_json(LEGACY_TEMP_VC_STATE_FILE, {"channels": {}})
        channels = legacy_payload.get("channels", {})
        if isinstance(channels, dict):
            for channel_id_str, payload in channels.items():
                if not str(channel_id_str).isdigit() or not isinstance(payload, dict):
                    continue
                channel_id = int(channel_id_str)
                guild_id = int(payload.get("guild_id", 0) or 0)
                if guild_id <= 0:
                    continue
                entry_path = self._entry_file_path(channel_id)
                if entry_path is None or entry_path.exists():
                    continue
                normalized_entry = self._normalize_channel_entry(payload, guild_id)
                self._write_json(entry_path, normalized_entry)

        migrated_path = DATA_DIR / "temp_vc_channels.legacy.migrated.json"
        try:
            if migrated_path.exists():
                migrated_path.unlink()
            LEGACY_TEMP_VC_STATE_FILE.replace(migrated_path)
        except OSError:
            pass

    def _read_json(self, path: Path, fallback: Dict[str, Any]) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
                if isinstance(data, dict):
                    return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return fallback

    def _write_json(self, path: Path, payload: Dict[str, Any]):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(
            f".{path.name}.{os.getpid()}.{int(datetime.now(timezone.utc).timestamp() * 1_000_000)}.tmp"
        )
        try:
            with open(tmp_path, "w", encoding="utf-8") as file:
                json.dump(payload, file, indent=4, ensure_ascii=False)
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _default_guild_config(self) -> Dict[str, Any]:
        return {
            "generator_channel_id": None,
            "temp_category_id": None,
            "log_channel_id": None,
            "log_channel_enabled": False,
            "user_presets": {},
            "default_user_limit": 0,
            "default_bitrate": 64000,
            "default_template": "default",
            "templates": {},
        }

    def _normalize_templates(self, templates: Dict[str, Any]) -> Dict[str, Any]:
        normalized = {}
        if isinstance(templates, dict):
            for key, value in templates.items():
                if not isinstance(value, dict):
                    continue
                normalized_key = str(key or "").strip()
                if not normalized_key:
                    continue
                permitted_roles = [
                    int(x)
                    for x in value.get("permitted_roles", [])
                    if str(x).isdigit() and int(x) not in PRIVILEGED_ROLE_ID_SET
                ]
                try:
                    user_limit_raw = int(value.get("user_limit", 0))
                except (TypeError, ValueError):
                    user_limit_raw = 0
                try:
                    bitrate_raw = int(value.get("bitrate", 64000))
                except (TypeError, ValueError):
                    bitrate_raw = 64000
                normalized[normalized_key] = {
                    "display_name": str(value.get("display_name", normalized_key)),
                    "locked": bool(value.get("locked", False)),
                    "hidden": bool(value.get("hidden", False)),
                    "push_to_talk": bool(value.get("push_to_talk", False)),
                    "user_limit": clamp(user_limit_raw, 0, 99),
                    "bitrate": max(8000, bitrate_raw),
                    "region": str(value.get("region", "auto") or "auto"),
                    "name_format": str(value.get("name_format", "{owner}'s VC")),
                    "permitted_roles": list(dict.fromkeys(permitted_roles)),
                }
        return normalized

    def normalize_user_preset_name(self, raw_name: str) -> str:
        cleaned = " ".join(str(raw_name or "").strip().split())
        return cleaned[:MAX_USER_PRESET_NAME_LENGTH]

    def _find_case_insensitive_key(self, mapping: Dict[str, Any], raw_name: str) -> Optional[str]:
        needle = self.normalize_user_preset_name(raw_name).casefold()
        if not needle:
            return None
        for key in mapping.keys():
            if str(key).casefold() == needle:
                return key
        return None

    def _normalize_preset_settings(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        source = raw if isinstance(raw, dict) else {}
        try:
            user_limit_raw = int(source.get("user_limit", 0))
        except (TypeError, ValueError):
            user_limit_raw = 0
        try:
            bitrate_raw = int(source.get("bitrate", 64000))
        except (TypeError, ValueError):
            bitrate_raw = 64000
        normalized = {
            "locked": bool(source.get("locked", False)),
            "hidden": bool(source.get("hidden", False)),
            "push_to_talk": bool(source.get("push_to_talk", False)),
            "user_limit": clamp(user_limit_raw, 0, 99),
            "bitrate": max(8000, bitrate_raw),
            "region": str(source.get("region", "auto") or "auto"),
            "template": str(source.get("template", "default") or "default"),
            "permitted_users": [int(x) for x in source.get("permitted_users", []) if str(x).isdigit()],
            "permitted_roles": [
                int(x)
                for x in source.get("permitted_roles", [])
                if str(x).isdigit() and int(x) not in PRIVILEGED_ROLE_ID_SET
            ],
            "banned_users": [int(x) for x in source.get("banned_users", []) if str(x).isdigit()],
            "banned_roles": [
                int(x)
                for x in source.get("banned_roles", [])
                if str(x).isdigit() and int(x) not in PRIVILEGED_ROLE_ID_SET
            ],
        }
        normalized["permitted_users"] = list(dict.fromkeys(normalized["permitted_users"]))
        normalized["permitted_roles"] = list(dict.fromkeys(normalized["permitted_roles"]))
        normalized["banned_users"] = list(dict.fromkeys(normalized["banned_users"]))
        normalized["banned_roles"] = list(dict.fromkeys(normalized["banned_roles"]))

        banned_user_set = set(normalized["banned_users"])
        banned_role_set = set(normalized["banned_roles"])
        normalized["permitted_users"] = [uid for uid in normalized["permitted_users"] if uid not in banned_user_set]
        normalized["permitted_roles"] = [rid for rid in normalized["permitted_roles"] if rid not in banned_role_set]
        return normalized

    def _normalize_user_preset_bucket(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        bucket = {"presets": {}, "default_preset": None}
        if not isinstance(raw, dict):
            return bucket

        presets_raw = raw.get("presets", {})
        if isinstance(presets_raw, dict):
            for preset_name_raw, preset_payload in presets_raw.items():
                if len(bucket["presets"]) >= MAX_USER_PRESETS_PER_USER:
                    break
                preset_name = self.normalize_user_preset_name(str(preset_name_raw))
                if not preset_name:
                    continue
                existing_name = self._find_case_insensitive_key(bucket["presets"], preset_name)
                if existing_name and existing_name != preset_name:
                    bucket["presets"].pop(existing_name, None)
                bucket["presets"][preset_name] = self._normalize_preset_settings(preset_payload)

        default_name_raw = raw.get("default_preset")
        if isinstance(default_name_raw, str):
            matched_name = self._find_case_insensitive_key(bucket["presets"], default_name_raw)
            if matched_name:
                bucket["default_preset"] = matched_name

        return bucket

    def _normalize_user_presets(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        normalized = {}
        if not isinstance(raw, dict):
            return normalized
        for user_id_raw, preset_bucket in raw.items():
            if not str(user_id_raw).isdigit():
                continue
            normalized[str(int(user_id_raw))] = self._normalize_user_preset_bucket(preset_bucket)
        return normalized

    def _normalize_guild_config(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        base = self._default_guild_config()
        if not isinstance(raw, dict):
            return base
        base["generator_channel_id"] = int(raw["generator_channel_id"]) if raw.get("generator_channel_id") else None
        base["temp_category_id"] = int(raw["temp_category_id"]) if raw.get("temp_category_id") else None
        base["log_channel_id"] = int(raw["log_channel_id"]) if raw.get("log_channel_id") else None
        if "log_channel_enabled" in raw:
            base["log_channel_enabled"] = bool(raw.get("log_channel_enabled"))
        else:
            base["log_channel_enabled"] = bool(base["log_channel_id"])
        base["user_presets"] = self._normalize_user_presets(raw.get("user_presets", {}))
        base["default_user_limit"] = clamp(int(raw.get("default_user_limit", 0)), 0, 99)
        base["default_bitrate"] = max(8000, int(raw.get("default_bitrate", 64000)))
        base["default_template"] = str(raw.get("default_template", "default") or "default")
        base["templates"] = self._normalize_templates(raw.get("templates", {}))
        if base["default_template"] not in base["templates"] and base["templates"]:
            base["default_template"] = next(iter(base["templates"].keys()))
        elif base["default_template"] not in base["templates"]:
            base["default_template"] = "default"
        return base

    async def get_guild_config(self, guild_id: int) -> Dict[str, Any]:
        async with self._config_lock:
            payload = self._read_json(TEMP_VC_CONFIG_FILE, {"guilds": {}})
            guild_cfg = payload.get("guilds", {}).get(str(guild_id), {})
            normalized = self._normalize_guild_config(guild_cfg)
            if str(guild_id) not in payload.get("guilds", {}):
                payload.setdefault("guilds", {})[str(guild_id)] = normalized
                self._write_json(TEMP_VC_CONFIG_FILE, payload)
            return normalized

    async def update_guild_config(self, guild_id: int, updates: Dict[str, Any]) -> Dict[str, Any]:
        async with self._config_lock:
            payload = self._read_json(TEMP_VC_CONFIG_FILE, {"guilds": {}})
            guild_cfg = self._normalize_guild_config(payload.get("guilds", {}).get(str(guild_id), {}))
            guild_cfg.update(updates)
            guild_cfg = self._normalize_guild_config(guild_cfg)
            payload.setdefault("guilds", {})[str(guild_id)] = guild_cfg
            self._write_json(TEMP_VC_CONFIG_FILE, payload)
            return guild_cfg

    def build_user_preset_from_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        try:
            user_limit_raw = int(entry.get("user_limit", 0))
        except (TypeError, ValueError):
            user_limit_raw = 0
        try:
            bitrate_raw = int(entry.get("bitrate", 64000))
        except (TypeError, ValueError):
            bitrate_raw = 64000
        return self._normalize_preset_settings(
            {
                "locked": bool(entry.get("locked", False)),
                "hidden": bool(entry.get("hidden", False)),
                "push_to_talk": bool(entry.get("push_to_talk", False)),
                "user_limit": user_limit_raw,
                "bitrate": bitrate_raw,
                "region": str(entry.get("region", "auto") or "auto"),
                "template": str(entry.get("template", "default") or "default"),
                "permitted_users": list(entry.get("permitted_users", []) or []),
                "permitted_roles": list(entry.get("permitted_roles", []) or []),
                "banned_users": list(entry.get("banned_users", []) or []),
                "banned_roles": list(entry.get("banned_roles", []) or []),
            }
        )

    def apply_user_preset_to_entry(self, entry: Dict[str, Any], preset_settings: Dict[str, Any]):
        normalized_preset = self._normalize_preset_settings(preset_settings)
        existing_non_privileged_roles = [
            int(role_id)
            for role_id in entry.get("permitted_roles", [])
            if str(role_id).isdigit() and int(role_id) not in PRIVILEGED_ROLE_ID_SET
        ]
        entry["locked"] = normalized_preset["locked"]
        entry["hidden"] = normalized_preset["hidden"]
        entry["push_to_talk"] = normalized_preset["push_to_talk"]
        entry["user_limit"] = normalized_preset["user_limit"]
        entry["bitrate"] = normalized_preset["bitrate"]
        entry["region"] = normalized_preset["region"]
        entry["template"] = normalized_preset["template"]
        entry["permitted_users"] = list(normalized_preset["permitted_users"])
        entry["permitted_roles"] = list(
            dict.fromkeys(
                list(normalized_preset["permitted_roles"]) + existing_non_privileged_roles + list(PRIVILEGED_ROLE_IDS)
            )
        )
        entry["banned_users"] = list(normalized_preset["banned_users"])
        entry["banned_roles"] = list(normalized_preset["banned_roles"])

        guild_id = int(entry.get("guild_id", 0) or 0)
        if guild_id > 0:
            normalized_entry = self._normalize_channel_entry(entry, guild_id)
            entry.clear()
            entry.update(normalized_entry)

    async def get_user_preset_bucket(self, guild_id: int, user_id: int) -> Dict[str, Any]:
        guild_cfg = await self.get_guild_config(guild_id)
        user_key = str(int(user_id))
        bucket = guild_cfg.get("user_presets", {}).get(user_key, {})
        normalized_bucket = self._normalize_user_preset_bucket(bucket)
        return {
            "presets": dict(normalized_bucket.get("presets", {})),
            "default_preset": normalized_bucket.get("default_preset"),
        }

    async def save_user_preset(
        self,
        guild_id: int,
        user_id: int,
        preset_name: str,
        preset_settings: Dict[str, Any],
    ) -> Dict[str, Any]:
        clean_name = self.normalize_user_preset_name(preset_name)
        if not clean_name:
            raise ValueError("Preset name cannot be empty.")

        user_key = str(int(user_id))
        guild_cfg = await self.get_guild_config(guild_id)
        user_presets = dict(guild_cfg.get("user_presets", {}))
        bucket = self._normalize_user_preset_bucket(user_presets.get(user_key, {}))
        presets = dict(bucket.get("presets", {}))

        existing_name = self._find_case_insensitive_key(presets, clean_name)
        is_new = existing_name is None
        if is_new and len(presets) >= MAX_USER_PRESETS_PER_USER:
            raise ValueError(f"You can only keep up to {MAX_USER_PRESETS_PER_USER} presets.")
        if existing_name and existing_name != clean_name:
            presets.pop(existing_name, None)
        presets[clean_name] = self._normalize_preset_settings(preset_settings)
        bucket["presets"] = presets
        if bucket.get("default_preset") is None:
            bucket["default_preset"] = clean_name
        elif self._find_case_insensitive_key(presets, bucket.get("default_preset")) is None:
            bucket["default_preset"] = clean_name

        user_presets[user_key] = bucket
        guild_cfg = await self.update_guild_config(guild_id, {"user_presets": user_presets})
        saved_bucket = self._normalize_user_preset_bucket(guild_cfg.get("user_presets", {}).get(user_key, {}))
        return {
            "name": clean_name,
            "created": is_new,
            "count": len(saved_bucket.get("presets", {})),
            "default_preset": saved_bucket.get("default_preset"),
        }

    async def get_user_preset(
        self,
        guild_id: int,
        user_id: int,
        preset_name: str,
    ) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        bucket = await self.get_user_preset_bucket(guild_id, user_id)
        presets = bucket.get("presets", {})
        matched_name = self._find_case_insensitive_key(presets, preset_name)
        if not matched_name:
            return None, None
        return matched_name, self._normalize_preset_settings(presets.get(matched_name, {}))

    async def delete_user_preset(
        self,
        guild_id: int,
        user_id: int,
        preset_name: str,
    ) -> Optional[str]:
        user_key = str(int(user_id))
        guild_cfg = await self.get_guild_config(guild_id)
        user_presets = dict(guild_cfg.get("user_presets", {}))
        bucket = self._normalize_user_preset_bucket(user_presets.get(user_key, {}))
        presets = dict(bucket.get("presets", {}))
        matched_name = self._find_case_insensitive_key(presets, preset_name)
        if not matched_name:
            return None

        presets.pop(matched_name, None)
        default_name = bucket.get("default_preset")
        if default_name and default_name.casefold() == matched_name.casefold():
            bucket["default_preset"] = None
        bucket["presets"] = presets
        user_presets[user_key] = bucket
        await self.update_guild_config(guild_id, {"user_presets": user_presets})
        return matched_name

    async def set_user_default_preset(
        self,
        guild_id: int,
        user_id: int,
        preset_name: Optional[str],
    ) -> Optional[str]:
        user_key = str(int(user_id))
        guild_cfg = await self.get_guild_config(guild_id)
        user_presets = dict(guild_cfg.get("user_presets", {}))
        bucket = self._normalize_user_preset_bucket(user_presets.get(user_key, {}))
        presets = dict(bucket.get("presets", {}))

        if preset_name is None:
            bucket["default_preset"] = None
            bucket["presets"] = presets
            user_presets[user_key] = bucket
            await self.update_guild_config(guild_id, {"user_presets": user_presets})
            return None

        matched_name = self._find_case_insensitive_key(presets, preset_name)
        if not matched_name:
            raise ValueError("Preset not found.")

        bucket["default_preset"] = matched_name
        bucket["presets"] = presets
        user_presets[user_key] = bucket
        await self.update_guild_config(guild_id, {"user_presets": user_presets})
        return matched_name

    async def get_user_default_preset(
        self,
        guild_id: int,
        user_id: int,
    ) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        bucket = await self.get_user_preset_bucket(guild_id, user_id)
        default_name = bucket.get("default_preset")
        if not default_name:
            return None, None
        presets = bucket.get("presets", {})
        matched_name = self._find_case_insensitive_key(presets, default_name)
        if not matched_name:
            return None, None
        return matched_name, self._normalize_preset_settings(presets.get(matched_name, {}))

    def _default_channel_entry(self, guild_id: int) -> Dict[str, Any]:
        return {
            "guild_id": guild_id,
            "owner_id": None,
            "created_at": now_iso(),
            "locked": False,
            "hidden": False,
            "push_to_talk": False,
            "user_limit": 0,
            "bitrate": 64000,
            "region": "auto",
            "template": "default",
            "permitted_users": [],
            "blocked_knock_users": [],
            "banned_users": [],
            "banned_roles": [],
            "permitted_roles": list(PRIVILEGED_ROLE_IDS),
            "member_join_times": {},
            "logs": [],
        }

    def _normalize_channel_entry(self, raw: Dict[str, Any], guild_id: int) -> Dict[str, Any]:
        base = self._default_channel_entry(guild_id)
        if isinstance(raw, dict):
            base["owner_id"] = int(raw["owner_id"]) if raw.get("owner_id") else None
            base["created_at"] = str(raw.get("created_at") or base["created_at"])
            base["locked"] = bool(raw.get("locked", False))
            base["hidden"] = bool(raw.get("hidden", False))
            base["push_to_talk"] = bool(raw.get("push_to_talk", False))
            base["user_limit"] = clamp(int(raw.get("user_limit", 0)), 0, 99)
            base["bitrate"] = max(8000, int(raw.get("bitrate", 64000)))
            base["region"] = str(raw.get("region", "auto") or "auto")
            base["template"] = str(raw.get("template", "default"))
            base["permitted_users"] = [int(x) for x in raw.get("permitted_users", []) if str(x).isdigit()]
            base["blocked_knock_users"] = [int(x) for x in raw.get("blocked_knock_users", []) if str(x).isdigit()]
            base["banned_users"] = [int(x) for x in raw.get("banned_users", []) if str(x).isdigit()]
            base["banned_roles"] = [int(x) for x in raw.get("banned_roles", []) if str(x).isdigit()]
            base["permitted_roles"] = [int(x) for x in raw.get("permitted_roles", []) if str(x).isdigit()]
            base["member_join_times"] = {
                str(uid): str(ts)
                for uid, ts in (raw.get("member_join_times", {}) or {}).items()
                if str(uid).isdigit()
            }
            base["logs"] = list(raw.get("logs", []) or [])
        if base["blocked_knock_users"]:
            base["banned_users"].extend(base["blocked_knock_users"])
        blocked_user_ids = {uid for uid in [int(base.get("owner_id") or 0), int(OWNER_ID or 0)] if uid > 0}
        base["banned_users"] = [uid for uid in base["banned_users"] if uid not in blocked_user_ids]
        base["banned_roles"] = [rid for rid in base["banned_roles"] if rid not in PRIVILEGED_ROLE_ID_SET]
        banned_user_set = set(base["banned_users"])
        banned_role_set = set(base["banned_roles"])
        base["permitted_users"] = [uid for uid in base["permitted_users"] if uid not in banned_user_set]
        base["permitted_roles"] = [
            rid for rid in base["permitted_roles"]
            if rid in PRIVILEGED_ROLE_ID_SET or rid not in banned_role_set
        ]
        for role_id in PRIVILEGED_ROLE_IDS:
            if role_id not in base["permitted_roles"]:
                base["permitted_roles"].append(role_id)
        base["permitted_users"] = list(dict.fromkeys(base["permitted_users"]))
        base["blocked_knock_users"] = list(dict.fromkeys(base["blocked_knock_users"]))
        base["banned_users"] = list(dict.fromkeys(base["banned_users"]))
        base["banned_roles"] = list(dict.fromkeys(base["banned_roles"]))
        base["permitted_roles"] = list(dict.fromkeys(base["permitted_roles"]))
        base["logs"] = base["logs"][-MAX_STORED_LOG_ENTRIES:]
        return base


    async def get_entry(self, channel_id: int) -> Optional[Dict[str, Any]]:
        entry_path = self._entry_file_path(channel_id)
        if entry_path is None:
            return None
        raw = self._read_json(entry_path, {})
        if not raw or "guild_id" not in raw:
            return None
        guild_id = int(raw.get("guild_id", 0) or 0)
        if guild_id <= 0:
            return None
        return self._normalize_channel_entry(raw, guild_id)

    async def upsert_entry(self, channel_id: int, entry: Dict[str, Any]):
        entry_path = self._entry_file_path(channel_id)
        if entry_path is None:
            return
        guild_id = int(entry.get("guild_id", 0))
        if guild_id <= 0:
            return
        normalized = self._normalize_channel_entry(entry, guild_id)
        channel_lock = self._get_channel_state_lock(int(channel_id))
        async with self._state_write_lock:
            async with channel_lock:
                existing_payload = self._read_json(entry_path, {})
                if isinstance(existing_payload, dict):
                    existing_guild_id = int(existing_payload.get("guild_id", 0) or 0)
                    if existing_guild_id == guild_id:
                        existing_entry = self._normalize_channel_entry(existing_payload, guild_id)
                        normalized["logs"] = self._merge_logs(
                            existing_entry.get("logs", []),
                            normalized.get("logs", []),
                        )
                self._write_json(entry_path, normalized)

    async def remove_entry(self, channel_id: int):
        entry_path = self._entry_file_path(channel_id)
        if entry_path is None:
            return
        channel_lock = self._get_channel_state_lock(int(channel_id))
        async with self._state_write_lock:
            async with channel_lock:
                try:
                    entry_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
        self._clear_channel_overwrite_retry_state(int(channel_id))

    async def list_guild_entries(self, guild_id: int) -> Dict[int, Dict[str, Any]]:
        parsed = {}
        if not TEMP_VC_STATE_DIR.exists():
            return parsed

        for entry_path in TEMP_VC_STATE_DIR.glob("*.json"):
            if not entry_path.is_file():
                continue
            channel_id_str = entry_path.stem
            if not channel_id_str.isdigit():
                continue
            payload = self._read_json(entry_path, {})
            if not isinstance(payload, dict):
                continue
            if int(payload.get("guild_id", 0)) != guild_id:
                continue
            parsed[int(channel_id_str)] = self._normalize_channel_entry(payload, guild_id)
        return parsed

    def is_admin_member(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        if OWNER_ID and member.id == OWNER_ID:
            return True
        return any(role.id in PRIVILEGED_ROLE_ID_SET for role in member.roles)

    def can_manage(self, member: discord.Member, entry: Dict[str, Any]) -> bool:
        return self.is_admin_member(member) or member.id == int(entry.get("owner_id") or 0)

    def add_log(self, entry: Dict[str, Any], actor_id: Optional[int], action: str, details: str = ""):
        entry.setdefault("logs", [])
        entry["logs"].append(
            {
                "timestamp": now_iso(),
                "actor_id": int(actor_id) if actor_id else None,
                "action": action,
                "details": details,
            }
        )
        entry["logs"] = entry["logs"][-MAX_STORED_LOG_ENTRIES:]

    def _merge_logs(self, existing_logs: List[Dict[str, Any]], incoming_logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        seen = set()

        for item in list(existing_logs or []) + list(incoming_logs or []):
            if not isinstance(item, dict):
                continue
            timestamp = str(item.get("timestamp") or now_iso())
            actor_raw = item.get("actor_id")
            actor_id = int(actor_raw) if actor_raw is not None and str(actor_raw).isdigit() else None
            action = str(item.get("action") or "unknown_action")
            details = str(item.get("details") or "")
            key = (timestamp, actor_id, action, details)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "timestamp": timestamp,
                    "actor_id": actor_id,
                    "action": action,
                    "details": details,
                }
            )

        merged.sort(key=lambda log: parse_iso(str(log.get("timestamp") or now_iso())))
        return merged[-MAX_STORED_LOG_ENTRIES:]

    def _get_bot_member(self, guild: discord.Guild) -> Optional[discord.Member]:
        bot_member = guild.me
        if bot_member is not None:
            return bot_member
        bot_user = getattr(self.bot, "user", None)
        bot_id = int(getattr(bot_user, "id", 0) or 0)
        if bot_id <= 0:
            return None
        return guild.get_member(bot_id)

    def _get_bot_overwrite_target(self, guild: discord.Guild) -> Optional[Any]:
        bot_member = self._get_bot_member(guild)
        if bot_member is not None:
            return bot_member
        bot_user = getattr(self.bot, "user", None)
        bot_id = int(getattr(bot_user, "id", 0) or 0)
        if bot_id <= 0:
            return None
        return discord.Object(id=bot_id)

    def _missing_permission_names(self, permissions: discord.Permissions, required_names: List[str]) -> List[str]:
        return [name for name in required_names if not bool(getattr(permissions, name, False))]

    def _format_permission_names(self, names: List[str]) -> str:
        return ", ".join(name.replace("_", " ").title() for name in names)

    def _format_bot_channel_permission_snapshot(self, channel: discord.VoiceChannel) -> str:
        bot_member = self._get_bot_member(channel.guild)
        if bot_member is None:
            return "effective -> Bot member unresolved in guild cache"
        permissions = channel.permissions_for(bot_member)
        return (
            "effective -> "
            f"Manage Channels: {permissions.manage_channels}, "
            f"Manage Roles: {permissions.manage_roles}, "
            f"Move Members: {permissions.move_members}, "
            f"Administrator: {permissions.administrator}"
        )

    def _can_set_role_overwrite(self, guild: discord.Guild, role: discord.Role) -> bool:
        bot_member = self._get_bot_member(guild)
        if bot_member is None:
            return False
        if bot_member.guild_permissions.administrator:
            return True
        if role == guild.default_role:
            return True
        return bot_member.top_role > role

    def _can_set_member_overwrite(self, guild: discord.Guild, member: discord.Member) -> bool:
        bot_member = self._get_bot_member(guild)
        if bot_member is None:
            return False
        if bot_member.guild_permissions.administrator:
            return True
        if member.id == bot_member.id:
            return True
        return bot_member.top_role > member.top_role

    def _partition_manageable_roles_for_overwrites(
        self,
        guild: discord.Guild,
        role_ids: List[int],
    ) -> tuple[List[int], List[str]]:
        manageable_ids: List[int] = []
        skipped_labels: List[str] = []
        seen = set()

        for raw_role_id in role_ids or []:
            if not str(raw_role_id).isdigit():
                continue
            role_id = int(raw_role_id)
            if role_id in seen:
                continue
            seen.add(role_id)
            role = guild.get_role(role_id)
            if role is None:
                continue
            if self._can_set_role_overwrite(guild, role):
                manageable_ids.append(role_id)
            else:
                skipped_labels.append(role.mention if getattr(role, "mention", None) else f"<@&{role_id}>")

        return manageable_ids, skipped_labels

    def _partition_manageable_members_for_overwrites(
        self,
        guild: discord.Guild,
        user_ids: List[int],
    ) -> tuple[List[int], List[str]]:
        manageable_ids: List[int] = []
        skipped_labels: List[str] = []
        seen = set()

        for raw_user_id in user_ids or []:
            if not str(raw_user_id).isdigit():
                continue
            user_id = int(raw_user_id)
            if user_id in seen:
                continue
            seen.add(user_id)
            member = guild.get_member(user_id)
            if member is None:
                continue
            if self._can_set_member_overwrite(guild, member):
                manageable_ids.append(user_id)
            else:
                skipped_labels.append(member.mention if getattr(member, "mention", None) else f"<@{user_id}>")

        return manageable_ids, skipped_labels

    def _ensure_bot_temp_vc_permissions(
        self,
        guild: discord.Guild,
        generator_channel: discord.VoiceChannel,
        category: Optional[discord.CategoryChannel],
    ):
        bot_member = self._get_bot_member(guild)
        if bot_member is None:
            raise PermissionError("Bot member could not be resolved in this guild.")

        missing_scopes: List[str] = []

        if isinstance(category, discord.CategoryChannel):
            target_permissions = category.permissions_for(bot_member)
            target_scope = f"category '{category.name}'"
        else:
            target_permissions = bot_member.guild_permissions
            target_scope = "guild-level permissions"

        missing_target = self._missing_permission_names(
            target_permissions,
            ["manage_channels"],
        )
        if missing_target:
            missing_scopes.append(f"{target_scope}: {self._format_permission_names(missing_target)}")

        generator_permissions = generator_channel.permissions_for(bot_member)
        missing_generator = self._missing_permission_names(
            generator_permissions,
            ["view_channel", "connect", "move_members"],
        )
        if missing_generator:
            missing_scopes.append(
                (
                    f"generator channel '{generator_channel.name}': "
                    f"{self._format_permission_names(missing_generator)} "
                    f"(effective -> View Channel: {generator_permissions.view_channel}, "
                    f"Connect: {generator_permissions.connect}, "
                    f"Move Members: {generator_permissions.move_members}, "
                    f"Administrator: {generator_permissions.administrator})"
                )
            )

        if missing_scopes:
            raise PermissionError(
                "Missing required permissions for temp VC automation -> " + " | ".join(missing_scopes)
            )

    def _build_everyone_overwrite(self, entry: Dict[str, Any]) -> discord.PermissionOverwrite:
        everyone_view = not entry.get("hidden", False)
        everyone_connect = not entry.get("locked", False) and not entry.get("hidden", False)
        return discord.PermissionOverwrite(
            view_channel=everyone_view,
            connect=everyone_connect,
            speak=True,
            use_voice_activation=False if entry.get("push_to_talk") else None,
        )

    def _owner_overwrite(self, entry: Dict[str, Any]) -> discord.PermissionOverwrite:
        return discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_embedded_activities=True,
            move_members=True,
            mute_members=True,
            deafen_members=True,
            priority_speaker=True,
            manage_channels=True,
            manage_permissions=True,
            use_voice_activation=False if entry.get("push_to_talk") else None,
        )

    def _permit_overwrite(self, entry: Dict[str, Any]) -> discord.PermissionOverwrite:
        return discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_embedded_activities=True,
            use_voice_activation=False if entry.get("push_to_talk") else None,
        )

    def _ban_overwrite(self) -> discord.PermissionOverwrite:
        return discord.PermissionOverwrite(
            view_channel=False,
            connect=False,
            speak=False,
        )

    def _overwrite_target_key(self, target: Any) -> Optional[tuple[str, int]]:
        target_id = int(getattr(target, "id", 0) or 0)
        if target_id <= 0:
            return None
        if isinstance(target, discord.Role):
            return ("role", target_id)
        return ("member", target_id)

    def _overwrite_target_label(self, target: Any) -> str:
        if isinstance(target, discord.Role):
            return target.mention if getattr(target, "mention", None) else f"<@&{target.id}>"
        target_id = int(getattr(target, "id", 0) or 0)
        if target_id > 0:
            return f"<@{target_id}>"
        return str(target)

    def _overwrite_pair_signature(self, overwrite: discord.PermissionOverwrite) -> tuple[int, int]:
        allow, deny = overwrite.pair()
        return (
            int(getattr(allow, "value", 0)),
            int(getattr(deny, "value", 0)),
        )

    def _build_overwrite_signature(self, overwrites: Dict[Any, discord.PermissionOverwrite]) -> tuple:
        signature_items: List[tuple[str, int, int, int]] = []
        for target, overwrite in overwrites.items():
            key = self._overwrite_target_key(target)
            if key is None:
                continue
            allow_value, deny_value = self._overwrite_pair_signature(overwrite)
            signature_items.append((key[0], key[1], allow_value, deny_value))
        signature_items.sort(key=lambda item: (item[0], item[1]))
        return tuple(signature_items)

    def _target_retry_key(self, channel_id: int, target_key: tuple[str, int]) -> tuple[int, str, int]:
        return (int(channel_id), target_key[0], int(target_key[1]))

    def _mark_bulk_overwrite_retry(self, channel_id: int, signature: tuple, now_ts: float):
        self._overwrite_bulk_retry_state[int(channel_id)] = {
            "signature": signature,
            "retry_after": float(now_ts) + OVERWRITE_BULK_RETRY_COOLDOWN_SECONDS,
            "last_notice_at": 0.0,
        }

    def _clear_bulk_overwrite_retry(self, channel_id: int, signature: Optional[tuple] = None):
        channel_key = int(channel_id)
        state = self._overwrite_bulk_retry_state.get(channel_key)
        if state is None:
            return
        if signature is not None and state.get("signature") != signature:
            return
        self._overwrite_bulk_retry_state.pop(channel_key, None)

    def _is_bulk_overwrite_retry_suppressed(self, channel_id: int, signature: tuple, now_ts: float) -> bool:
        state = self._overwrite_bulk_retry_state.get(int(channel_id))
        if state is None:
            return False
        if state.get("signature") != signature:
            return False
        retry_after = float(state.get("retry_after", 0.0) or 0.0)
        return float(now_ts) < retry_after

    def _consume_bulk_overwrite_retry_notice(self, channel_id: int, now_ts: float) -> bool:
        state = self._overwrite_bulk_retry_state.get(int(channel_id))
        if state is None:
            return False
        last_notice = float(state.get("last_notice_at", 0.0) or 0.0)
        if float(now_ts) - last_notice < OVERWRITE_BULK_RETRY_NOTICE_COOLDOWN_SECONDS:
            return False
        state["last_notice_at"] = float(now_ts)
        return True

    def _mark_target_overwrite_retry(
        self,
        channel_id: int,
        target_key: tuple[str, int],
        overwrite_signature: tuple[int, int],
        now_ts: float,
    ):
        self._overwrite_target_retry_state[self._target_retry_key(channel_id, target_key)] = {
            "signature": overwrite_signature,
            "retry_after": float(now_ts) + OVERWRITE_TARGET_RETRY_COOLDOWN_SECONDS,
        }

    def _clear_target_overwrite_retry(self, channel_id: int, target_key: tuple[str, int]):
        self._overwrite_target_retry_state.pop(self._target_retry_key(channel_id, target_key), None)

    def _is_target_overwrite_retry_suppressed(
        self,
        channel_id: int,
        target_key: tuple[str, int],
        overwrite_signature: tuple[int, int],
        now_ts: float,
    ) -> bool:
        state = self._overwrite_target_retry_state.get(self._target_retry_key(channel_id, target_key))
        if state is None:
            return False
        if state.get("signature") != overwrite_signature:
            return False
        retry_after = float(state.get("retry_after", 0.0) or 0.0)
        return float(now_ts) < retry_after

    def _prune_target_overwrite_retry_state(self, channel_id: int, desired_target_keys: set[tuple[str, int]]):
        channel_key = int(channel_id)
        stale_keys = [
            cache_key
            for cache_key in list(self._overwrite_target_retry_state.keys())
            if cache_key[0] == channel_key and (cache_key[1], cache_key[2]) not in desired_target_keys
        ]
        for cache_key in stale_keys:
            self._overwrite_target_retry_state.pop(cache_key, None)

    def _clear_channel_overwrite_retry_state(self, channel_id: int):
        channel_key = int(channel_id)
        self._overwrite_bulk_retry_state.pop(channel_key, None)
        stale_keys = [
            cache_key
            for cache_key in list(self._overwrite_target_retry_state.keys())
            if cache_key[0] == channel_key
        ]
        for cache_key in stale_keys:
            self._overwrite_target_retry_state.pop(cache_key, None)

    def _sanitize_overwrite_for_channel(
        self,
        channel: discord.VoiceChannel,
        overwrite: discord.PermissionOverwrite,
    ) -> Optional[discord.PermissionOverwrite]:
        bot_member = self._get_bot_member(channel.guild)
        if bot_member is None:
            return overwrite
        bot_permissions = channel.permissions_for(bot_member)
        if bot_permissions.administrator:
            return overwrite

        sanitized = discord.PermissionOverwrite()
        for permission_name, value in overwrite:
            if value is None:
                continue
            if bool(getattr(bot_permissions, permission_name, False)):
                setattr(sanitized, permission_name, value)

        if not any(value is not None for _, value in sanitized):
            return None
        return sanitized

    async def _sync_overwrites_best_effort(
        self,
        channel: discord.VoiceChannel,
        desired_overwrites: Dict[Any, discord.PermissionOverwrite],
        entry: Dict[str, Any],
        reason: str,
    ):
        current_overwrites = dict(channel.overwrites)
        current_by_key: Dict[tuple[str, int], tuple[Any, discord.PermissionOverwrite]] = {}
        for target, overwrite in current_overwrites.items():
            key = self._overwrite_target_key(target)
            if key is not None:
                current_by_key[key] = (target, overwrite)

        desired_by_key: Dict[tuple[str, int], tuple[Any, discord.PermissionOverwrite]] = {}
        for target, overwrite in desired_overwrites.items():
            key = self._overwrite_target_key(target)
            if key is None:
                continue
            desired_by_key[key] = (target, overwrite)
        self._prune_target_overwrite_retry_state(channel.id, set(desired_by_key.keys()))

        changed_count = 0
        failed_targets: List[str] = []
        now_ts = monotonic()

        for key, (target, overwrite) in desired_by_key.items():
            current = current_by_key.get(key)
            if current and current[1].pair() == overwrite.pair():
                self._clear_target_overwrite_retry(channel.id, key)
                continue
            overwrite_signature = self._overwrite_pair_signature(overwrite)
            if self._is_target_overwrite_retry_suppressed(channel.id, key, overwrite_signature, now_ts):
                continue
            try:
                await channel.set_permissions(target, overwrite=overwrite, reason=reason)
                changed_count += 1
                self._clear_target_overwrite_retry(channel.id, key)
            except discord.Forbidden:
                self._mark_target_overwrite_retry(channel.id, key, overwrite_signature, monotonic())
                failed_targets.append(self._overwrite_target_label(target))
            except discord.HTTPException as exc:
                self._mark_target_overwrite_retry(channel.id, key, overwrite_signature, monotonic())
                status = getattr(exc, "status", "unknown")
                code = getattr(exc, "code", "unknown")
                failed_targets.append(
                    f"{self._overwrite_target_label(target)} ({status}/{code})"
                )

        if failed_targets:
            failed_text = ", ".join(failed_targets[:8])
            if len(failed_targets) > 8:
                failed_text = f"{failed_text}, +{len(failed_targets) - 8} more"
            self.add_log(
                entry,
                None,
                "overwrite_targets_skipped",
                f"Some overwrite targets could not be updated: {failed_text}",
            )
            print(
                f"[VC_GENERATOR] Some overwrite targets could not be updated for channel {channel.id}: {failed_text}"
            )
        elif changed_count > 0:
            print(
                f"[VC_GENERATOR] Applied partial/best-effort overwrite sync for channel {channel.id}; "
                f"changed {changed_count} targets."
            )
    def _build_channel_overwrites(self, guild: discord.Guild, entry: Dict[str, Any]) -> Dict[Any, discord.PermissionOverwrite]:
        overwrites: Dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: self._build_everyone_overwrite(entry)
        }

        owner_id = int(entry.get("owner_id") or 0)
        if owner_id:
            owner_member = guild.get_member(owner_id)
            if owner_member and self._can_set_member_overwrite(guild, owner_member):
                overwrites[owner_member] = self._owner_overwrite(entry)

        permit_overwrite = self._permit_overwrite(entry)
        ban_overwrite = self._ban_overwrite()
        permitted_role_ids, _ = self._partition_manageable_roles_for_overwrites(
            guild,
            entry.get("permitted_roles", []),
        )
        for role_id in permitted_role_ids:
            role = guild.get_role(int(role_id))
            if role:
                if role.id in PRIVILEGED_ROLE_ID_SET:
                    overwrites[role] = self._owner_overwrite(entry)
                else:
                    overwrites[role] = permit_overwrite

        permitted_user_ids, _ = self._partition_manageable_members_for_overwrites(
            guild,
            entry.get("permitted_users", []),
        )
        for user_id in permitted_user_ids:
            member = guild.get_member(int(user_id))
            if member:
                overwrites[member] = permit_overwrite
        banned_role_ids, _ = self._partition_manageable_roles_for_overwrites(
            guild,
            entry.get("banned_roles", []),
        )
        for role_id in banned_role_ids:
            role = guild.get_role(int(role_id))
            if role:
                overwrites[role] = ban_overwrite

        banned_user_ids, _ = self._partition_manageable_members_for_overwrites(
            guild,
            entry.get("banned_users", []),
        )
        for user_id in banned_user_ids:
            member = guild.get_member(int(user_id))
            if member:
                overwrites[member] = ban_overwrite
        bot_overwrite_target = self._get_bot_overwrite_target(guild)
        if bot_overwrite_target is not None:
            overwrites[bot_overwrite_target] = self._owner_overwrite(entry)

        return overwrites

    async def sync_channel(self, channel: discord.VoiceChannel, entry: Dict[str, Any], reason: Optional[str] = None):
        entry.setdefault("permitted_roles", [])
        for role_id in PRIVILEGED_ROLE_IDS:
            if role_id not in entry["permitted_roles"]:
                entry["permitted_roles"].append(role_id)
        max_bitrate = int(channel.guild.bitrate_limit)
        bitrate = clamp(int(entry.get("bitrate", 64000)), 8000, max_bitrate)
        user_limit = clamp(int(entry.get("user_limit", 0)), 0, 99)
        region = str(entry.get("region", "auto") or "auto")
        rtc_region = None if region == "auto" else region
        overwrites = self._build_channel_overwrites(channel.guild, entry)
        overwrite_signature = self._build_overwrite_signature(overwrites)
        edit_reason = reason or "Temporary VC state update"
        current_region = str(getattr(channel, "rtc_region", None) or "auto")
        settings_changed = (
            int(getattr(channel, "user_limit", 0) or 0) != user_limit
            or int(getattr(channel, "bitrate", 0) or 0) != bitrate
            or current_region != region
        )

        now_ts = monotonic()
        if self._is_bulk_overwrite_retry_suppressed(channel.id, overwrite_signature, now_ts):
            if self._consume_bulk_overwrite_retry_notice(channel.id, now_ts):
                retry_after = float(
                    self._overwrite_bulk_retry_state.get(channel.id, {}).get("retry_after", now_ts) or now_ts
                )
                seconds_until_retry = max(1, int(retry_after - now_ts))
                print(
                    f"[VC_GENERATOR] Skipping full overwrite sync for channel {channel.id}; "
                    f"retrying bulk overwrite update in ~{seconds_until_retry}s."
                )
            if settings_changed:
                await channel.edit(
                    user_limit=user_limit,
                    bitrate=bitrate,
                    rtc_region=rtc_region,
                    reason=edit_reason,
                )
            await self._sync_overwrites_best_effort(channel, overwrites, entry, edit_reason)
        else:
            try:
                await channel.edit(
                    user_limit=user_limit,
                    bitrate=bitrate,
                    rtc_region=rtc_region,
                    overwrites=overwrites,
                    reason=edit_reason,
                )
                self._clear_bulk_overwrite_retry(channel.id, overwrite_signature)
            except discord.Forbidden:
                self._mark_bulk_overwrite_retry(channel.id, overwrite_signature, monotonic())
                permission_snapshot = self._format_bot_channel_permission_snapshot(channel)
                self.add_log(
                    entry,
                    None,
                    "overwrite_sync_skipped",
                    (
                        "Could not apply full overwrite sync; updated channel settings without overwrite changes. "
                        f"{permission_snapshot}"
                    ),
                )
                print(
                    f"[VC_GENERATOR] Overwrite sync denied for channel {channel.id}; "
                    f"retrying update without overwrites ({permission_snapshot})"
                )
                await channel.edit(
                    user_limit=user_limit,
                    bitrate=bitrate,
                    rtc_region=rtc_region,
                    reason=edit_reason,
                )
                await self._sync_overwrites_best_effort(channel, overwrites, entry, edit_reason)
            else:
                self._prune_target_overwrite_retry_state(channel.id, set())

        entry["bitrate"] = bitrate
        entry["user_limit"] = user_limit
        entry["region"] = region

    def _resolve_template(self, config: Dict[str, Any], template_name: str) -> Dict[str, Any]:
        templates = config.get("templates", {})
        chosen = templates.get(template_name) or templates.get(config.get("default_template"))
        if not chosen:
            chosen = FALLBACK_TEMPLATE
        return chosen

    def apply_template_to_entry(self, entry: Dict[str, Any], template_name: str, config: Dict[str, Any]):
        template = self._resolve_template(config, template_name)
        entry["template"] = template_name if template_name in config.get("templates", {}) else config.get("default_template", "default")
        entry["locked"] = bool(template.get("locked", False))
        entry["hidden"] = bool(template.get("hidden", False))
        entry["push_to_talk"] = bool(template.get("push_to_talk", False))
        entry["user_limit"] = clamp(int(template.get("user_limit", entry.get("user_limit", 0))), 0, 99)
        entry["bitrate"] = max(8000, int(template.get("bitrate", entry.get("bitrate", 64000))))
        entry["region"] = str(template.get("region", "auto") or "auto")
        template_permitted_roles = [
            int(x)
            for x in template.get("permitted_roles", [])
            if str(x).isdigit() and int(x) not in PRIVILEGED_ROLE_ID_SET
        ]
        entry["permitted_roles"] = list(dict.fromkeys(template_permitted_roles + list(PRIVILEGED_ROLE_IDS)))

    def _compute_channel_name(self, owner: discord.Member, template: Dict[str, Any]) -> str:
        template_format = str(template.get("name_format", "{owner}'s VC"))
        try:
            formatted = template_format.format(owner=owner.display_name)
        except Exception:
            formatted = f"{owner.display_name}'s VC"
        return clean_channel_name(formatted)
    async def _send_creation_setup_embed(
        self,
        channel: discord.VoiceChannel,
        owner: discord.Member,
        default_preset_name: Optional[str] = None,
    ):
        embed = discord.Embed(
            title="Temporary VC Setup",
            description=(
                f"{owner.mention}, your temporary VC is ready.\n"
                "Use the controls below to quickly configure it."
            ),
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Quick Start",
            value=(
                "• Run `/vc_manage` to open the panel\n"
                "• Use Lock/Hide/Limit/Rename/Ban/Permit buttons\n"
                "• Use **Save Preset** / **Load Preset** for reusable setups"
            ),
            inline=False,
        )
        embed.add_field(
            name="Preset Management",
            value=(
                "Use `/vc_presets` to list, delete, or set your default preset.\n"
                "Default presets are auto-applied on future temp VC creations."
            ),
            inline=False,
        )
        if isinstance(default_preset_name, str) and default_preset_name.strip():
            embed.add_field(
                name="Default Preset Applied",
                value=f"✅ **{default_preset_name}** was auto-applied to this VC.",
                inline=False,
            )
        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            pass

    async def _emit_log_channel_message(self, guild: discord.Guild, content: Optional[str] = None, embed: Optional[discord.Embed] = None):
        config = await self.get_guild_config(guild.id)
        log_channel_id = config.get("log_channel_id")
        log_channel_enabled = bool(config.get("log_channel_enabled", bool(log_channel_id)))
        if not log_channel_id or not log_channel_enabled:
            return
        channel = guild.get_channel(int(log_channel_id))
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(content=content, embed=embed)
            except Exception:
                pass

    async def _find_owned_temp_channel(self, guild: discord.Guild, owner_id: int) -> Optional[discord.VoiceChannel]:
        entries = await self.list_guild_entries(guild.id)
        stale_channel_ids = []
        for channel_id, entry in entries.items():
            if int(entry.get("owner_id") or 0) != owner_id:
                continue
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.VoiceChannel):
                return channel
            stale_channel_ids.append(channel_id)
        for channel_id in stale_channel_ids:
            await self.remove_entry(channel_id)
        return None

    async def create_temp_channel_for_member(self, member: discord.Member, generator_channel: discord.VoiceChannel):
        creation_lock = self._get_member_creation_lock(member.guild.id, member.id)
        async with creation_lock:
            existing_channel = await self._find_owned_temp_channel(member.guild, member.id)
            if isinstance(existing_channel, discord.VoiceChannel):
                if not member.voice or not member.voice.channel or member.voice.channel.id != existing_channel.id:
                    try:
                        await member.move_to(existing_channel, reason="Moved to existing temporary VC")
                    except Exception:
                        pass
                return

            config = await self.get_guild_config(member.guild.id)
            category = member.guild.get_channel(config.get("temp_category_id")) if config.get("temp_category_id") else None
            if not isinstance(category, discord.CategoryChannel):
                category = generator_channel.category

            self._ensure_bot_temp_vc_permissions(member.guild, generator_channel, category)

            template_name = config.get("default_template", "default")
            template = self._resolve_template(config, template_name)
            channel_name = self._compute_channel_name(member, template)

            base_entry = self._default_channel_entry(member.guild.id)
            base_entry["owner_id"] = member.id
            base_entry["template"] = template_name
            base_entry["bitrate"] = clamp(int(config.get("default_bitrate", 64000)), 8000, int(member.guild.bitrate_limit))
            base_entry["user_limit"] = clamp(int(config.get("default_user_limit", 0)), 0, 99)
            self.apply_template_to_entry(base_entry, template_name, config)
            default_preset_name, default_preset_settings = await self.get_user_default_preset(member.guild.id, member.id)
            if default_preset_settings:
                self.apply_user_preset_to_entry(base_entry, default_preset_settings)
                self.add_log(base_entry, member.id, "preset_default_applied", f"Applied default preset {default_preset_name}")
            base_entry["member_join_times"] = {str(member.id): now_iso()}
            self.add_log(base_entry, member.id, "channel_created", f"Created from generator {generator_channel.name}")

            _, skipped_permit_roles = self._partition_manageable_roles_for_overwrites(
                member.guild,
                base_entry.get("permitted_roles", []),
            )
            _, skipped_ban_roles = self._partition_manageable_roles_for_overwrites(
                member.guild,
                base_entry.get("banned_roles", []),
            )
            skipped_role_labels = list(dict.fromkeys(skipped_permit_roles + skipped_ban_roles))
            if skipped_role_labels:
                skipped_text = ", ".join(skipped_role_labels[:10])
                if len(skipped_role_labels) > 10:
                    skipped_text = f"{skipped_text}, +{len(skipped_role_labels) - 10} more"
                self.add_log(
                    base_entry,
                    None,
                    "overwrite_roles_skipped",
                    f"Skipped role overwrites (hierarchy): {skipped_text}",
                )
            initial_overwrites = self._build_channel_overwrites(member.guild, base_entry)
            create_reason = f"Temporary VC created for {member}"
            create_kwargs = {
                "name": channel_name,
                "category": category,
                "user_limit": base_entry["user_limit"],
                "bitrate": clamp(base_entry["bitrate"], 8000, int(member.guild.bitrate_limit)),
                "rtc_region": None if base_entry["region"] == "auto" else base_entry["region"],
                "reason": create_reason,
            }

            try:
                temp_channel = await member.guild.create_voice_channel(
                    **create_kwargs,
                    overwrites=initial_overwrites,
                )
            except discord.Forbidden:
                self.add_log(
                    base_entry,
                    None,
                    "initial_overwrites_skipped",
                    "Could not apply full initial overwrites; retrying creation without explicit overwrites.",
                )
                try:
                    temp_channel = await member.guild.create_voice_channel(**create_kwargs)
                except discord.Forbidden as fallback_exc:
                    target_scope = f"category '{category.name}'" if isinstance(category, discord.CategoryChannel) else "guild root"
                    raise PermissionError(
                        f"Discord denied channel creation in {target_scope}. Check Manage Channels for the bot role and role hierarchy."
                    ) from fallback_exc

            try:
                await self.sync_channel(temp_channel, base_entry, reason="Apply initial temporary VC permissions")
            except discord.Forbidden:
                self.add_log(
                    base_entry,
                    None,
                    "initial_sync_permissions_skipped",
                    "Could not sync full temporary VC overwrites after creation; keeping channel with default/base permissions.",
                )
            await self.upsert_entry(temp_channel.id, base_entry)

            try:
                await member.move_to(temp_channel, reason="Moved to generated temporary VC")
            except Exception:
                self.add_log(base_entry, None, "move_failed", "Could not move owner automatically")
                await self.upsert_entry(temp_channel.id, base_entry)
            await self._send_creation_setup_embed(
                temp_channel,
                member,
                default_preset_name=default_preset_name,
            )

            await self._emit_log_channel_message(
                member.guild,
                embed=discord.Embed(
                    title="Temporary VC Created",
                    description=f"{member.mention} created {temp_channel.mention}",
                    color=0x57F287,
                    timestamp=datetime.now(timezone.utc),
                ),
            )

    async def mark_member_join(self, member: discord.Member, channel: discord.VoiceChannel):
        entry = await self.get_entry(channel.id)
        if not entry:
            return
        entry.setdefault("member_join_times", {})
        member_id = str(member.id)
        if member_id not in entry["member_join_times"]:
            entry["member_join_times"][member_id] = now_iso()
            await self.upsert_entry(channel.id, entry)

    async def handle_member_left(self, member: discord.Member, channel: discord.VoiceChannel):
        entry = await self.get_entry(channel.id)
        if not entry:
            return

        member_id_str = str(member.id)
        if member_id_str in entry.get("member_join_times", {}):
            entry["member_join_times"].pop(member_id_str, None)

        if len(channel.members) == 0:
            try:
                await channel.delete(reason="Temporary VC emptied")
            except Exception:
                pass
            await self.remove_entry(channel.id)
            return

        owner_id = int(entry.get("owner_id") or 0)
        if owner_id == member.id:
            entry["owner_id"] = None
            self.add_log(entry, member.id, "owner_left", "Owner left the channel; VC is now claimable")
            try:
                await self.sync_channel(channel, entry, reason="Owner left temporary VC")
            except Exception:
                pass

        await self.upsert_entry(channel.id, entry)

    async def cleanup_deleted_channel(self, channel_id: int):
        await self.remove_entry(channel_id)

    async def reconcile_state(self):
        if not TEMP_VC_STATE_DIR.exists():
            return

        for entry_path in TEMP_VC_STATE_DIR.glob("*.json"):
            if not entry_path.is_file():
                continue

            channel_id_str = entry_path.stem
            if not channel_id_str.isdigit():
                try:
                    entry_path.unlink()
                except OSError:
                    pass
                continue

            channel_id = int(channel_id_str)
            payload = self._read_json(entry_path, {})
            if not isinstance(payload, dict):
                await self.remove_entry(channel_id)
                continue

            guild_id = int(payload.get("guild_id", 0) or 0)
            guild = self.bot.get_guild(guild_id) if guild_id else None
            if guild is None:
                await self.remove_entry(channel_id)
                continue

            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                await self.remove_entry(channel_id)
                continue

            entry = self._normalize_channel_entry(payload, guild.id)
            if len(channel.members) == 0:
                try:
                    await channel.delete(reason="Cleaning stale temporary VC")
                except Exception:
                    pass
                await self.remove_entry(channel_id)
                continue

            try:
                await self.sync_channel(channel, entry, reason="Reconcile temporary VC state")
            except Exception:
                pass

            await self.upsert_entry(channel_id, entry)

    def get_oldest_member(self, channel: discord.VoiceChannel, entry: Dict[str, Any]) -> Optional[discord.Member]:
        join_map = entry.get("member_join_times", {})
        ranked = []
        for member in channel.members:
            ts = parse_iso(join_map.get(str(member.id), now_iso()))
            ranked.append((ts, member.id, member))
        if not ranked:
            return None
        ranked.sort(key=lambda item: (item[0], item[1]))
        return ranked[0][2]

    def build_panel_embed(self, channel: discord.VoiceChannel, entry: Dict[str, Any], viewer: discord.Member, status: str = "") -> discord.Embed:
        owner_id = int(entry.get("owner_id") or 0)
        owner_text = f"<@{owner_id}>" if owner_id else "*Unclaimed*"
        state_text = (
            f"Locked: **{'Yes' if entry.get('locked') else 'No'}**\n"
            f"Hidden: **{'Yes' if entry.get('hidden') else 'No'}**\n"
            f"Push-to-talk: **{'On' if entry.get('push_to_talk') else 'Off'}**\n"
            f"Template: **{entry.get('template', 'default')}**"
        )

        members = []
        for member in channel.members:
            crown = " 👑" if owner_id and member.id == owner_id else ""
            members.append(f"{member.mention}{crown}")
        members_text = ", ".join(members) if members else "*No members*"

        permitted_users = [f"<@{uid}>" for uid in entry.get("permitted_users", [])]
        permitted_roles = [role_mention(rid) for rid in entry.get("permitted_roles", [])]
        banned_users = [f"<@{uid}>" for uid in entry.get("banned_users", [])]
        banned_roles = [role_mention(rid) for rid in entry.get("banned_roles", [])]
        permits_text = ", ".join((permitted_users + permitted_roles)[:20]) or "*None*"
        banned_text = ", ".join((banned_users + banned_roles)[:20]) or "*None*"

        can_manage = self.can_manage(viewer, entry)
        subtitle = "You have management access." if can_manage else "You do not have management access for this temporary VC."

        embed = discord.Embed(
            title=f"Temp VC Panel - {channel.name}",
            description=subtitle,
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Owner", value=owner_text, inline=True)
        embed.add_field(name="Members", value=str(len(channel.members)), inline=True)
        embed.add_field(name="User Limit", value=str(entry.get("user_limit", 0) or "0 (unlimited)"), inline=True)
        embed.add_field(name="State", value=state_text, inline=False)
        embed.add_field(
            name="Channel Settings",
            value=(
                f"**Bitrate:** {int(entry.get('bitrate', 64000)) // 1000} kbps\n"
                f"**Region:** {entry.get('region', 'auto')}"
            ),
            inline=True,
        )
        embed.add_field(name="Members in VC", value=members_text, inline=False)
        embed.add_field(name="Permitted Users/Roles", value=permits_text, inline=False)
        embed.add_field(name="Banned Users/Roles", value=banned_text, inline=False)
        if status:
            embed.set_footer(text=status)
        return embed

    def build_info_embed(self, channel: discord.VoiceChannel, entry: Dict[str, Any]) -> discord.Embed:
        owner_id = int(entry.get("owner_id") or 0)
        owner_text = f"<@{owner_id}>" if owner_id else "*Unclaimed*"
        created = entry.get("created_at")
        created_display = f"<t:{int(parse_iso(created).timestamp())}:R>" if created else "Unknown"
        embed = discord.Embed(
            title="ℹ️ Temporary VC Info",
            color=0x00B0F4,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Channel", value=channel.mention, inline=False)
        embed.add_field(name="Owner", value=owner_text, inline=True)
        embed.add_field(name="Created", value=created_display, inline=True)
        embed.add_field(name="Template", value=entry.get("template", "default"), inline=True)
        embed.add_field(name="Locked", value="Yes" if entry.get("locked") else "No", inline=True)
        embed.add_field(name="Hidden", value="Yes" if entry.get("hidden") else "No", inline=True)
        embed.add_field(name="Push-to-talk", value="On" if entry.get("push_to_talk") else "Off", inline=True)
        embed.add_field(name="User Limit", value=str(entry.get("user_limit", 0) or "Unlimited"), inline=True)
        embed.add_field(name="Bitrate", value=f"{int(entry.get('bitrate', 64000)) // 1000} kbps", inline=True)
        embed.add_field(name="Region", value=entry.get("region", "auto"), inline=True)
        return embed

    def build_logs_embed(self, channel: discord.VoiceChannel, entry: Dict[str, Any]) -> discord.Embed:
        embed = discord.Embed(
            title=f"Temp VC Logs - {channel.name}",
            color=0xFEE75C,
            timestamp=datetime.now(timezone.utc),
        )
        logs = entry.get("logs", [])
        if not logs:
            embed.description = "*No logs recorded yet.*"
            return embed

        lines = []
        for item in logs[-15:]:
            timestamp = parse_iso(item.get("timestamp", now_iso()))
            actor_id = item.get("actor_id")
            actor = f"<@{actor_id}>" if actor_id else "System"
            action = item.get("action", "unknown_action")
            details = item.get("details", "")
            lines.append(f"`{timestamp.strftime('%H:%M:%S')}` **{action}** by {actor} {details}".strip())

        embed.description = "\n".join(lines)
        return embed

    async def send_knock_notification(self, requester: discord.Member, channel: discord.VoiceChannel, entry: Dict[str, Any]) -> str:
        owner_id = int(entry.get("owner_id") or 0)
        if owner_id <= 0:
            return "This temporary VC has no owner to notify."
        if requester.id == owner_id:
            return "You already own this temporary VC."
        banned_users = {int(uid) for uid in entry.get("banned_users", []) if str(uid).isdigit()}
        banned_roles = {int(rid) for rid in entry.get("banned_roles", []) if str(rid).isdigit()}
        requester_role_ids = {role.id for role in requester.roles}
        if requester.id in banned_users or (requester_role_ids & banned_roles):
            return "You are banned from this VC and cannot knock."
        permissions = channel.permissions_for(requester)
        if permissions.view_channel and permissions.connect:
            return "You already have access to this VC, so you can't knock."

        owner_member = requester.guild.get_member(owner_id)
        if owner_member is None:
            try:
                owner_member = await requester.guild.fetch_member(owner_id)
            except Exception:
                owner_member = None
        if owner_member is None:
            return "The VC owner could not be found right now."


        knock_embed = discord.Embed(
            title="VC Knock Request",
            description=f"{requester.mention} wants to join {channel.mention}.",
            color=0xFAA61A,
            timestamp=datetime.now(timezone.utc),
        )
        knock_embed.add_field(name="Requester", value=requester.mention, inline=True)
        knock_embed.add_field(name="Owner", value=owner_member.mention, inline=True)
        knock_embed.set_footer(text="Only the VC owner can use the buttons below.")

        try:
            await channel.send(
                content=owner_member.mention,
                embed=knock_embed,
                view=KnockResponseView(self, channel.id, requester.id),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except Exception:
            return "Failed to post the knock request in this VC chat."

        self.add_log(entry, requester.id, "knock", f"Knock requested for {channel.name}")
        await self.upsert_entry(channel.id, entry)

        await self._emit_log_channel_message(
            requester.guild,
            embed=discord.Embed(
                title="VC Knock",
                description=f"{requester.mention} knocked on {channel.mention}",
                color=0xFAA61A,
                timestamp=datetime.now(timezone.utc),
            ),
        )

        return f"Knock request posted in {channel.mention} and sent to {owner_member.mention}."


async def send_ephemeral(
    interaction: discord.Interaction,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
):
    payload: Dict[str, Any] = {"ephemeral": True}
    if content is not None:
        payload["content"] = content
    if embed is not None:
        payload["embed"] = embed
    if view is not None:
        payload["view"] = view

    response = getattr(interaction, "response", None)
    followup = getattr(interaction, "followup", None)

    if response and hasattr(response, "is_done") and response.is_done():
        if followup and hasattr(followup, "send"):
            await followup.send(**payload)
            return
    elif response and hasattr(response, "send_message"):
        await response.send_message(**payload)
        return

    if followup and hasattr(followup, "send"):
        await followup.send(**payload)
        return

    raise RuntimeError("Unable to send ephemeral response for interaction.")
