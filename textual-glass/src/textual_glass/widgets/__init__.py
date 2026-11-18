"""Widgets that work with and without glass, with the same layout in both.

With glass they draw pixels under their text; without it they are plain
Textual (block-character meters, glyphs, rounded borders).

* ``GlassImage``: a picture (kitty placeholders with glass, half blocks without).
* ``GlassPanel``: a titled bordered container (``set_title``, ``set_accent``).
* ``SelectList``: a keyboard list with a sliding selection pill; subclass it.
* ``RingGauge``, ``Telemetry``, ``StatValue``, ``StatTile``, ``ProgressArc``,
  ``SpriteView``, ``InfoCard``: instruments.
* ``LineChart``: numbers over time as glowing lines (braille without glass), and
  its helpers ``series_colours``, ``short_number``, ``scale_for``, ``time_ticks``.
* ``THEME``: pass it to a setter's colour argument to go back to the theme's colour.
* ``PixelWidget``, ``strip``, ``fit``, ``placeholder_parts``, ``Tween``: for
  writing your own (see ``textual_glass.widgets.base``).
"""

from .base import (
    BRIGHT,
    DIM,
    FAINT,
    SOFT,
    THEME,
    Part,
    PixelWidget,
    SelectList,
    ThemeColours,
    Tween,
    ellipsize,
    fit,
    hex_colour,
    placeholder_parts,
    strip,
    text_meter,
    tone,
)
from .chart import MAX_LINES, LineChart, nice_ceil, round_in, scale_for, series_colours, short_number, time_ticks
from .image import GlassImage
from .instruments import BLOCKS, InfoCard, ProgressArc, RingGauge, SpriteView, StatTile, StatValue, Telemetry
from .panel import GlassPanel

__all__ = [
    "BLOCKS", "BRIGHT", "DIM", "FAINT", "MAX_LINES", "SOFT", "THEME", "GlassImage", "GlassPanel", "InfoCard",
    "LineChart", "Part", "PixelWidget", "ProgressArc", "RingGauge", "SelectList", "SpriteView", "StatTile",
    "StatValue", "Telemetry", "ThemeColours", "Tween", "ellipsize", "fit", "hex_colour", "nice_ceil",
    "placeholder_parts", "round_in", "scale_for", "series_colours", "short_number", "strip", "text_meter",
    "time_ticks", "tone",
]
