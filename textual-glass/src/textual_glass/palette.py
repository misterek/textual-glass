"""Colours: small helpers on (r, g, b) int tuples, and a palette from the Textual theme."""

from __future__ import annotations

import colorsys
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Union

from textual.color import Color, ColorParseError

if TYPE_CHECKING:
    from textual.app import App

RGB = tuple[int, int, int]
BackgroundOption = Union[str, Sequence[str], None]  # noqa: UP007 - a runtime alias on 3.10


def to_rgb(value: Color | str | None, fallback: RGB = (128, 128, 128)) -> RGB:
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            value = Color.parse(value)
        except ColorParseError:
            return fallback
    if value.ansi is not None:
        return fallback
    return (value.r, value.g, value.b)


def hex_of(c: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*c)


def rgb(hex_color: str) -> RGB:
    """``"#38e1ff"`` as ``(56, 225, 255)``. No Textual involved, so the renderers can use it."""
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _clamp(v: float) -> int:
    return max(0, min(255, int(round(v))))


def mix(a: RGB, b: RGB, t: float) -> RGB:
    return (_clamp(a[0] + (b[0] - a[0]) * t), _clamp(a[1] + (b[1] - a[1]) * t), _clamp(a[2] + (b[2] - a[2]) * t))


def lighten(c: RGB, amount: float) -> RGB:
    return mix(c, (255, 255, 255), amount)


def darken(c: RGB, amount: float) -> RGB:
    return mix(c, (0, 0, 0), amount)


def luminance(c: RGB) -> float:
    return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255


def chroma(c: RGB) -> int:
    return max(c) - min(c)


def distance(a: RGB, b: RGB) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def hue_shift(c: RGB, degrees: float, light: float = 0.0, sat: float = 1.0) -> RGB:
    """Rotate the hue, nudge lightness by ``light`` (-1..1) and scale saturation."""
    hue, lightness, saturation = colorsys.rgb_to_hls(c[0] / 255, c[1] / 255, c[2] / 255)
    hue = (hue + degrees / 360) % 1.0
    lightness = min(1.0, max(0.0, lightness + light))
    saturation = min(1.0, max(0.0, saturation * sat))
    r, g, b = colorsys.hls_to_rgb(hue, lightness, saturation)
    return (_clamp(r * 255), _clamp(g * 255), _clamp(b * 255))


def border_pair(c: RGB) -> tuple[RGB, RGB]:
    """The two ends of a border gradient for a border colour."""
    return c, hue_shift(c, 32, light=0.07, sat=1.05)


#: Backdrop painters (``textual_glass.backdrop``), picked by the theme variable ``glass-backdrop``.
BACKDROPS = ("gradient", "nebula")
#: The ``nebula`` painter's defaults: a deep-space gradient (top, bottom) and three nebula colours.
SPACE: tuple[RGB, RGB] = ((0x0B, 0x14, 0x33), (0x03, 0x05, 0x0D))
NEBULA: tuple[RGB, RGB, RGB] = ((0x5B, 0x2A, 0x9E), (0x0E, 0x6A, 0x78), (0x1B, 0x3D, 0x8F))


@dataclass(frozen=True)
class Palette:
    """Theme colours the renderer needs, as RGB tuples.

    The fields after ``bg_bottom`` come from optional theme variables (any
    Textual ``Theme.variables``): ``glass-backdrop`` (``gradient`` or
    ``nebula``), ``glass-scrim-alpha``, ``glass-space-top/bottom``,
    ``glass-nebula-1/2/3`` and ``glass-nebula-strength`` (1 is the full
    nebulae, 0 leaves the plain space gradient and its stars).
    """

    dark: bool
    background: RGB
    surface: RGB
    panel: RGB
    foreground: RGB
    primary: RGB
    secondary: RGB
    accent: RGB
    success: RGB
    warning: RGB
    error: RGB
    bg_top: RGB
    bg_bottom: RGB
    scrim_alpha: float = 0.5  # from_app: 0.5 on dark themes, 0.3 on light ones
    backdrop: str = "gradient"
    space: tuple[RGB, RGB] = SPACE
    nebula: tuple[RGB, RGB, RGB] = NEBULA
    nebula_strength: float = 1.0

    @property
    def scrim(self) -> RGB:
        return darken(self.background, 0.35) if self.dark else darken(self.background, 0.55)

    @property
    def mid_background(self) -> RGB:
        return mix(self.bg_top, self.bg_bottom, 0.45)

    def is_structural(self, c: RGB) -> bool:
        """Is this a neutral surface colour that the glass should replace?

        Colourful backgrounds (badges, buttons, swatches) are the app's own and
        stay on top of the glass as ordinary cells.
        """
        if chroma(c) <= 36:
            return True
        return any(distance(c, s) < 40 for s in (self.background, self.surface, self.panel))

    @classmethod
    def from_app(cls, app: App, background: BackgroundOption = None) -> Palette:
        try:
            variables = app.get_css_variables()
        except Exception:  # noqa: BLE001
            variables = {}
        dark = bool(getattr(app.current_theme, "dark", True))
        default_bg = (18, 18, 18) if dark else (240, 240, 240)

        def get(name: str, fallback: RGB) -> RGB:
            return to_rgb(variables.get(name), fallback)

        def number(name: str, fallback: float, low: float, high: float) -> float:
            try:
                return min(high, max(low, float(variables.get(name, fallback))))
            except (TypeError, ValueError):
                return fallback

        bg = get("background", default_bg)
        surface = get("surface", lighten(bg, 0.05) if dark else darken(bg, 0.03))
        panel = get("panel", lighten(bg, 0.1) if dark else darken(bg, 0.06))
        primary = get("primary", (1, 120, 212))
        top, bottom = _gradient(background, bg, panel, primary, dark)
        backdrop = str(variables.get("glass-backdrop", "gradient")).strip()
        return cls(
            dark=dark,
            background=bg,
            surface=surface,
            panel=panel,
            foreground=get("foreground", (224, 224, 224) if dark else (30, 30, 30)),
            primary=primary,
            secondary=get("secondary", primary),
            accent=get("accent", (254, 166, 43)),
            success=get("success", (78, 191, 113)),
            warning=get("warning", (254, 166, 43)),
            error=get("error", (186, 60, 91)),
            bg_top=top,
            bg_bottom=bottom,
            scrim_alpha=number("glass-scrim-alpha", 0.5 if dark else 0.3, 0.0, 1.0),
            backdrop=backdrop if backdrop in BACKDROPS else "gradient",
            space=(get("glass-space-top", SPACE[0]), get("glass-space-bottom", SPACE[1])),
            nebula=(get("glass-nebula-1", NEBULA[0]), get("glass-nebula-2", NEBULA[1]),
                    get("glass-nebula-3", NEBULA[2])),
            nebula_strength=number("glass-nebula-strength", 1.0, 0.0, math.inf),
        )


def _gradient(option: BackgroundOption, bg: RGB, panel: RGB, primary: RGB, dark: bool) -> tuple[RGB, RGB]:
    """Top and bottom colours of the backdrop gradient."""
    if isinstance(option, str) and option not in ("", "auto"):
        c = to_rgb(option, bg)
        return (lighten(c, 0.05), darken(c, 0.3)) if dark else (lighten(c, 0.04), darken(c, 0.08))
    if isinstance(option, (tuple, list)) and len(option) == 2:
        return to_rgb(option[0], bg), to_rgb(option[1], bg)
    if dark:
        base = mix(bg, panel, 0.5)
        return lighten(mix(base, primary, 0.09), 0.05), darken(mix(bg, primary, 0.03), 0.18)
    base = mix(bg, panel, 0.35)
    return lighten(base, 0.04), darken(mix(base, primary, 0.06), 0.07)
