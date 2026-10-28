"""The line filter that lets the glass show through.

Textual paints every cell with an explicit background colour. For each widget,
glass works out which of those colours are *structural* (the screen, container
and table surfaces, the table header and cursor, the footer...) and rewrites
them to the terminal's default background. Ghostty leaves default-background
cells transparent, so the pixel layer under them shows. Any other background
(a score badge, a coloured button) stays an ordinary opaque cell on top.

The filter runs last in Textual's filter chain, after its ANSI-to-truecolor
conversion (which would otherwise turn the default colour back into RGB).
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.color import Color as RichColor
from rich.segment import Segment
from rich.style import Style
from textual.filter import LineFilter

from .kitty import PLACEHOLDER

if TYPE_CHECKING:
    from textual.color import Color
    from textual.widget import Widget

RGB = tuple[int, int, int]
_DEFAULT_BG = Style(bgcolor=RichColor.default())
_BLANK = Style(bgcolor=RichColor.default(), underline=False, strike=False, reverse=False, overline=False)


@dataclass(frozen=True)
class Rules:
    """What to do with one widget's segments."""

    owned: frozenset[RGB] = field(default_factory=frozenset)
    """Background colours to replace with the terminal default."""
    blank: bool = False
    """Replace all text with spaces (scrollbars and progress bars drawn in pixels)."""
    dim: tuple[RGB, float] | None = None
    """Blend explicit colours toward this colour (the screen under a modal).

    The screen under a modal reaches the modal's filters already filtered, and
    Textual's ANSI-to-truecolor filter has turned its default backgrounds into
    the ANSI theme's background colour on the way, so that colour goes in
    ``owned`` to be turned back into the default."""

    def apply(self, segments: list[Segment]) -> list[Segment]:
        owned, blank, dim = self.owned, self.blank, self.dim
        out: list[Segment] = []
        append = out.append
        for segment in segments:
            text, style, control = segment
            if control or style is None:
                if blank and not control and text:
                    append(Segment(" " * cell_len(text), _DEFAULT_BG))
                else:
                    append(segment)
                continue
            if blank:
                append(Segment(" " * cell_len(text), _blanked(style)))
                continue
            bg = style.bgcolor
            triplet = bg.triplet if bg is not None else None
            is_owned = triplet is not None and (triplet[0], triplet[1], triplet[2]) in owned
            if dim is not None:
                if is_owned:
                    style = _defaulted(style)
                if PLACEHOLDER in text:  # image cells: the foreground is the image id, so only the background goes
                    append(Segment(text, style, control))
                else:
                    append(Segment(text, _dimmed(style, dim[0], dim[1]), control))
                continue
            if is_owned and not style.reverse:
                style = _defaulted(style)
            append(Segment(text, style, control))
        return out


NO_RULES = Rules()


@lru_cache(maxsize=4096)
def _defaulted(style: Style) -> Style:
    return style + _DEFAULT_BG


@lru_cache(maxsize=1024)
def _blanked(style: Style) -> Style:
    return style + _BLANK


@lru_cache(maxsize=4096)
def _dimmed(style: Style, toward: RGB, amount: float) -> Style:
    def blend(c: RichColor | None) -> RichColor | None:
        if c is None or c.triplet is None:
            return c
        r, g, b = c.triplet
        return RichColor.from_rgb(r + (toward[0] - r) * amount, g + (toward[1] - g) * amount,
                                  b + (toward[2] - b) * amount)

    color, bgcolor = blend(style.color), blend(style.bgcolor)
    if color is style.color and bgcolor is style.bgcolor:
        return style
    return style + Style.from_color(color, bgcolor)


class GlassFilter(LineFilter):
    """One per widget. Asks ``rules_for(widget)`` what to rewrite."""

    def __init__(self, widget: Widget, rules_for: Callable[[Widget], Rules | None]) -> None:
        super().__init__()
        self._widget = weakref.ref(widget)
        self._rules_for = rules_for

    def apply(self, segments: list[Segment], background: Color) -> list[Segment]:
        widget = self._widget()
        if widget is None:
            return segments
        rules = self._rules_for(widget)
        if rules is None or rules is NO_RULES:
            return segments
        return rules.apply(segments)
