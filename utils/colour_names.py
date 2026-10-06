from __future__ import annotations

from typing import Dict, Optional, Tuple

# Display name -> ``#rrggbb``. Covers every CSS colour keyword (both the ``gray``
# and ``grey`` spellings) plus X11's ``Light Goldenrod``, which CSS never adopted.
CSS3_NAMES_TO_HEX: Dict[str, str] = {
    "Alice Blue": "#f0f8ff",
    "Antique White": "#faebd7",
    "Aqua": "#00ffff",
    "Aquamarine": "#7fffd4",
    "Azure": "#f0ffff",
    "Beige": "#f5f5dc",
    "Bisque": "#ffe4c4",
    "Black": "#000000",
    "Blanched Almond": "#ffebcd",
    "Blue": "#0000ff",
    "Blue Violet": "#8a2be2",
    "Brown": "#a52a2a",
    "Burly Wood": "#deb887",
    "Cadet Blue": "#5f9ea0",
    "Chartreuse": "#7fff00",
    "Chocolate": "#d2691e",
    "Coral": "#ff7f50",
    "Cornflower Blue": "#6495ed",
    "Cornsilk": "#fff8dc",
    "Crimson": "#dc143c",
    "Cyan": "#00ffff",
    "Dark Blue": "#00008b",
    "Dark Cyan": "#008b8b",
    "Dark Goldenrod": "#b8860b",
    "Dark Gray": "#a9a9a9",
    "Dark Green": "#006400",
    "Dark Grey": "#a9a9a9",
    "Dark Khaki": "#bdb76b",
    "Dark Magenta": "#8b008b",
    "Dark Olive Green": "#556b2f",
    "Dark Orange": "#ff8c00",
    "Dark Orchid": "#9932cc",
    "Dark Red": "#8b0000",
    "Dark Salmon": "#e9967a",
    "Dark Sea Green": "#8fbc8f",
    "Dark Slate Blue": "#483d8b",
    "Dark Slate Gray": "#2f4f4f",
    "Dark Slate Grey": "#2f4f4f",
    "Dark Turquoise": "#00ced1",
    "Dark Violet": "#9400d3",
    "Deep Pink": "#ff1493",
    "Deep Sky Blue": "#00bfff",
    "Dim Gray": "#696969",
    "Dim Grey": "#696969",
    "Dodger Blue": "#1e90ff",
    "Fire Brick": "#b22222",
    "Floral White": "#fffaf0",
    "Forest Green": "#228b22",
    "Fuchsia": "#ff00ff",
    "Gainsboro": "#dcdcdc",
    "Ghost White": "#f8f8ff",
    "Gold": "#ffd700",
    "Goldenrod": "#daa520",
    "Gray": "#808080",
    "Green": "#008000",
    "Green Yellow": "#adff2f",
    "Grey": "#808080",
    "Honey Dew": "#f0fff0",
    "Hot Pink": "#ff69b4",
    "Indian Red": "#cd5c5c",
    "Indigo": "#4b0082",
    "Ivory": "#fffff0",
    "Khaki": "#f0e68c",
    "Lavender": "#e6e6fa",
    "Lavender Blush": "#fff0f5",
    "Lawn Green": "#7cfc00",
    "Lemon Chiffon": "#fffacd",
    "Light Blue": "#add8e6",
    "Light Coral": "#f08080",
    "Light Cyan": "#e0ffff",
    "Light Goldenrod": "#eedd82",
    "Light Goldenrod Yellow": "#fafad2",
    "Light Gray": "#d3d3d3",
    "Light Green": "#90ee90",
    "Light Grey": "#d3d3d3",
    "Light Pink": "#ffb6c1",
    "Light Salmon": "#ffa07a",
    "Light Sea Green": "#20b2aa",
    "Light Sky Blue": "#87cefa",
    "Light Slate Gray": "#778899",
    "Light Slate Grey": "#778899",
    "Light Steel Blue": "#b0c4de",
    "Light Yellow": "#ffffe0",
    "Lime": "#00ff00",
    "Lime Green": "#32cd32",
    "Linen": "#faf0e6",
    "Magenta": "#ff00ff",
    "Maroon": "#800000",
    "Medium Aquamarine": "#66cdaa",
    "Medium Blue": "#0000cd",
    "Medium Orchid": "#ba55d3",
    "Medium Purple": "#9370db",
    "Medium Sea Green": "#3cb371",
    "Medium Slate Blue": "#7b68ee",
    "Medium Spring Green": "#00fa9a",
    "Medium Turquoise": "#48d1cc",
    "Medium Violet Red": "#c71585",
    "Midnight Blue": "#191970",
    "Mint Cream": "#f5fffa",
    "Misty Rose": "#ffe4e1",
    "Moccasin": "#ffe4b5",
    "Navajo White": "#ffdead",
    "Navy": "#000080",
    "Old Lace": "#fdf5e6",
    "Olive": "#808000",
    "Olive Drab": "#6b8e23",
    "Orange": "#ffa500",
    "Orange Red": "#ff4500",
    "Orchid": "#da70d6",
    "Pale Goldenrod": "#eee8aa",
    "Pale Green": "#98fb98",
    "Pale Turquoise": "#afeeee",
    "Pale Violet Red": "#db7093",
    "Papaya Whip": "#ffefd5",
    "Peach Puff": "#ffdab9",
    "Peru": "#cd853f",
    "Pink": "#ffc0cb",
    "Plum": "#dda0dd",
    "Powder Blue": "#b0e0e6",
    "Purple": "#800080",
    "Rebecca Purple": "#663399",
    "Red": "#ff0000",
    "Rosy Brown": "#bc8f8f",
    "Royal Blue": "#4169e1",
    "Saddle Brown": "#8b4513",
    "Salmon": "#fa8072",
    "Sandy Brown": "#f4a460",
    "Sea Green": "#2e8b57",
    "Seashell": "#fff5ee",
    "Sienna": "#a0522d",
    "Silver": "#c0c0c0",
    "Sky Blue": "#87ceeb",
    "Slate Blue": "#6a5acd",
    "Slate Gray": "#708090",
    "Slate Grey": "#708090",
    "Snow": "#fffafa",
    "Spring Green": "#00ff7f",
    "Steel Blue": "#4682b4",
    "Tan": "#d2b48c",
    "Teal": "#008080",
    "Thistle": "#d8bfd8",
    "Tomato": "#ff6347",
    "Turquoise": "#40e0d0",
    "Violet": "#ee82ee",
    "Wheat": "#f5deb3",
    "White": "#ffffff",
    "White Smoke": "#f5f5f5",
    "Yellow": "#ffff00",
    "Yellow Green": "#9acd32",
}

SIMPLE_COLOURS: Tuple[str, ...] = (
    "black",
    "white",
    "grey",
    "red",
    "pink",
    "orange",
    "brown",
    "yellow",
    "green",
    "cyan",
    "blue",
    "purple",
)

NAME_TO_SIMPLE_COLOUR: Dict[str, str] = {
    # Neutrals
    "Black": "black",
    "White": "white",
    "Snow": "white",
    "Ghost White": "white",
    "White Smoke": "white",
    "Floral White": "white",
    "Ivory": "white",
    "Seashell": "white",
    "Old Lace": "white",
    "Linen": "white",
    "Antique White": "white",
    "Beige": "white",
    "Cornsilk": "white",
    "Honey Dew": "white",
    "Mint Cream": "white",
    "Alice Blue": "white",
    "Azure": "white",
    "Blanched Almond": "white",
    "Papaya Whip": "white",
    "Gainsboro": "grey",
    "Light Gray": "grey",
    "Light Grey": "grey",
    "Silver": "grey",
    "Dark Gray": "grey",
    "Dark Grey": "grey",
    "Gray": "grey",
    "Grey": "grey",
    "Dim Gray": "grey",
    "Dim Grey": "grey",
    "Slate Gray": "grey",
    "Slate Grey": "grey",
    "Light Slate Gray": "grey",
    "Light Slate Grey": "grey",
    "Dark Slate Gray": "grey",
    "Dark Slate Grey": "grey",
    # Reds, pinks and browns
    "Red": "red",
    "Dark Red": "red",
    "Maroon": "red",
    "Fire Brick": "red",
    "Crimson": "red",
    "Indian Red": "red",
    "Light Coral": "red",
    "Salmon": "red",
    "Dark Salmon": "red",
    "Light Salmon": "red",
    "Tomato": "red",
    "Orange Red": "red",
    "Pink": "pink",
    "Light Pink": "pink",
    "Hot Pink": "pink",
    "Deep Pink": "pink",
    "Lavender Blush": "pink",
    "Misty Rose": "pink",
    "Pale Violet Red": "pink",
    "Medium Violet Red": "pink",
    "Rosy Brown": "pink",
    "Brown": "brown",
    "Saddle Brown": "brown",
    "Sandy Brown": "brown",
    "Sienna": "brown",
    "Peru": "brown",
    "Chocolate": "brown",
    "Tan": "brown",
    "Burly Wood": "brown",
    "Orange": "orange",
    "Dark Orange": "orange",
    "Coral": "orange",
    "Bisque": "orange",
    "Moccasin": "orange",
    "Navajo White": "orange",
    "Peach Puff": "orange",
    # Yellows
    "Yellow": "yellow",
    "Light Yellow": "yellow",
    "Lemon Chiffon": "yellow",
    "Light Goldenrod Yellow": "yellow",
    "Light Goldenrod": "yellow",
    "Pale Goldenrod": "yellow",
    "Khaki": "yellow",
    "Dark Khaki": "yellow",
    "Wheat": "yellow",
    "Gold": "yellow",
    "Goldenrod": "yellow",
    "Dark Goldenrod": "yellow",
    "Olive": "yellow",
    # Greens
    "Green": "green",
    "Dark Green": "green",
    "Forest Green": "green",
    "Lime": "green",
    "Lime Green": "green",
    "Lawn Green": "green",
    "Chartreuse": "green",
    "Green Yellow": "green",
    "Yellow Green": "green",
    "Spring Green": "green",
    "Medium Spring Green": "green",
    "Pale Green": "green",
    "Light Green": "green",
    "Sea Green": "green",
    "Medium Sea Green": "green",
    "Dark Sea Green": "green",
    "Olive Drab": "green",
    "Dark Olive Green": "green",
    # Cyans
    "Cyan": "cyan",
    "Aqua": "cyan",
    "Teal": "cyan",
    "Dark Cyan": "cyan",
    "Light Cyan": "cyan",
    "Turquoise": "cyan",
    "Medium Turquoise": "cyan",
    "Dark Turquoise": "cyan",
    "Pale Turquoise": "cyan",
    "Aquamarine": "cyan",
    "Medium Aquamarine": "cyan",
    "Light Sea Green": "cyan",
    "Cadet Blue": "cyan",
    "Powder Blue": "cyan",
    # Blues
    "Blue": "blue",
    "Medium Blue": "blue",
    "Dark Blue": "blue",
    "Navy": "blue",
    "Midnight Blue": "blue",
    "Royal Blue": "blue",
    "Dodger Blue": "blue",
    "Cornflower Blue": "blue",
    "Deep Sky Blue": "blue",
    "Sky Blue": "blue",
    "Light Sky Blue": "blue",
    "Light Blue": "blue",
    "Light Steel Blue": "blue",
    "Steel Blue": "blue",
    # Purples
    "Purple": "purple",
    "Rebecca Purple": "purple",
    "Violet": "purple",
    "Orchid": "purple",
    "Plum": "purple",
    "Thistle": "purple",
    "Lavender": "purple",
    "Magenta": "purple",
    "Fuchsia": "purple",
    "Medium Orchid": "purple",
    "Dark Orchid": "purple",
    "Dark Violet": "purple",
    "Blue Violet": "purple",
    "Dark Magenta": "purple",
    "Medium Purple": "purple",
    "Slate Blue": "purple",
    "Dark Slate Blue": "purple",
    "Medium Slate Blue": "purple",
    "Indigo": "purple",
}

# Simple colour -> the names that read as that colour.
SIMPLE_COLOUR_CATEGORIES: Dict[str, Tuple[str, ...]] = {
    simple: tuple(sorted(name for name, bucket in NAME_TO_SIMPLE_COLOUR.items() if bucket == simple))
    for simple in SIMPLE_COLOURS
}

_SIMPLE_COLOUR_ALIASES: Dict[str, str] = {
    "gray": "grey",
}

_SIMPLE_COLOUR_SET = frozenset(SIMPLE_COLOURS)


def _hex_to_rgb(value: int) -> Tuple[int, int, int]:
    """Split a ``0xRRGGBB`` value into ``(red, green, blue)``."""
    value = int(value) & 0xFFFFFF
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def _redmean_distance(
    red: int,
    green: int,
    blue: int,
    other_red: int,
    other_green: int,
    other_blue: int,
) -> float:
    """Weighted RGB distance, a cheap stand-in for perceptual difference."""
    red_mean = (red + other_red) / 2
    delta_red = red - other_red
    delta_green = green - other_green
    delta_blue = blue - other_blue
    return (
        (2 + red_mean / 256) * delta_red * delta_red
        + 4 * delta_green * delta_green
        + (2 + (255 - red_mean) / 256) * delta_blue * delta_blue
    )


_HEX_TO_NAME: Dict[int, str] = {}
for _name, _value in CSS3_NAMES_TO_HEX.items():
    _HEX_TO_NAME.setdefault(int(_value.lstrip("#"), 16), _name)

_NAMED_RGB: Tuple[Tuple[str, int, int, int], ...] = tuple(
    (name, *_hex_to_rgb(int(value.lstrip("#"), 16)))
    for name, value in CSS3_NAMES_TO_HEX.items()
)


def normalise_simple_colour(raw: object) -> Optional[str]:
    """Return the canonical simple colour for *raw*, or ``None`` if unknown."""
    key = str(raw or "").strip().casefold()
    key = _SIMPLE_COLOUR_ALIASES.get(key, key)
    return key if key in _SIMPLE_COLOUR_SET else None


def is_simple_colour(raw: object) -> bool:
    """Whether *raw* names a simple colour this module knows about."""
    return normalise_simple_colour(raw) is not None


def colour_name(value: int) -> Optional[str]:
    """The named colour for an exact ``0xRRGGBB`` match, otherwise ``None``."""
    return _HEX_TO_NAME.get(int(value) & 0xFFFFFF)


def nearest_colour_name(value: int) -> str:
    """The named colour closest to an arbitrary ``0xRRGGBB`` value."""
    red, green, blue = _hex_to_rgb(value)
    best_name = ""
    best_distance: Optional[float] = None
    for name, other_red, other_green, other_blue in _NAMED_RGB:
        distance = _redmean_distance(red, green, blue, other_red, other_green, other_blue)
        if best_distance is None or distance < best_distance:
            best_name, best_distance = name, distance
    return best_name


def simple_colour_of(value: int) -> str:
    """The simple colour an arbitrary ``0xRRGGBB`` value reads as.

    Exact named colours use their curated bucket; everything else snaps to the
    closest named colour and borrows its bucket. Always returns a bucket.
    """
    name = colour_name(value) or nearest_colour_name(value)
    return NAME_TO_SIMPLE_COLOUR[name]


def names_for_simple_colour(raw: object) -> Tuple[str, ...]:
    """Every colour name that belongs to the simple colour *raw* (may be empty)."""
    simple = normalise_simple_colour(raw)
    if simple is None:
        return ()
    return SIMPLE_COLOUR_CATEGORIES[simple]
