"""The Orbital look, colour helpers and the small formatting helpers every widget shares.

The look is a Textual theme, :data:`ORBITAL`: register it and select it
(``app.register_theme(ORBITAL); app.theme = "orbital"``). Its ``glass-*``
variables tell textual-glass how to paint the backdrop and the scrim, so any
Textual app can use it, and an orbital app keeps working under any other theme.

:data:`PALETTE` holds the same colours (and a few more) as hex strings for Rich
styles. The themes are built from it when this module is imported; widgets read
it when they render, so assign new values to its keys before the app starts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from textual.theme import Theme

#: Every colour of the Orbital look, as hex strings for Rich styles.
PALETTE: dict[str, str] = {
    # surfaces (the theme's $background, $surface and $panel)
    "background": "#070b18",
    "surface": "#0d1430",
    "panel": "#16203d",
    # accents
    "accent": "#38e1ff",
    "accent_2": "#a46bff",
    # text, brightest to faintest
    "text_bright": "#e6eeff",
    "text": "#c9d6f2",
    "text_soft": "#9db0d6",
    "text_dim": "#6d7fa6",
    "text_faint": "#4d5d7f",
    "text_ghost": "#3b4a6b",
    # states
    "green": "#6dffa8",
    "amber": "#ffc24b",
    "orange": "#ff8a3d",
    "red": "#ff4d6d",
    "magenta": "#ff6ec7",
    "violet": "#a46bff",
    "blue": "#5aa9ff",
    "teal": "#3fe0c5",
    "slate": "#7083ad",
    "grey": "#4f5d7e",
}


def _orbital(name: str, nebula_strength: str) -> Theme:
    return Theme(
        name=name,
        primary=PALETTE["accent"],
        secondary=PALETTE["accent_2"],
        accent=PALETTE["amber"],
        background=PALETTE["background"],
        surface=PALETTE["surface"],
        panel=PALETTE["panel"],
        foreground=PALETTE["text"],
        success=PALETTE["green"],
        warning=PALETTE["amber"],
        error=PALETTE["red"],
        dark=True,
        variables={
            "glass-backdrop": "nebula",
            "glass-scrim-alpha": "0.62",
            "glass-nebula-1": "#5b2a9e",  # violet, top right
            "glass-nebula-2": "#0e6a78",  # teal, bottom left
            "glass-nebula-3": "#1b3d8f",  # blue, scattered
            "glass-nebula-strength": nebula_strength,  # 1 is the dim default, 0 no nebulae
        },
    )


#: Deep space with dim violet and teal nebulae (the default).
ORBITAL = _orbital("orbital", nebula_strength="1.0")
#: The same space without the nebulae: dark navy and stars.
ORBITAL_NAVY = _orbital("orbital-navy", nebula_strength="0")

#: Badge colours for the data sources; other keys get a neutral colour.
SOURCE_COLORS: dict[str, str] = {
    "CC": "#ff9a5c",  # Claude Code
    "GH": "#d6e2ff",  # GitHub
}

#: Names for sources that don't give one.
SOURCE_NAMES: dict[str, str] = {
    "CC": "Claude Code",
    "GH": "GitHub",
}

#: Colours for the "why" column, by the verb a source uses; other verbs are dim.
WHY_COLORS: dict[str, str] = {
    "claude waiting": PALETTE["red"],
    "respond": PALETTE["red"],
    "unblock": PALETTE["orange"],
    "review": PALETTE["orange"],
    "land it": PALETTE["green"],
}


def urgency_color(score: float) -> str:
    """Colour for a 0-10 attention score: red when it is hot, grey when it isn't."""
    if score >= 9:
        return PALETTE["red"]
    if score >= 7:
        return PALETTE["orange"]
    if score >= 5:
        return PALETTE["amber"]
    if score >= 3:
        return PALETTE["slate"]
    return PALETTE["grey"]


def source_color(source: str) -> str:
    return SOURCE_COLORS.get(source, PALETTE["text_soft"])


def why_color(why: str) -> str:
    return WHY_COLORS.get(why, PALETTE["text_dim"])


def load_color(fraction: float) -> tuple[str, str]:
    """Two-stop gradient for a "more is worse" gauge (cyan, then amber, then red)."""
    if fraction < 0.45:
        return PALETTE["accent"], "#7c8cff"
    if fraction < 0.75:
        return PALETTE["amber"], PALETTE["orange"]
    return PALETTE["orange"], PALETTE["red"]


def health_gradient(fraction: float) -> tuple[str, str]:
    """Two-stop gradient for a "more is better" gauge (red, then amber, then green)."""
    if fraction >= 0.75:
        return PALETTE["green"], PALETTE["accent"]
    if fraction >= 0.45:
        return PALETTE["amber"], PALETTE["green"]
    return PALETTE["red"], PALETTE["orange"]


# -- time ---------------------------------------------------------------------


def now() -> datetime:
    """Timezone-aware now (all model timestamps are aware)."""
    return datetime.now(timezone.utc)


def fmt_age(delta: timedelta | float) -> str:
    """How long ago, at minute resolution: <1m, 3m, 5h, 2d, 6w.

    Every age on screen uses this, so they agree with each other and change
    at most once a minute (seconds would make an idle dashboard redraw).
    """
    seconds = delta.total_seconds() if isinstance(delta, timedelta) else float(delta)
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "<1m"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h"
    days = hours / 24
    if days < 14:
        return f"{int(days)}d"
    return f"{int(days // 7)}w"


def fmt_interval(seconds: float) -> str:
    """A polling interval: 10s, 1m, 5m."""
    if seconds < 60:
        return f"{seconds:g}s"
    return f"{seconds / 60:g}m"


def ellipsize(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"
