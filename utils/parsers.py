import re

# ---------------------------------------------------------------------------
# Health: integer with optional K/M/B/T suffix (e.g. "20M", "100K", "1.5M")
# ---------------------------------------------------------------------------

_HEALTH_SUFFIXES = {
    "K": 1_000,
    "M": 1_000_000,
    "B": 1_000_000_000,
    "T": 1_000_000_000_000,
}


def parse_health(value) -> int:
    """Parse a health value such as ``"20M"``, ``"100K"``, ``"1.5M"`` or ``"5000"``.

    Returns the value as an integer.
    """
    if value is None:
        raise ValueError("Health value is required")
    s = str(value).strip().replace(",", "").replace(" ", "")
    if not s:
        raise ValueError("Health value cannot be empty")

    multiplier = 1
    last = s[-1].upper()
    if last in _HEALTH_SUFFIXES:
        multiplier = _HEALTH_SUFFIXES[last]
        s = s[:-1]

    try:
        num = float(s)
    except ValueError:
        raise ValueError(f"Invalid health value: {value!r}")

    if num < 0:
        raise ValueError(f"Health cannot be negative: {value!r}")

    return int(round(num * multiplier))


def format_health(hp: int) -> str:
    """Format an integer health value back into a short human-readable string."""
    hp = int(hp)
    if hp >= 1_000_000_000_000:
        return f"{hp / 1_000_000_000_000:g}T"
    if hp >= 1_000_000_000:
        return f"{hp / 1_000_000_000:g}B"
    if hp >= 1_000_000:
        return f"{hp / 1_000_000:g}M"
    if hp >= 1_000:
        return f"{hp / 1_000:g}K"
    return str(hp)


# ---------------------------------------------------------------------------
# Defense: percentage (e.g. "50%", "50", "37.5")
# ---------------------------------------------------------------------------


def parse_defense(value) -> float:
    """Parse a defense percentage like ``"50%"`` or ``"50"`` into a float."""
    if value is None:
        raise ValueError("Defense value is required")
    s = str(value).strip().replace(" ", "")
    if not s:
        raise ValueError("Defense value cannot be empty")
    if s.endswith("%"):
        s = s[:-1]
    try:
        pct = float(s)
    except ValueError:
        raise ValueError(f"Invalid defense value: {value!r}")
    return pct


def format_defense(pct: float) -> str:
    """Format a percentage as a short string with no trailing zeros."""
    return f"{float(pct):g}%"


# ---------------------------------------------------------------------------
# Duration: seconds with flexible spelling
#   e.g. "4m20s", "410s", "5 minutes", "10 min", "5min", "1h30m", "410"
# ---------------------------------------------------------------------------

_DURATION_UNITS = {
    "ms": 0.001, "milli": 0.001, "millis": 0.001,
    "millisecond": 0.001, "milliseconds": 0.001,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

# A single pair. Longer unit names must come first
_DURATION_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(milliseconds|millisecond|millis|milli|ms|"
    r"seconds|second|secs|sec|s|"
    r"minutes|minute|mins|min|m|"
    r"hours|hour|hrs|hr|h|"
    r"days|day|d)",
    re.IGNORECASE,
)


def parse_duration(value) -> int:
    """Parse a duration string and return the total number of whole seconds.

    Accepts a wide range of formats including ``"4m20s"``, ``"410s"``,
    ``"5 minutes"``, ``"10 min"``, ``"5min"``, ``"1h30m"``, and a plain
    number which is interpreted as seconds.
    """
    if value is None:
        raise ValueError("Duration value is required")
    s = str(value).strip().lower()
    if not s:
        raise ValueError("Duration value cannot be empty")

    # Plain number -> seconds.
    try:
        return int(round(float(s)))
    except ValueError:
        pass

    matches = list(_DURATION_PATTERN.finditer(s))
    if not matches:
        raise ValueError(f"Invalid duration: {value!r}")

    # Make sure every non-whitespace character was part of a matched token
    pos = 0
    for m in matches:
        if s[pos:m.start()].strip():
            raise ValueError(f"Invalid duration: {value!r}")
        pos = m.end()
    if s[pos:].strip():
        raise ValueError(f"Invalid duration: {value!r}")

    total_seconds = 0.0
    for m in matches:
        num = float(m.group(1))
        unit = m.group(2).lower()
        if unit not in _DURATION_UNITS:
            raise ValueError(f"Unknown duration unit: {m.group(2)!r}")
        total_seconds += num * _DURATION_UNITS[unit]

    if total_seconds < 0:
        raise ValueError(f"Duration cannot be negative: {value!r}")

    return int(round(total_seconds))


def format_duration(seconds: int) -> str:
    """Format a number of seconds back into a compact ``1h 2m 3s`` string."""
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)
