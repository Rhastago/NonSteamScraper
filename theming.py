"""Theming for NonSteamScraper: the light/dark base palettes, the accent colors
layered on top, the spacing/typography tokens, and load/save of the user's theme
and accent choices. Pure (no tkinter), so it imports cleanly anywhere."""

import os

THEME_FILE  = os.path.expanduser("~/.steamart_theme")
ACCENT_FILE = os.path.expanduser("~/.steamart_accent")

# Color palettes for light and dark appearance modes.
THEMES = {
    "light": {
        "bg": "#f0f0f0", "fg": "#1a1a1a", "entry_bg": "#ffffff",
        "select_bg": "#d8d8d8", "muted": "#888888", "muted2": "#666666",
        "placeholder": "#cccccc", "link": "#1565c0", "warn": "#d17a00",
        "danger": "#c0392b", "ok": "#2e7d32", "button_bg": "#e4e4e4",
        "button_fg": "#1a1a1a",
    },
    "dark": {
        "bg": "#2b2b2b", "fg": "#e6e6e6", "entry_bg": "#3c3f41",
        "select_bg": "#4a4d4f", "muted": "#9aa0a6", "muted2": "#888888",
        "placeholder": "#555555", "link": "#5aa0ff", "warn": "#ffb14e",
        "danger": "#ff6b6b", "ok": "#66bb6a", "button_bg": "#3c3f41",
        "button_fg": "#e6e6e6",
    },
}


# --- Design system (v1.4.0) -------------------------------------------------
# Accent is a SECOND, independent axis layered on the light/dark base palette:
# the base supplies structural colors (bg/fg/entry/…), the accent supplies the
# brand color used for the primary CTA, highlights, and links. `accent_fg` is the
# text color that sits ON the accent (white on the darker accents, near-black on
# the bright green/teal). `link` is tuned per base mode for contrast. Picking an
# accent re-launches the app (same path as the light/dark toggle).
ACCENTS = {
    "blue":   {"label": "Steam Blue", "accent": "#1a9fff", "accent_fg": "#ffffff",
               "accent_hover": "#3db0ff", "link_dark": "#5aa0ff", "link_light": "#1565c0"},
    "green":  {"label": "Vibrant Green", "accent": "#2ecc71", "accent_fg": "#0d2b16",
               "accent_hover": "#45e08a", "link_dark": "#4cd787", "link_light": "#1b8f4d"},
    "purple": {"label": "Purple", "accent": "#8b5cf6", "accent_fg": "#ffffff",
               "accent_hover": "#9d72f8", "link_dark": "#a78bfa", "link_light": "#6d28d9"},
    "teal":   {"label": "Teal", "accent": "#14b8a6", "accent_fg": "#03241f",
               "accent_hover": "#20c9b5", "link_dark": "#2dd4bf", "link_light": "#0d8276"},
}
DEFAULT_ACCENT = "blue"

# Spacing scale — use these instead of ad-hoc pixel values so padding stays
# consistent across windows. XS=tight, S=default gap, M=section, L=window edge.
PAD_XS, PAD_S, PAD_M, PAD_L = 2, 6, 12, 20

# Typography: one sans family for everything human-facing, mono ONLY for the log.
# "Arial" is already used throughout the chrome and renders on both the Deck and
# Windows, so it's the safe shared sans; "Courier" stays for the monospace log.
FONT_UI   = "Arial"
FONT_MONO = "Courier"


def resolve_theme(mode, accent):
    """Merge a base palette (light/dark) with an accent into the effective theme
    dict the UI reads via self.theme. Unknown names fall back to safe defaults."""
    base = dict(THEMES.get(mode, THEMES["dark"]))
    acc = ACCENTS.get(accent, ACCENTS[DEFAULT_ACCENT])
    base["accent"] = acc["accent"]
    base["accent_fg"] = acc["accent_fg"]
    base["accent_hover"] = acc["accent_hover"]
    base["link"] = acc["link_dark"] if mode == "dark" else acc["link_light"]
    return base


def load_theme():
    """Return the saved theme name, defaulting to dark."""
    try:
        with open(THEME_FILE, "r") as f:
            name = f.read().strip()
            return name if name in THEMES else "dark"
    except Exception:
        return "dark"


def save_theme(name):
    """Persist the chosen theme name."""
    with open(THEME_FILE, "w") as f:
        f.write(name)


def load_accent():
    """Return the saved accent name, defaulting to DEFAULT_ACCENT."""
    try:
        with open(ACCENT_FILE, "r") as f:
            name = f.read().strip()
            return name if name in ACCENTS else DEFAULT_ACCENT
    except Exception:
        return DEFAULT_ACCENT


def save_accent(name):
    """Persist the chosen accent name."""
    with open(ACCENT_FILE, "w") as f:
        f.write(name)
