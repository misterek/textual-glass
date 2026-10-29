"""textual-glass: a pixel glass look for stock Textual apps.

    from textual.app import App
    from textual_glass import Glass

    class MyApp(Glass, App):
        ...

    class Gauge(PixelWidget):              # a widget that adds pixels of its own
        def render_line(self, y): ...      # text, and a fallback without glass
        def pixel_items(self, ctx: PixelContext): ...

Ready-made widgets with pixels (``GlassPanel``, ``SelectList``, ``RingGauge``,
``Telemetry``, ``StatTile``, ``ProgressArc``, ``SpriteView``...) live in
``textual_glass.widgets``. ``app.glass.flourish(name, at=widget)`` plays a short
celestial animation when something happens; ``FLOURISHES`` lists the kinds.

In terminals with the kitty graphics protocol (Ghostty, kitty) the app gets
glass panels, gradient borders, a sliding selection pill and more, drawn in
pixels under the text. Everywhere else it is exactly stock Textual.
"""

from .detection import Support, detect
from .engine import MOTIONS, GlassEngine
from .flourishes import FLOURISHES
from .glass import Glass, PixelContext, PixelMixin, terminal_write
from .hard_exit import on_hard_exit
from .images import LoadedImage, load_image
from .widgets import (
    GlassImage,
    GlassPanel,
    InfoCard,
    PixelWidget,
    ProgressArc,
    RingGauge,
    SelectList,
    SpriteView,
    StatTile,
    StatValue,
    Telemetry,
)

__version__ = "0.3.0"
__all__ = [
    "FLOURISHES", "MOTIONS", "Glass", "GlassEngine", "GlassImage", "GlassPanel", "InfoCard", "LoadedImage",
    "PixelContext", "PixelMixin", "PixelWidget", "ProgressArc", "RingGauge", "SelectList", "SpriteView", "StatTile",
    "StatValue", "Support", "Telemetry", "__version__", "detect", "load_image", "on_hard_exit", "terminal_write",
]
