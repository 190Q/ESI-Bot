"""Wynncraft /player profile card command."""

import asyncio
import base64
import io
import json
import math
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from utils import errors


load_dotenv()

WYNNCRAFT_API_BASE = "https://api.wynncraft.com/v3"
NMSR_API_BASE = "https://nmsr.nickac.dev"
ATHENA_API_BASE = "https://athena.wynntils.com"
MOJANG_SESSION_BASE = "https://sessionserver.mojang.com"
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

WYNNTILS_HEADERS = {
    "User-Agent": "Wynntils Artemis PlayerCard/1.0",
    "Accept": "application/json",
}
NMSR_HEADERS = {"User-Agent": "Wynncraft-PlayerCard/1.0"}
MOJANG_HEADERS = {"User-Agent": "Wynncraft-PlayerCard/1.0", "Accept": "application/json"}

WYNNCRAFT_KEYS = [
    key
    for key in (
        os.getenv("WYNNCRAFT_KEY_1"),
        os.getenv("WYNNCRAFT_KEY_2"),
        os.getenv("WYNNCRAFT_KEY_3"),
    )
    if key
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMAGES_DIR = PROJECT_ROOT / "images"
ICONS_DIR = IMAGES_DIR / "icons"
PREFERRED_IMAGE_EXTENSIONS = (".webp", ".png")
BACKGROUND_PATH = IMAGES_DIR / "suscardbackground.png"


RAIDS = [
    ("Nol", "Orphion's Nexus of Light", "Nol.webp"),
    ("Tcc", "The Canyon Colossus", "Tcc.webp"),
    ("Tna", "The Nameless Anomaly", "Tna.webp"),
    ("Notg", "Nest of the Grootslangs", "Notg.webp"),
    ("Wtp", "The Wartorn Palace", "Wtp.webp"),
]

# Gemcraft is only shown if the API returns it.
PROFESSIONS = [
    ("armouring", "Armouring", "armor.webp"),
    ("farming", "Farming", "crop.webp"),
    ("woodcutting", "Woodcutting", "axe.webp"),
    ("woodworking", "Woodworking", "arrow.webp"),
    ("gemcraft", "Gemcraft", "emerald.webp"),
    ("fishing", "Fishing", "fish.webp"),
    ("cooking", "Cooking", "food.webp"),
    ("mining", "Mining", "pickaxe.webp"),
    ("alchemism", "Alchemy", "potion.webp"),
    ("jeweling", "Jeweling", "ring.webp"),
    ("scribing", "Scribing", "scribe.webp"),
    ("weaponsmithing", "Weaponsmithing", "sword.webp"),
    ("tailoring", "Tailoring", "tailoring.webp"),
]

def _resolve_image_path(
    base_dir: Path,
    filename: str,
    *,
    preferred_exts=PREFERRED_IMAGE_EXTENSIONS,
) -> Optional[Path]:
    """Prefer WebP and fall back to PNG."""
    raw = Path(filename)
    directory = raw.parent if raw.is_absolute() else Path(base_dir)
    stem = raw.stem if raw.suffix else raw.name

    candidates = [raw] if raw.is_absolute() else [directory / raw.name]
    candidates.extend(directory / f"{stem}{ext}" for ext in preferred_exts)

    for path in dict.fromkeys(candidates):
        try:
            if path.is_file():
                return path
        except OSError:
            pass
    return None


class WynncraftAPI:
    _key_index = 0

    @classmethod
    def headers(cls) -> Dict[str, str]:
        if not WYNNCRAFT_KEYS:
            return {}
        key = WYNNCRAFT_KEYS[cls._key_index % len(WYNNCRAFT_KEYS)]
        cls._key_index += 1
        return {"apikey": key}

    @staticmethod
    def _uuid_candidates(value: str) -> List[str]:
        raw = str(value or "").strip()
        compact = raw.replace("-", "")
        candidates = [raw]
        if len(compact) == 32:
            dashed = (
                f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-"
                f"{compact[16:20]}-{compact[20:]}"
            )
            candidates.extend((compact, dashed))
        return list(dict.fromkeys(candidate for candidate in candidates if candidate))

    @staticmethod
    async def _read_json(session: aiohttp.ClientSession, url: str) -> Optional[Dict]:
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None

    @staticmethod
    async def _fetch_bytes_with_retry(
        session: aiohttp.ClientSession,
        url: str,
    ) -> Tuple[Optional[bytes], bool]:
        fallback_bytes: Optional[bytes] = None
        for attempt in range(2):
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.read()
                        if not data:
                            return None, False
                        is_fallback = response.headers.get("x-nmsr-fallback", "").lower() == "true"
                        if is_fallback:
                            fallback_bytes = fallback_bytes or data
                            break
                        return data, False
                    if response.status not in RETRYABLE_STATUSES:
                        break
            except (aiohttp.ClientError, TimeoutError):
                pass
            if attempt == 0:
                await asyncio.sleep(0.4)
        return fallback_bytes, bool(fallback_bytes)

    @staticmethod
    async def _render_with_cape(
        session: aiohttp.ClientSession,
        raw_skin: bytes,
        cape_bytes: bytes,
        player_model: str,
    ) -> Optional[bytes]:
        for attempt in range(2):
            form = aiohttp.FormData()
            form.add_field("skin", raw_skin, filename="skin.png", content_type="image/png")
            form.add_field("cape", cape_bytes, filename="cape.png", content_type="image/png")
            form.add_field("model", player_model)
            try:
                async with session.post(f"{NMSR_API_BASE}/bodybust", data=form) as response:
                    if response.status == 200:
                        data = await response.read()
                        if data:
                            return data
                    if response.status not in RETRYABLE_STATUSES:
                        break
            except (aiohttp.ClientError, TimeoutError):
                pass
            if attempt == 0:
                await asyncio.sleep(0.4)
        return None

    @staticmethod
    def _decode_cape_texture(encoded: str) -> Optional[bytes]:
        if not encoded:
            return None
        try:
            cape_bytes = base64.b64decode(encoded, validate=True)
            cape = Image.open(io.BytesIO(cape_bytes)).convert("RGBA")
            frame_height = cape.width // 2
            if frame_height > 0 and cape.height > frame_height:
                cape = cape.crop((0, 0, cape.width, frame_height))
            output = io.BytesIO()
            cape.save(output, format="PNG")
            return output.getvalue()
        except (ValueError, OSError, TypeError):
            return None

    @classmethod
    async def fetch_player(cls, name_or_uuid: str) -> Optional[Dict]:
        url = f"{WYNNCRAFT_API_BASE}/player/{name_or_uuid}?fullResult"
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=cls.headers()) as response:
                    if response.status == 200:
                        return await response.json(content_type=None)
                    if response.status == 300:
                        return {"multiple": True, "players": await response.json(content_type=None)}
                    return None
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            print(f"[playercard] player fetch failed: {exc}")
            return None

    @classmethod
    async def fetch_guild_contribution(cls, player_data: Dict) -> int:
        guild = player_data.get("guild") or {}
        guild_uuid = guild.get("uuid")
        username = str(player_data.get("username") or "")
        player_uuid = str(player_data.get("uuid") or "").replace("-", "").lower()
        if not guild_uuid:
            return 0

        url = f"{WYNNCRAFT_API_BASE}/guild/uuid/{guild_uuid}"
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=cls.headers()) as response:
                    if response.status != 200:
                        return 0
                    payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return 0

        members = payload.get("members") or {}
        for rank_group in members.values():
            if not isinstance(rank_group, dict):
                continue
            for member_name, member_data in rank_group.items():
                if not isinstance(member_data, dict):
                    continue
                member_uuid = str(member_data.get("uuid") or "").replace("-", "").lower()
                same_name = str(member_name).casefold() == username.casefold()
                same_uuid = bool(player_uuid and member_uuid and player_uuid == member_uuid)
                if same_name or same_uuid:
                    try:
                        return int(member_data.get("contributed") or 0)
                    except (TypeError, ValueError):
                        return 0
        return 0

    @classmethod
    async def fetch_wynntils_cape(cls, player_uuid: str) -> Optional[bytes]:
        identifiers = cls._uuid_candidates(player_uuid)
        if not identifiers:
            return None

        timeout = aiohttp.ClientTimeout(total=8)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=WYNNTILS_HEADERS) as session:
                for identifier in identifiers:
                    payload = await cls._read_json(session, f"{ATHENA_API_BASE}/user/getInfo/{identifier}")
                    cosmetics = ((payload or {}).get("user") or {}).get("cosmetics") or {}
                    if cosmetics.get("hasCape") is not True:
                        continue
                    cape_bytes = cls._decode_cape_texture(cosmetics.get("texture"))
                    if cape_bytes:
                        return cape_bytes
        except (aiohttp.ClientError, TimeoutError):
            return None
        return None

    @staticmethod
    async def fetch_skin_model(player_uuid: str) -> Optional[str]:
        compact_uuid = str(player_uuid or "").replace("-", "").strip()
        if len(compact_uuid) != 32:
            return None

        timeout = aiohttp.ClientTimeout(total=8)
        url = f"{MOJANG_SESSION_BASE}/session/minecraft/profile/{compact_uuid}"
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=MOJANG_HEADERS) as session:
                payload = await WynncraftAPI._read_json(session, url)
            if not payload:
                return None
            for prop in payload.get("properties") or []:
                if prop.get("name") != "textures":
                    continue
                value = prop.get("value")
                if not value:
                    continue
                padded = value + "=" * (-len(value) % 4)
                decoded = base64.b64decode(padded)
                texture_data = json.loads(decoded.decode("utf-8"))
                metadata = ((((texture_data.get("textures") or {}).get("SKIN") or {}).get("metadata")) or {})
                return "alex" if str(metadata.get("model", "")).lower() == "slim" else "steve"
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, UnicodeDecodeError):
            return None
        return None

    @classmethod
    async def fetch_skin(
        cls,
        player_uuid: str,
        username: str = "",
        cape_bytes: Optional[bytes] = None,
    ) -> Optional[bytes]:
        identifiers = list(dict.fromkeys(value for value in (player_uuid, username) if value))
        if not identifiers:
            return None

        timeout = aiohttp.ClientTimeout(total=18)
        fallback_render: Optional[bytes] = None

        async with aiohttp.ClientSession(timeout=timeout, headers=NMSR_HEADERS) as session:
            if cape_bytes:
                player_model = await cls.fetch_skin_model(player_uuid)
                if player_model:
                    for identifier in identifiers:
                        raw_skin, _ = await cls._fetch_bytes_with_retry(
                            session,
                            f"{NMSR_API_BASE}/skin/{identifier}",
                        )
                        if not raw_skin:
                            continue
                        rendered = await cls._render_with_cape(session, raw_skin, cape_bytes, player_model)
                        if rendered:
                            return rendered

            for identifier in identifiers:
                rendered, is_fallback = await cls._fetch_bytes_with_retry(
                    session,
                    f"{NMSR_API_BASE}/bodybust/{identifier}",
                )
                if rendered and not is_fallback:
                    return rendered
                fallback_render = fallback_render or rendered

        return fallback_render


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def _normalise_name(value: str) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _compact_number(value: int) -> str:
    """Truncate large values to one decimal place."""
    number = max(0, _to_int(value))
    for divisor, suffix in (
        (1_000_000_000_000, "T"),
        (1_000_000_000, "B"),
        (1_000_000, "M"),
        (1_000, "K"),
    ):
        if number >= divisor:
            truncated = math.floor((number / divisor) * 10.0) / 10.0
            text = f"{truncated:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{number:,}"


def _rank_display(player_data: Dict) -> str:
    support_rank = player_data.get("supportRank")
    if support_rank:
        rank = {
            "vipplus": "VIP+",
            "heroplus": "HERO+",
        }.get(str(support_rank).lower(), str(support_rank).upper())
    else:
        rank = str(player_data.get("shortenedRank") or player_data.get("rank") or "NO RANK")
    if player_data.get("veteran"):
        rank += " (VET)"
    return rank


def _best_professions(player_data: Dict) -> Dict[str, Dict[str, float]]:
    """Keep the highest version of each profession across characters."""
    best: Dict[str, Dict[str, float]] = {}
    characters = player_data.get("characters") or {}
    if not isinstance(characters, dict):
        return best

    for character in characters.values():
        if not isinstance(character, dict):
            continue
        professions = character.get("professions") or {}
        if not isinstance(professions, dict):
            continue

        for key, raw in professions.items():
            if not isinstance(raw, dict):
                continue
            key = str(key).lower()
            level = _to_int(raw.get("level"))
            percent = _clamp(_to_float(raw.get("xpPercent")))
            current = best.get(key)
            if current is None or level > current["level"] or (
                level == current["level"] and percent > current["xpPercent"]
            ):
                best[key] = {"level": level, "xpPercent": percent}
    return best


def _raid_counts(global_data: Dict) -> Tuple[Dict[str, int], int]:
    raids = global_data.get("raids") or {}
    if not isinstance(raids, dict):
        raids = {}

    raid_list = raids.get("list") or {}
    counts = {short: 0 for short, _, _ in RAIDS}

    aliases = {}
    for short, full_name, _ in RAIDS:
        aliases[_normalise_name(short)] = short
        aliases[_normalise_name(full_name)] = short

    if isinstance(raid_list, dict):
        for raw_name, raw_count in raid_list.items():
            short = aliases.get(_normalise_name(raw_name))
            if short:
                counts[short] += _to_int(raw_count)

    total = _to_int(raids.get("total"))
    if total <= 0:
        total = sum(counts.values())
    return counts, total


def build_card_data(player_data: Dict, guild_xp: int = 0) -> Dict:
    global_data = player_data.get("globalData") or {}
    if not isinstance(global_data, dict):
        global_data = {}

    raid_counts, total_raids = _raid_counts(global_data)
    dungeons = global_data.get("dungeons") or {}
    dungeon_total = _to_int(dungeons.get("total")) if isinstance(dungeons, dict) else 0

    guild = player_data.get("guild") or {}
    guild_name = str(guild.get("name") or "None")
    guild_prefix = str(guild.get("prefix") or "").strip()
    guild_rank = str(guild.get("rank") or "None").replace("_", " ").title()

    return {
        "username": str(player_data.get("username") or "Unknown"),
        "uuid": str(player_data.get("uuid") or ""),
        "rank": _rank_display(player_data),
        "guild_name": guild_name,
        "guild_prefix": guild_prefix,
        "guild_rank": guild_rank,
        "guild_xp": max(0, _to_int(guild_xp)),
        "wars": _to_int(global_data.get("wars")),
        "playtime": _to_float(player_data.get("playtime")),
        "total_level": _to_int(global_data.get("totalLevel")),
        "quests": _to_int(global_data.get("completedQuests")),
        "dungeons": dungeon_total,
        "chests": _to_int(global_data.get("chestsFound")),
        "raid_counts": raid_counts,
        "total_raids": total_raids,
        "professions": _best_professions(player_data),
    }


class PlayerCardImageGenerator:
    WIDTH = 1650
    HEIGHT = 900

    _BACKGROUND_CACHE: Optional[Image.Image] = None
    _ICON_CACHE: Dict[str, Image.Image] = {}

    PANEL = (10, 26, 40, 226)
    PANEL_SOFT = (13, 31, 48, 214)
    BORDER = (116, 148, 177, 190)
    BORDER_SOFT = (87, 113, 139, 125)
    WHITE = (245, 247, 250, 255)
    MUTED = (177, 190, 214, 255)
    TRACK = (36, 62, 86, 235)
    ACCENT = (77, 190, 245, 255)
    SEPARATOR = (170, 181, 194, 255)

    CARD_COLORS = [
        (255, 74, 92, 255),
        (0, 239, 111, 255),
        (18, 191, 239, 255),
        (0, 230, 145, 255),
        (255, 208, 0, 255),
        (211, 0, 239, 255),
    ]

    @staticmethod
    def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        candidates = (
            ["arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
            if bold
            else ["arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
        )
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

    @classmethod
    def _fit_font(
        cls,
        draw: ImageDraw.ImageDraw,
        text: str,
        max_width: int,
        start_size: int,
        min_size: int = 14,
        *,
        bold: bool = True,
    ):
        size = start_size
        while size >= min_size:
            font = cls._font(size, bold=bold)
            if draw.textlength(str(text), font=font) <= max_width:
                return font
            size -= 1
        return cls._font(min_size, bold=bold)

    @classmethod
    def _fit_text(
        cls,
        draw: ImageDraw.ImageDraw,
        text: str,
        max_width: int,
        start_size: int,
        min_size: int = 14,
        *,
        bold: bool = True,
    ):
        text = str(text)
        font = cls._fit_font(draw, text, max_width, start_size, min_size, bold=bold)
        if draw.textlength(text, font=font) <= max_width:
            return text, font

        ellipsis = "…"
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            candidate = text[:mid].rstrip() + ellipsis
            if draw.textlength(candidate, font=font) <= max_width:
                low = mid
            else:
                high = mid - 1
        return (text[:low].rstrip() + ellipsis if low else ellipsis), font

    @staticmethod
    def _panel(draw: ImageDraw.ImageDraw, box, *, fill, outline=None, radius=18, width=2):
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)

    @classmethod
    def _background(cls) -> Image.Image:
        if cls._BACKGROUND_CACHE is None:
            path = BACKGROUND_PATH if BACKGROUND_PATH.is_file() else None
            try:
                if path:
                    background = Image.open(path).convert("RGB")
                    background = ImageOps.fit(
                        background,
                        (cls.WIDTH, cls.HEIGHT),
                        method=Image.Resampling.LANCZOS,
                    )
                    background = background.filter(ImageFilter.GaussianBlur(2.4)).convert("RGBA")
                else:
                    raise OSError("background image not found")
            except (OSError, ValueError):
                background = Image.new("RGBA", (cls.WIDTH, cls.HEIGHT), (18, 30, 43, 255))

            dark = Image.new("RGBA", background.size, (4, 13, 24, 154))
            cls._BACKGROUND_CACHE = Image.alpha_composite(background, dark)

        return cls._BACKGROUND_CACHE.copy()

    @staticmethod
    def _icon_path(filename: str) -> Optional[Path]:
        return _resolve_image_path(ICONS_DIR, filename)

    @classmethod
    def _paste_local_icon(
        cls,
        canvas: Image.Image,
        filename: str,
        box,
        *,
        pixel_art: bool = False,
    ) -> None:
        path = cls._icon_path(filename)
        if not path:
            return
        try:
            key = str(path.resolve())
            cached = cls._ICON_CACHE.get(key)
            if cached is None:
                cached = Image.open(path).convert("RGBA")
                cls._ICON_CACHE[key] = cached
            icon = cached.copy()

            max_w = max(1, box[2] - box[0])
            max_h = max(1, box[3] - box[1])
            resample = Image.Resampling.NEAREST if pixel_art else Image.Resampling.LANCZOS
            icon.thumbnail((max_w, max_h), resample)
            x = box[0] + (max_w - icon.width) // 2
            y = box[1] + (max_h - icon.height) // 2
            canvas.alpha_composite(icon, (x, y))
        except (OSError, ValueError):
            return

    @classmethod
    def _paste_skin(cls, canvas: Image.Image, skin_bytes: Optional[bytes], box) -> None:
        if not skin_bytes:
            return
        try:
            skin = Image.open(io.BytesIO(skin_bytes)).convert("RGBA")
            max_w = box[2] - box[0]
            max_h = box[3] - box[1]
            skin.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
            x = box[0] + (max_w - skin.width) // 2
            y = box[1] + (max_h - skin.height) // 2
            canvas.alpha_composite(skin, (x, y))
        except (OSError, ValueError):
            return

    @classmethod
    def _rank_color(cls, rank: str):
        clean = str(rank).lower().replace(" (vet)", "")
        return {
            "vip": (85, 255, 85, 255),
            "vip+": (85, 85, 255, 255),
            "hero": (216, 0, 225, 255),
            "hero+": (244, 0, 255, 255),
            "champion": (255, 170, 0, 255),
        }.get(clean, cls.WHITE)

    @classmethod
    def _draw_metric_card(cls, draw: ImageDraw.ImageDraw, box, label: str, value: str, color) -> None:
        x1, y1, x2, y2 = box
        cls._panel(draw, box, fill=cls.PANEL, outline=cls.BORDER, radius=17, width=2)
        draw.rounded_rectangle((x1, y1, x1 + 18, y2), radius=16, fill=color)
        draw.rectangle((x1 + 10, y1 + 1, x1 + 18, y2 - 1), fill=color)

        usable = x2 - x1 - 72
        label_text, label_font = cls._fit_text(draw, label.upper(), usable, 25, 18)
        draw.text((x1 + 44, y1 + 20), label_text, font=label_font, fill=cls.MUTED)

        value_text, value_font = cls._fit_text(draw, value, usable, 56, 30)
        draw.text((x1 + 44, y1 + 57), value_text, font=value_font, fill=cls.WHITE)

    @classmethod
    def _draw_left_column(cls, canvas: Image.Image, draw: ImageDraw.ImageDraw, data: Dict, skin_bytes: Optional[bytes]):
        panel = (32, 20, 430, 880)
        cls._panel(draw, panel, fill=cls.PANEL, outline=cls.BORDER, radius=19, width=2)
        cls._paste_skin(canvas, skin_bytes, (63, 45, 399, 390))

        username = data["username"]
        username_text, username_font = cls._fit_text(draw, username, 350, 48, 28)
        username_w = draw.textlength(username_text, font=username_font)
        draw.text((231 - username_w / 2, 416), username_text, font=username_font, fill=cls.WHITE)

        rank = data["rank"]
        rank_text, rank_font = cls._fit_text(draw, rank, 330, 38, 24)
        rank_w = draw.textlength(rank_text, font=rank_font)
        draw.text((231 - rank_w / 2, 494), rank_text, font=rank_font, fill=cls._rank_color(rank))

        inner_margin = 15
        gap = 15
        box_left = panel[0] + inner_margin
        box_right = panel[2] - inner_margin
        top_y = 557
        bottom_y = panel[3] - inner_margin
        available_h = bottom_y - top_y - gap
        top_box_h = available_h // 2
        bottom_box_h = available_h - top_box_h

        guild_box = (box_left, top_y, box_right, top_y + top_box_h)
        gxp_box = (box_left, guild_box[3] + gap, box_right, guild_box[3] + gap + bottom_box_h)

        cls._panel(draw, guild_box, fill=cls.PANEL_SOFT, outline=cls.BORDER_SOFT, radius=17, width=2)
        gx1, gy1, gx2, gy2 = guild_box
        text_x = gx1 + 22

        draw.text((text_x, gy1 + 20), "GUILD", font=cls._font(23, True), fill=cls.MUTED)
        guild_label = data["guild_name"]
        if data["guild_prefix"]:
            guild_label += f" [{data['guild_prefix']}]"
        guild_text, guild_font = cls._fit_text(draw, guild_label, gx2 - text_x - 20, 38, 21)
        draw.text((text_x, gy1 + 52), guild_text, font=guild_font, fill=cls.WHITE)

        rank_line, rank_line_font = cls._fit_text(
            draw,
            f"Rank: {data['guild_rank']}",
            gx2 - text_x - 20,
            25,
            18,
        )
        draw.text((text_x, gy1 + 102), rank_line, font=rank_line_font, fill=(164, 177, 214, 255))

        cls._panel(draw, gxp_box, fill=cls.PANEL_SOFT, outline=cls.BORDER_SOFT, radius=17, width=2)
        x1, y1, x2, y2 = gxp_box
        draw.rounded_rectangle((x1, y1, x1 + 20, y2), radius=16, fill=(255, 74, 92, 255))
        draw.rectangle((x1 + 10, y1 + 1, x1 + 20, y2 - 1), fill=(255, 74, 92, 255))

        text_x = x1 + 41
        draw.text((text_x, y1 + 20), "GUILD XP", font=cls._font(22, True), fill=cls.MUTED)
        gxp = _compact_number(data["guild_xp"])
        gxp_text, gxp_font = cls._fit_text(draw, gxp, x2 - text_x - 22, 55, 34)
        draw.text((text_x, y1 + 62), gxp_text, font=gxp_font, fill=cls.WHITE)

    @classmethod
    def _draw_raids(cls, canvas: Image.Image, draw: ImageDraw.ImageDraw, data: Dict):
        panel = (445, 335, 1060, 880)
        cls._panel(draw, panel, fill=cls.PANEL, outline=cls.BORDER, radius=18, width=2)

        title_x = panel[0] + 22
        title_y = panel[1] + 17
        separator_y = panel[1] + 62
        line_x1 = panel[0] + 22
        line_x2 = panel[2] - 27

        draw.text((title_x, title_y), "RAIDS", font=cls._font(34, True), fill=cls.WHITE)
        draw.line((line_x1, separator_y, line_x2, separator_y), fill=cls.SEPARATOR, width=2)

        inset_x = 18
        gap_x = 18
        gap_y = 16
        cell_h = 139
        top_y = 416
        left_x = panel[0] + inset_x
        right_x = panel[2] - inset_x
        cell_w = (right_x - left_x - gap_x) // 2
        x_positions = [left_x, left_x + cell_w + gap_x]

        for index, (short, _, icon_file) in enumerate(RAIDS):
            row = index // 2
            col = index % 2
            x1 = x_positions[col]
            y1 = top_y + row * (cell_h + gap_y)
            box = (x1, y1, x1 + cell_w, y1 + cell_h)

            cls._panel(draw, box, fill=cls.PANEL_SOFT, outline=(83, 129, 167, 160), radius=15, width=2)
            cls._paste_local_icon(canvas, icon_file, (x1 + 14, y1 + 12, x1 + 116, y1 + 126))

            draw.text((x1 + 130, y1 + 37), short, font=cls._font(26, True), fill=cls.WHITE)
            count = _to_int(data["raid_counts"].get(short))
            raid_word = "raid" if count == 1 else "raids"
            count_text, count_font = cls._fit_text(draw, f"{count:,} {raid_word}", cell_w - 146, 22, 16)
            draw.text((x1 + 130, y1 + 75), count_text, font=count_font, fill=(172, 193, 239, 255))

        x1 = x_positions[1]
        y1 = top_y + 2 * (cell_h + gap_y)
        box = (x1, y1, x1 + cell_w, y1 + cell_h)
        cls._panel(draw, box, fill=(15, 34, 51, 220), outline=(83, 129, 167, 160), radius=15, width=2)
        draw.text((x1 + 28, y1 + 27), "TOTAL RAIDS", font=cls._font(23, True), fill=cls.MUTED)
        total_text = f"{_to_int(data['total_raids']):,}"
        total_font = cls._fit_font(draw, total_text, cell_w - 56, 54, 34)
        draw.text((x1 + 28, y1 + 61), total_text, font=total_font, fill=cls.WHITE)


    @classmethod
    def _draw_professions(cls, canvas: Image.Image, draw: ImageDraw.ImageDraw, data: Dict):
        panel = (1075, 335, 1618, 880)
        cls._panel(draw, panel, fill=cls.PANEL, outline=cls.BORDER, radius=18, width=2)

        title_x = panel[0] + 22
        title_y = panel[1] + 17
        separator_y = panel[1] + 62
        line_x1 = panel[0] + 22
        line_x2 = panel[2] - 21

        draw.text((title_x, title_y), "PROFESSIONS", font=cls._font(34, True), fill=cls.WHITE)
        draw.line((line_x1, separator_y, line_x2, separator_y), fill=cls.SEPARATOR, width=2)

        professions = data.get("professions") or {}
        rows = []
        for key, label, icon in PROFESSIONS:
            stat = professions.get(key)
            if stat is None:
                if key == "gemcraft":
                    continue
                stat = {"level": 0, "xpPercent": 0.0}
            rows.append((key, label, icon, stat))

        if not rows:
            draw.text((panel[0] + 29, 430), "Profession data is hidden", font=cls._font(22, True), fill=cls.MUTED)
            return

        top = 409
        bottom = 865
        row_h = (bottom - top) / len(rows)

        icon_x1 = panel[0] + 22
        name_x = panel[0] + 60
        pct_x = panel[0] + 217
        track_x1, track_x2 = panel[0] + 272, panel[0] + 441
        level_right = panel[2] - 26

        for index, (_, label, icon_file, stat) in enumerate(rows):
            y1 = int(top + index * row_h)
            y2 = int(top + (index + 1) * row_h)
            center_y = (y1 + y2) // 2

            if index:
                draw.line((panel[0] + 21, y1, panel[2] - 21, y1), fill=(64, 92, 116, 105), width=1)

            cls._paste_local_icon(
                canvas,
                icon_file,
                (icon_x1, center_y - 14, icon_x1 + 28, center_y + 14),
                pixel_art=True,
            )

            label_text, label_font = cls._fit_text(draw, label, 146, 18, 14)
            draw.text((name_x, center_y - 11), label_text, font=label_font, fill=cls.WHITE)

            pct = _clamp(_to_float(stat.get("xpPercent")))
            pct_text = f"{int(round(pct))}%"
            pct_font = cls._font(17, True)
            draw.text((pct_x, center_y - 10), pct_text, font=pct_font, fill=cls.MUTED)

            track_y1 = center_y - 6
            track_y2 = center_y + 6
            draw.rounded_rectangle((track_x1, track_y1, track_x2, track_y2), radius=6, fill=cls.TRACK)
            fill_width = int((track_x2 - track_x1) * pct / 100.0)
            if fill_width > 0:
                draw.rounded_rectangle(
                    (track_x1, track_y1, track_x1 + max(fill_width, 8), track_y2),
                    radius=6,
                    fill=cls.ACCENT,
                )

            level = max(0, _to_int(stat.get("level")))
            level_text = f"Lvl {level}"
            level_font = cls._font(17, True)
            level_w = draw.textlength(level_text, font=level_font)
            draw.text((level_right - level_w, center_y - 10), level_text, font=level_font, fill=cls.WHITE)


    @classmethod
    async def generate(cls, card_data: Dict, skin_bytes: Optional[bytes] = None) -> io.BytesIO:
        img = cls._background()
        draw = ImageDraw.Draw(img, "RGBA")

        cls._draw_left_column(img, draw, card_data, skin_bytes)

        x1, x2 = 445, 1618
        gap = 14
        card_w = (x2 - x1 - 2 * gap) // 3
        xs = [x1 + i * (card_w + gap) for i in range(3)]
        top_y, second_y, card_h = 20, 179, 143
        cards = [
            ("WARCOUNT", f"{card_data['wars']:,}", cls.CARD_COLORS[0]),
            ("PLAYTIME", f"{card_data['playtime']:,.0f}h", cls.CARD_COLORS[1]),
            ("TOTAL LEVEL", f"{card_data['total_level']:,}", cls.CARD_COLORS[2]),
            ("QUESTS COMPLETED", f"{card_data['quests']:,}", cls.CARD_COLORS[3]),
            ("DUNGEONS COMPLETED", f"{card_data['dungeons']:,}", cls.CARD_COLORS[4]),
            ("CHESTS OPENED", f"{card_data['chests']:,}", cls.CARD_COLORS[5]),
        ]
        for index, (label, value, color) in enumerate(cards):
            row_y = top_y if index < 3 else second_y
            col = index % 3
            box = (xs[col], row_y, xs[col] + card_w, row_y + card_h)
            cls._draw_metric_card(draw, box, label, value, color)

        cls._draw_raids(img, draw, card_data)
        cls._draw_professions(img, draw, card_data)

        output = io.BytesIO()
        img.convert("RGB").save(output, format="PNG", optimize=True)
        output.seek(0)
        return output


def _safe_filename(name: str) -> str:
    cleaned = "".join(ch for ch in str(name) if ch.isalnum() or ch in ("-", "_"))
    return cleaned or "player"


def setup(bot, has_required_role, config):
    @bot.tree.command(name="player", description="Generate a visual Wynncraft player card")
    @app_commands.describe(username="The Wynncraft player to show")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def playercard(interaction: discord.Interaction, username: str):
        await interaction.response.defer()

        try:
            player_data = await WynncraftAPI.fetch_player(username)
            if not player_data:
                await errors.PLAYER_NOT_FOUND.send(interaction, username=username)
                return

            if player_data.get("multiple"):
                await errors.INVALID_INPUT.send(
                    interaction,
                    reason=f"Multiple players found for `{username}`. Please be more specific.",
                )
                return

            cape_bytes, guild_xp = await asyncio.gather(
                WynncraftAPI.fetch_wynntils_cape(str(player_data.get("uuid") or "")),
                WynncraftAPI.fetch_guild_contribution(player_data),
            )

            skin_bytes = await WynncraftAPI.fetch_skin(
                str(player_data.get("uuid") or ""),
                str(player_data.get("username") or username),
                cape_bytes=cape_bytes,
            )

            card_data = build_card_data(player_data, guild_xp=guild_xp)
            image = await PlayerCardImageGenerator.generate(card_data, skin_bytes=skin_bytes)

            filename = f"playercard-{_safe_filename(card_data['username'])}.png"
            await interaction.followup.send(file=discord.File(image, filename=filename))

        except Exception as exc:
            print(f"[playercard] command failed: {exc}")
            await errors.IMAGE_GENERATION_FAILED.send(
                interaction,
                reason="Something went wrong while generating the player card.",
            )

    print("[OK] Loaded playercard command")
