"""Widgets that work with and without glass, with the same layout in both.

With glass they draw pixels under their text; without it they are plain
Textual (block-character meters, glyphs, rounded borders).

* ``GlassImage``: a picture (kitty placeholders with glass, half blocks without).
* ``GlassPanel``: a titled bordered container (``set_title``, ``set_accent``).
* ``SelectList``: a keyboard list with a sliding selection pill; subclass it.
* ``RingGauge``, ``Telemetry``, ``StatValue``, ``StatTile``, ``ProgressArc``,
  ``SpriteView``, ``InfoCard``: instruments.
* ``PixelWidget``, ``strip``, ``fit``, ``placeholder_parts``, ``Tween``: for
  writing your own (see ``textual_glass.widgets.base``).
"""

from .base import (
    BRIGHT,
    DIM,
    FAINT,
    SOFT,
    Part,
    PixelWidget,
    SelectList,
    Tween,
    ellipsize,
    fit,
    hex_colour,
    placeholder_parts,
    strip,
    text_meter,
    tone,
)
from .image import GlassImage
from .instruments import BLOCKS, InfoCard, ProgressArc, RingGauge, SpriteView, StatTile, StatValue, Telemetry
from .panel import GlassPanel

__all__ = [
    "BLOCKS", "BRIGHT", "DIM", "FAINT", "SOFT", "GlassImage", "GlassPanel", "InfoCard", "Part", "PixelWidget",
    "ProgressArc", "RingGauge", "SelectList", "SpriteView", "StatTile", "StatValue", "Telemetry", "Tween",
    "ellipsize", "fit", "hex_colour", "placeholder_parts", "strip", "text_meter", "tone",
]
