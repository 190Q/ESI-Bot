import asyncio
import base64
import io
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

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
    "User-Agent": "Wynntils Artemis SusCard/1.0",
    "Accept": "application/json",
}
NMSR_HEADERS = {"User-Agent": "Wynncraft-SusCard/1.0"}
MOJANG_HEADERS = {"User-Agent": "Wynncraft-SusCard/1.0", "Accept": "application/json"}
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
BACKGROUND_PATH = IMAGES_DIR / "background.webp"
PREFERRED_IMAGE_EXTENSIONS = (".webp", ".png")

def _resolve_image_path(base_dir: Path, filename: str, *, preferred_exts=PREFERRED_IMAGE_EXTENSIONS) -> Optional[Path]:
    """Resolve an image path, preferring WebP and falling back to PNG."""
    if not filename:
        return None

    base_dir = Path(base_dir)
    raw = Path(filename)
    candidates = []

    def _add(path: Path):
        if path not in candidates:
            candidates.append(path)

    if raw.is_absolute():
        _add(raw)
        stem = raw.stem
        for ext in preferred_exts:
            _add(raw.with_name(stem + ext))
    else:
        _add(base_dir / raw.name)
        stem = raw.stem if raw.suffix else raw.name
        for ext in preferred_exts:
            _add(base_dir / f"{stem}{ext}")

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
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
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=cls.headers()) as response:
                    if response.status == 200:
                        return await response.json()
                    if response.status == 300:
                        return {"multiple": True, "players": await response.json()}
                    return None
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            print(f"[suscard] player fetch failed: {exc}")
            return None

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
                        raw_skin, _ = await cls._fetch_bytes_with_retry(session, f"{NMSR_API_BASE}/skin/{identifier}")
                        if not raw_skin:
                            continue
                        rendered = await cls._render_with_cape(session, raw_skin, cape_bytes, player_model)
                        if rendered:
                            return rendered

            for identifier in identifiers:
                rendered, is_fallback = await cls._fetch_bytes_with_retry(session, f"{NMSR_API_BASE}/bodybust/{identifier}")
                if rendered and not is_fallback:
                    return rendered
                fallback_render = fallback_render or rendered

        return fallback_render

    @classmethod
    async def fetch_guild_history(cls, player_data: Dict) -> List[str]:
        guild_ids = player_data.get("guildHistory") or []
        names: List[str] = []
        seen_names = set()

        if guild_ids:
            url = f"{WYNNCRAFT_API_BASE}/guild/list/guild?identifier=uuid"
            timeout = aiohttp.ClientTimeout(total=15)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=cls.headers()) as response:
                        if response.status == 200:
                            directory = await response.json()
                            for guild_uuid in dict.fromkeys(guild_ids):
                                guild = directory.get(guild_uuid) or {}
                                name = guild.get("name")
                                if name and name.casefold() not in seen_names:
                                    seen_names.add(name.casefold())
                                    names.append(name)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                pass

        current_guild = (player_data.get("guild") or {}).get("name")
        if current_guild and current_guild.casefold() not in seen_names:
            names.append(current_guild)

        return names[-4:]


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def _format_percent(value: float) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0

    if not math.isfinite(number):
        number = 0.0

    floored = math.floor(number * 100.0 + 1e-9) / 100.0
    if floored == 0:
        floored = 0.0

    text = f"{floored:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _sigmoid(value: float) -> float:
    try:
        return 100.0 / (1.0 + math.exp(-0.1 * (value - 50.0)))
    except OverflowError:
        return 0.0 if value < 0 else 100.0


def calculate_suspiciousness(player_data: Dict) -> Optional[Dict]:
    support_ranks = ["vip", "vipplus", "hero", "heroplus", "champion"]

    try:
        first_join = player_data.get("firstJoin")
        playtime = float(player_data.get("playtime") or 0)
        global_data = player_data.get("globalData") or {}
        total_level = int(global_data.get("totalLevel") or 0)
        completed_quests = int(global_data.get("completedQuests") or 0)
        support_rank = player_data.get("supportRank")
        veteran = bool(player_data.get("veteran"))

        total_raids = 0
        for character in (player_data.get("characters") or {}).values():
            if isinstance(character, dict):
                raids = character.get("raids") or {}
                if isinstance(raids, dict):
                    total_raids += int(raids.get("total") or 0)

        if first_join:
            join_dt = datetime.fromisoformat(first_join.replace("Z", "+00:00"))
            account_age_seconds = (datetime.now(timezone.utc) - join_dt).total_seconds()
            two_years = 63_072_000
            join_sus = _clamp(max(0.0, two_years - account_age_seconds) * 100.0 / two_years)
            days_since_join = max(1, (datetime.now(timezone.utc) - join_dt).days)
            time_spent_percentage = playtime / (days_since_join * 24.0) * 100.0
        else:
            join_dt = None
            join_sus = 50.0
            time_spent_percentage = 100.0

        playtime_sus = _clamp(max(0.0, 800.0 - playtime) * 100.0 / 800.0)
        total_level_sus = _clamp(max(0.0, 250.0 - total_level) * 100.0 / 250.0)
        quests_sus = _clamp(max(0.0, 150.0 - completed_quests) * 100.0 / 150.0)
        time_spent_sus = _clamp(_sigmoid(time_spent_percentage))

        if support_rank in support_ranks:
            rank_index = support_ranks.index(support_rank)
            rank_sus = _clamp(max(0.0, (2 - rank_index) * 50.0))
        else:
            rank_sus = 100.0

        raids_sus = 0.0 if total_raids >= 50 else _clamp((50 - total_raids) * 2.0)

        metrics = [
            join_sus,
            playtime_sus,
            time_spent_sus,
            total_level_sus,
            quests_sus,
            rank_sus,
            raids_sus,
        ]
        overall_sus = sum(metrics) / len(metrics)

        rank_display = "No rank"
        if support_rank:
            rank_display = {
                "vipplus": "VIP+",
                "heroplus": "HERO+",
            }.get(support_rank, str(support_rank).upper())
        if veteran:
            rank_display += " (VET)"

        guild = player_data.get("guild") or {}
        guild_name = guild.get("name") or "None"
        raw_guild_rank = guild.get("rank") or "None"
        guild_rank = str(raw_guild_rank).replace("_", " ").title()

        return {
            "username": player_data.get("username") or "Unknown",
            "uuid": player_data.get("uuid") or "",
            "overall_sus": overall_sus,
            "join_date": join_dt.strftime("%Y-%m-%d") if join_dt else "Unknown",
            "join_sus": join_sus,
            "playtime": playtime,
            "playtime_sus": playtime_sus,
            "time_spent_percentage": time_spent_percentage,
            "time_spent_sus": time_spent_sus,
            "total_level": total_level,
            "total_level_sus": total_level_sus,
            "quests": completed_quests,
            "quests_sus": quests_sus,
            "rank": rank_display,
            "rank_sus": rank_sus,
            "raids": total_raids,
            "raids_sus": raids_sus,
            "guild_name": guild_name,
            "guild_rank": guild_rank,
        }
    except (TypeError, ValueError, KeyError) as exc:
        print(f"[suscard] calculation failed: {exc}")
        return None


class SusCardImageGenerator:
    WIDTH = 1650
    HEIGHT = 900

    _BACKGROUND_CACHE: Optional[Image.Image] = None

    PANEL = (13, 25, 37, 220)
    PANEL_SOFT = (15, 28, 42, 205)
    BORDER = (111, 129, 145, 175)
    TRACK = (38, 55, 75, 235)
    WHITE = (245, 247, 250, 255)
    MUTED = (160, 171, 198, 255)

    COLORS = {
        "join": (255, 83, 96, 255),
        "playtime": (0, 235, 104, 255),
        "time": (255, 211, 0, 255),
        "level": (0, 174, 239, 255),
        "quests": (0, 227, 191, 255),
        "rank": (231, 0, 241, 255),
        "raids": (255, 77, 91, 255),
    }

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
        min_size: int = 18,
        *,
        bold: bool = True,
    ):
        size = start_size
        while size >= min_size:
            font = cls._font(size, bold=bold)
            if draw.textlength(text, font=font) <= max_width:
                return font
            size -= 2
        return cls._font(min_size, bold=bold)

    @classmethod
    def _fit_text(
        cls,
        draw: ImageDraw.ImageDraw,
        text: str,
        max_width: int,
        start_size: int,
        min_size: int = 18,
        *,
        bold: bool = True,
    ):
        text = str(text)
        font = cls._fit_font(
            draw,
            text,
            max_width,
            start_size,
            min_size,
            bold=bold,
        )
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

        fitted = text[:low].rstrip() + ellipsis if low else ellipsis
        return fitted, font

    @staticmethod
    def _panel(draw: ImageDraw.ImageDraw, box, *, fill, outline=None, radius=18, width=2):
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)

    @classmethod
    def _background(cls) -> Image.Image:
        if cls._BACKGROUND_CACHE is None:
            path = _resolve_image_path(IMAGES_DIR, "background")
            try:
                if path:
                    background = Image.open(path).convert("RGB")
                    background = ImageOps.fit(background, (cls.WIDTH, cls.HEIGHT), method=Image.Resampling.LANCZOS)
                    background = background.filter(ImageFilter.GaussianBlur(2.2)).convert("RGBA")
                else:
                    raise OSError("background image not found")
            except (OSError, ValueError):
                background = Image.new("RGBA", (cls.WIDTH, cls.HEIGHT), (20, 30, 42, 255))

            dark = Image.new("RGBA", background.size, (5, 14, 24, 150))
            cls._BACKGROUND_CACHE = Image.alpha_composite(background, dark)

        return cls._BACKGROUND_CACHE.copy()

    @classmethod
    def _draw_metric_card(
        cls,
        draw: ImageDraw.ImageDraw,
        box,
        label: str,
        value: str,
        percentage: float,
        color,
    ) -> None:
        x1, y1, x2, y2 = box
        cls._panel(draw, box, fill=cls.PANEL, outline=cls.BORDER, radius=17, width=2)
        draw.rounded_rectangle((x1, y1, x1 + 18, y2), radius=16, fill=color)
        draw.rectangle((x1 + 10, y1 + 1, x1 + 18, y2 - 1), fill=color)

        content_width = x2 - x1 - 70
        label_text, label_font = cls._fit_text(draw, label.upper(), content_width, 25, 20)
        draw.text((x1 + 42, y1 + 19), label_text, font=label_font, fill=cls.MUTED)

        value_text, value_font = cls._fit_text(draw, value, content_width, 54, 30)
        draw.text((x1 + 42, y1 + 54), value_text, font=value_font, fill=cls.WHITE)

        pct_text, pct_font = cls._fit_text(draw, _format_percent(percentage), content_width, 28, 20)
        draw.text((x1 + 42, y1 + 116), pct_text, font=pct_font, fill=color)

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
            pass

    @classmethod
    def _rank_color(cls, rank: str):
        clean = rank.lower().replace(" (vet)", "")
        return {
            "vip": (85, 255, 85, 255),
            "vip+": (85, 85, 255, 255),
            "hero": (216, 0, 225, 255),
            "hero+": (244, 0, 255, 255),
            "champion": (255, 170, 0, 255),
        }.get(clean, (220, 225, 232, 255))

    @classmethod
    async def generate(
        cls,
        sus_data: Dict,
        skin_bytes: Optional[bytes] = None,
        guild_history_names: Optional[Iterable[str]] = None,
    ) -> io.BytesIO:
        img = cls._background()
        draw = ImageDraw.Draw(img, "RGBA")

        # Left player column.
        cls._panel(draw, (46, 22, 432, 878), fill=cls.PANEL, outline=cls.BORDER, radius=18, width=2)
        cls._paste_skin(img, skin_bytes, (73, 42, 405, 398))

        username = str(sus_data["username"])
        username_text, username_font = cls._fit_text(draw, username, 330, 68, 34)
        username_width = draw.textlength(username_text, font=username_font)
        draw.text((239 - username_width / 2, 424), username_text, font=username_font, fill=cls.WHITE)

        rank = str(sus_data["rank"])
        rank_text, rank_font = cls._fit_text(draw, rank, 330, 34, 24)
        rank_width = draw.textlength(rank_text, font=rank_font)
        draw.text((239 - rank_width / 2, 502), rank_text, font=rank_font, fill=cls._rank_color(rank))

        guild_box = (63, 564, 415, 710)
        cls._panel(draw, guild_box, fill=cls.PANEL_SOFT, outline=(88, 105, 122, 130), radius=18, width=2)
        draw.text((84, 585), "GUILD", font=cls._font(24, True), fill=cls.MUTED)
        guild_text, guild_font = cls._fit_text(draw, str(sus_data["guild_name"]), 305, 37, 22)
        draw.text((84, 618), guild_text, font=guild_font, fill=cls.WHITE)
        guild_rank_text, guild_rank_font = cls._fit_text(
            draw,
            f"Rank: {sus_data['guild_rank']}",
            305,
            24,
            18,
        )
        draw.text((84, 667), guild_rank_text, font=guild_rank_font, fill=(132, 143, 180, 255))

        suspicion_box = (63, 726, 415, 862)
        cls._panel(draw, suspicion_box, fill=cls.PANEL_SOFT, outline=(88, 105, 122, 130), radius=18, width=2)
        draw.rounded_rectangle((63, 726, 82, 862), radius=16, fill=cls.COLORS["join"])
        draw.rectangle((73, 727, 82, 861), fill=cls.COLORS["join"])
        draw.text((104, 749), "OVERALL SUSPICION", font=cls._font(22, True), fill=cls.MUTED)
        overall_text, overall_font = cls._fit_text(
            draw,
            _format_percent(sus_data["overall_sus"]),
            285,
            62,
            36,
        )
        draw.text((104, 785), overall_text, font=overall_font, fill=cls.COLORS["join"])

        # Six headline metric cards.
        area_x1, area_x2 = 449, 1607
        gap = 16
        card_w = (area_x2 - area_x1 - gap * 2) // 3
        x_positions = [area_x1 + i * (card_w + gap) for i in range(3)]
        top_y, second_y, card_h = 22, 190, 155

        cards = [
            ("JOIN DATE", sus_data["join_date"], sus_data["join_sus"], cls.COLORS["join"]),
            ("PLAYTIME", f"{sus_data['playtime']:.0f}h", sus_data["playtime_sus"], cls.COLORS["playtime"]),
            ("TIME SPENT", _format_percent(sus_data["time_spent_percentage"]), sus_data["time_spent_sus"], cls.COLORS["time"]),
            ("TOTAL LEVEL", f"{sus_data['total_level']:,}", sus_data["total_level_sus"], cls.COLORS["level"]),
            ("QUESTS", str(sus_data["quests"]), sus_data["quests_sus"], cls.COLORS["quests"]),
            ("RANK", rank, sus_data["rank_sus"], cls.COLORS["rank"]),
        ]

        for index, card in enumerate(cards):
            row_y = top_y if index < 3 else second_y
            col = index % 3
            box = (x_positions[col], row_y, x_positions[col] + card_w, row_y + card_h)
            cls._draw_metric_card(draw, box, *card)

        # Player analysis panel with the 15%-cap visual scale used by the reference card.
        analysis = (449, 363, 1607, 875)
        cls._panel(draw, analysis, fill=cls.PANEL, outline=cls.BORDER, radius=17, width=2)
        draw.text((476, 383), "PLAYER ANALYSIS", font=cls._font(34, True), fill=cls.WHITE)
        draw.line((476, 430, 1580, 430), fill=(160, 174, 190, 210), width=2)

        rows = [
            ("Join date", sus_data["join_sus"], cls.COLORS["join"]),
            ("Playtime", sus_data["playtime_sus"], cls.COLORS["playtime"]),
            ("Time spent", sus_data["time_spent_sus"], cls.COLORS["time"]),
            ("Total level", sus_data["total_level_sus"], cls.COLORS["level"]),
            ("Quests", sus_data["quests_sus"], cls.COLORS["quests"]),
            ("Rank", sus_data["rank_sus"], cls.COLORS["rank"]),
            ("Raids", sus_data["raids_sus"], cls.COLORS["raids"]),
        ]

        label_x = 484
        track_x1, track_x2 = 684, 1464
        pct_x = 1496
        row_y = 456
        row_gap = 39
        track_h = 21

        for label, percentage, color in rows:
            draw.text((label_x, row_y - 2), label, font=cls._font(27, True), fill=cls.WHITE)
            draw.rounded_rectangle((track_x1, row_y + 2, track_x2, row_y + 2 + track_h), radius=5, fill=cls.TRACK)

            visual_ratio = _clamp(float(percentage), 0.0, 15.0) / 15.0
            fill_width = int((track_x2 - track_x1) * visual_ratio)
            if fill_width > 0:
                draw.rounded_rectangle(
                    (track_x1, row_y + 2, track_x1 + max(fill_width, 8), row_y + 2 + track_h),
                    radius=5,
                    fill=color,
                )

            pct_text, pct_font = cls._fit_text(draw, _format_percent(percentage), 96, 27, 20)
            pct_width = draw.textlength(pct_text, font=pct_font)
            draw.text((pct_x + 80 - pct_width, row_y - 2), pct_text, font=pct_font, fill=cls.WHITE)
            row_y += row_gap

        draw.line((476, 749, 1580, 749), fill=(144, 158, 174, 190), width=1)
        draw.text((476, 774), "GUILD HISTORY", font=cls._font(21, True), fill=cls.WHITE)

        history = [str(name).strip() for name in (guild_history_names or []) if str(name).strip()]
        history_text = "  •  ".join(history[-4:]) if history else "No public guild history"
        history_text, history_font = cls._fit_text(draw, history_text, 1080, 28, 18)
        draw.text((476, 812), history_text, font=history_font, fill=(175, 185, 202, 255))

        output = io.BytesIO()
        img.convert("RGB").save(output, format="PNG", optimize=True)
        output.seek(0)
        return output


def _safe_filename(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_"))
    return cleaned or "player"


def setup(bot, has_required_role, config):
    # Keep this signature because the bot loader supplies all three arguments.
    @bot.tree.command(name="sus", description="Generate a visual sus card for a player")
    @app_commands.describe(username="The Wynncraft player to check")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def suscard(interaction: discord.Interaction, username: str):
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

            sus_data = calculate_suspiciousness(player_data)
            if not sus_data:
                await errors.IMAGE_GENERATION_FAILED.send(
                    interaction,
                    reason="Could not calculate suspiciousness for this player.",
                )
                return

            cape_bytes, guild_history = await asyncio.gather(
                WynncraftAPI.fetch_wynntils_cape(sus_data["uuid"]),
                WynncraftAPI.fetch_guild_history(player_data),
            )
            skin_bytes = await WynncraftAPI.fetch_skin(
                sus_data["uuid"],
                sus_data["username"],
                cape_bytes=cape_bytes,
            )
            image = await SusCardImageGenerator.generate(
                sus_data,
                skin_bytes=skin_bytes,
                guild_history_names=guild_history,
            )

            # Success sends only the generated PNG attachment: no embed, chart image, or extra message.
            filename = f"suscard-{_safe_filename(sus_data['username'])}.png"
            await interaction.followup.send(file=discord.File(image, filename=filename))

        except Exception as exc:
            print(f"[suscard] command failed: {exc}")
            await errors.IMAGE_GENERATION_FAILED.send(
                interaction,
                reason="Something went wrong while generating the sus card.",
            )

    print("[OK] Loaded suscard command")
