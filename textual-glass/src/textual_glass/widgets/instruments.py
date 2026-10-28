"""Instruments: ring gauges, charts, stat values and tiles, progress arcs, a
sprite view and an info card.

Numbers and labels are Textual text; rings, bars and areas are pixels. Without
glass the pixels become block characters and glyphs in the same cells, so the
layout is identical in both modes. Colours default to the app's theme.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, ClassVar, Literal

import numpy as np
from rich.text import Text
from textual.strip import Strip

from ..engine import SpriteMaker
from ..items import Area, Bars, Ring
from ..palette import Palette, border_pair, hex_of, mix
from ..render import Colour, Item, rgb_of
from ..scene import PixelContext
from .base import (
    BRIGHT,
    DIM,
    FAINT,
    SOFT,
    Part,
    PixelWidget,
    ellipsize,
    hex_colour,
    placeholder_parts,
    strip,
    text_meter,
    tone,
)
from .panel import GlassPanel

#: Eighth blocks, empty to full: the text form of bars and areas.
BLOCKS = " ▁▂▃▄▅▆▇█"
Pair = tuple[Colour, Colour]


def _blend(c1: Colour, c2: Colour, t: float) -> str:
    return hex_of(mix(rgb_of(c1), rgb_of(c2), t))


def _eighths(fraction: float, rows: int, from_bottom: int) -> int:
    """How many eighths of the cell ``from_bottom`` rows up a bar of ``fraction`` fills."""
    level = max(0.0, min(1.0, fraction)) * rows * 8
    eighths = int(max(0.0, min(8.0, level - from_bottom * 8)))
    return max(1, eighths) if from_bottom == 0 and fraction > 0 else eighths  # a value above zero always shows


class RingGauge(PixelWidget):
    """A 270-degree ring with a value written in the middle and a label underneath.

    ``set(fraction, text)``: ``fraction`` (0..1) lights the arc, ``text`` is
    written inside (a count, a percentage). Without glass the arc is a row of
    blocks under the text.

    Args:
        label: Under the ring (empty: the ring takes the whole height).
        sublabel: A dimmer line under the label, when there is room.
        colors: The arc's gradient (default: the theme's primary colour, shaded).
    """

    DEFAULT_CSS = """
    RingGauge { width: 1fr; height: 1fr; min-height: 3; }
    """

    def __init__(self, label: str = "", *, fraction: float = 0.0, text: str = "", sublabel: str = "",
                 colors: Pair | None = None, name: str | None = None, id: str | None = None,
                 classes: str | None = None) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.label = label
        self.fraction = fraction
        self.text = text
        self.sublabel = sublabel
        self.tones = colors

    def set(self, fraction: float, text: str, colors: Pair | None = None, sublabel: str | None = None) -> None:
        """Show ``fraction`` (0..1) with ``text`` inside; ``colors`` and ``sublabel`` stay as they were if None."""
        new = (fraction, text, colors or self.tones, self.sublabel if sublabel is None else sublabel)
        if new != (self.fraction, self.text, self.tones, self.sublabel):
            self.fraction, self.text, self.tones, self.sublabel = new
            self.refresh()

    def arc_colours(self, palette: Palette) -> Pair:
        return self.tones or border_pair(palette.primary)

    def _ring_rows(self) -> int:
        h = self.size.height
        rows = h - (1 if self.label else 0) - (1 if self.label and self.sublabel and h >= 8 else 0)
        return max(1, rows if rows % 2 else rows - 1)

    def render_line(self, y: int) -> Strip:
        w = self.size.width
        pal = self.theme_palette
        c1 = hex_colour(self.arc_colours(pal)[0])
        ring = self._ring_rows()
        mid = ring // 2
        if y == mid:
            return strip([(self.text.center(w), f"bold {c1}")], w)
        if y == mid + 1 and ring >= 3 and not self.glass_on:
            return strip([(text_meter(self.fraction, min(8, w - 2)).center(w), c1)], w)
        if self.label and y == ring:
            return strip([(ellipsize(self.label, w).center(w), f"bold {tone(pal, SOFT)}")], w)
        if self.label and self.sublabel and y == ring + 1:
            return strip([(ellipsize(self.sublabel, w).center(w), tone(pal, DIM))], w)
        return Strip.blank(w)

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        r = self.content_region
        rows = self._ring_rows()
        if rows < 3 or not r.width:
            return ()
        c1, c2 = self.arc_colours(ctx.palette)
        return (Ring((float(r.x), float(r.y), float(r.width), float(rows)), self.fraction, c1, c2,
                     ident=f"ring:{id(self)}", clip=self.visible_clip()),)


class Telemetry(PixelWidget):
    """A glowing chart: per-period ``bars`` (with a *ghost* series, such as last
    period's values, as faint outlines) or a smooth ``area`` (ghost not drawn).

    ``set_series(values, ghost=(), labels=(), today=-1, scale_max=None)``. With
    labels, the last row holds them, one letter under each bar or point;
    ``today`` is the index drawn brighter. Without glass the chart is drawn in
    eighth blocks. Both modes, with and without glass, fill the chart's height
    at :attr:`scale`: ``scale_max``, or else 1 or the largest value or ghost,
    whichever is bigger. Values above ``scale_max`` are cut at the top.

    Args:
        mode: ``"bars"`` or ``"area"``.
        colors: The gradient from the first bar to the last (default: the
            theme's primary colour, shaded).
    """

    DEFAULT_CSS = """
    Telemetry { height: 1fr; min-height: 2; }
    """

    def __init__(self, *, mode: Literal["bars", "area"] = "bars", colors: Pair | None = None,
                 name: str | None = None, id: str | None = None, classes: str | None = None) -> None:
        if mode not in ("bars", "area"):
            raise ValueError(f"mode must be 'bars' or 'area', not {mode!r}")
        super().__init__(name=name, id=id, classes=classes)
        self.mode = mode
        self.tones = colors
        self.values: tuple[float, ...] = ()
        self.ghost: tuple[float, ...] = ()
        self.labels: tuple[str, ...] = ()
        self.today = -1
        self.scale_max: float | None = None

    def set_series(self, values: Sequence[float], ghost: Sequence[float] = (), labels: Sequence[str] = (),
                   today: int = -1, colors: Pair | None = None, scale_max: float | None = None) -> None:
        """Show ``values``; ``scale_max`` (above 0) is the value that fills the height (None: see :attr:`scale`)."""
        if scale_max is not None and scale_max <= 0:
            raise ValueError(f"scale_max must be above 0, not {scale_max!r}")
        new = (tuple(values), tuple(ghost), tuple(labels), today, colors or self.tones, scale_max)
        if new != (self.values, self.ghost, self.labels, self.today, self.tones, self.scale_max):
            self.values, self.ghost, self.labels, self.today, self.tones, self.scale_max = new
            self.refresh()

    @property
    def scale(self) -> float:
        """The value that fills the chart's height: ``scale_max``, or max(1, the largest value or ghost)."""
        return self.scale_max if self.scale_max is not None else max([1.0, *self.values, *self.ghost])

    def chart_colours(self, palette: Palette) -> Pair:
        return self.tones or border_pair(palette.primary)

    @property
    def chart_rows(self) -> int:
        return self.size.height - (1 if self.labels else 0)

    def _columns(self, width: int) -> list[int]:
        """The cell under each bar's centre (bars) or each point (area), as the items place them."""
        n = len(self.values)
        if self.mode == "area":
            return [int(0.5 + (width - 1) * k / (n - 1)) for k in range(n)] if n > 1 else []
        pitch = (width - 0.6) / max(1, n)
        return [int(0.3 + (k + 0.5) * pitch) for k in range(n)]

    def render_line(self, y: int) -> Strip:
        w, h = self.size
        pal = self.theme_palette
        c1, c2 = self.chart_colours(pal)
        rows = self.chart_rows
        if self.labels and y == h - 1:
            cells: list[Part] = [(" ", "")] * w
            dim = tone(pal, DIM)
            for k, x in enumerate(self._columns(w)):
                if k < len(self.labels) and 0 <= x < w and self.labels[k]:
                    cells[x] = (self.labels[k][:1], f"bold {hex_colour(c1)}" if k == self.today else dim)
            return strip(cells, w)
        if self.glass_on or y >= rows or not self.values:
            return Strip.blank(w)
        from_bottom = rows - 1 - y
        faint = tone(pal, FAINT)
        if self.mode == "area":
            n = len(self.values)
            if n < 2:
                return Strip.blank(w)
            peak = self.scale
            points = 0.5 + (w - 1) * np.arange(n) / (n - 1)
            levels = np.interp(np.arange(w) + 0.5, points, np.asarray(self.values, float) / peak)
            parts: list[Part] = []
            for x, level in enumerate(levels):
                eighths = _eighths(float(level), rows, from_bottom)
                parts.append((BLOCKS[eighths], _blend(c1, c2, x / max(1, w - 1))) if eighths else (" ", ""))
            return strip(parts, w)
        peak = self.scale
        cells = [(" ", "")] * w
        n = len(self.values)
        for k, x in enumerate(self._columns(w)):
            if not 0 <= x < w:
                continue
            eighths = _eighths(self.values[k] / peak, rows, from_bottom)
            if eighths:
                cells[x] = (BLOCKS[eighths], _blend(c1, c2, k / max(1, n - 1)))
            elif k < len(self.ghost) and _eighths(self.ghost[k] / peak, rows, from_bottom):
                cells[x] = ("░", faint)
            elif from_bottom == 0:
                cells[x] = ("·", faint)
        return strip(cells, w)

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        r = self.content_region
        rows = self.chart_rows
        if rows < 1 or not r.width or not self.values:
            return ()
        rect = (float(r.x), float(r.y), float(r.width), float(rows))
        c1, c2 = self.chart_colours(ctx.palette)
        clip = self.visible_clip()
        peak = self.scale
        if self.mode == "area":
            return (Area(rect, tuple(v / peak for v in self.values), c1, c2, ident=f"area:{id(self)}", clip=clip),)
        return (Bars(rect, self.values, c1, c2, self.ghost, today=self.today, scale_max=peak, ident=f"bars:{id(self)}",
                     clip=clip),)


class StatValue(PixelWidget):
    """A big number with its change ("12  ▲3 vs last week"), on one line.

    ``set(value, delta=None, better="up")``: a rise is good (success colour)
    when ``better`` is ``"up"``, bad (error colour) when it is ``"down"``.

    Args:
        versus: What the change is measured against ("vs last week"), shown when it fits.
        color: The number's colour (default: bright text).
    """

    DEFAULT_CSS = """
    StatValue { height: 1; }
    """

    def __init__(self, *, versus: str = "", color: Colour | None = None, name: str | None = None,
                 id: str | None = None, classes: str | None = None) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.versus = versus
        self.value = "–"
        self.delta: float | None = None
        self.better: Literal["up", "down"] = "up"
        self.color = color

    def set(self, value: str, delta: float | None = None, better: Literal["up", "down"] = "up",
            color: Colour | None = None) -> None:
        new = (value, delta, better, color if color is not None else self.color)
        if new != (self.value, self.delta, self.better, self.color):
            self.value, self.delta, self.better, self.color = new
            self.refresh()

    def render_line(self, y: int) -> Strip:
        w = self.size.width
        if y:
            return Strip.blank(w)
        pal = self.theme_palette
        colour = hex_colour(self.color) if self.color is not None else tone(pal, BRIGHT)
        parts: list[Part] = [(self.value, f"bold {colour}")]
        if self.delta is not None:
            if self.delta == 0:
                parts.append(("  ±0", tone(pal, DIM)))
            else:
                good = (self.delta > 0) == (self.better == "up")
                arrow = "▲" if self.delta > 0 else "▼"
                parts.append((f"  {arrow}{abs(self.delta):g}", f"bold {hex_of(pal.success if good else pal.error)}"))
            if self.versus and sum(len(t) for t, _ in parts) + 1 + len(self.versus) <= w:
                parts.append((" " + self.versus, tone(pal, FAINT)))
        return strip(parts, w)


class StatTile(GlassPanel):
    """A panel with a title, a big number with its change, and a chart of recent values.

    ``set(value, delta, series, ghost=(), labels=(), today=-1, better="up", scale_max=None)``.

    Args:
        mode: The chart's mode (see :class:`Telemetry`).
        versus: What the change is measured against (see :class:`StatValue`).
        colors: The chart's gradient and the border's colours (default: the theme's).
    """

    DEFAULT_CSS = """
    StatTile { height: 1fr; width: 1fr; }
    """

    def __init__(self, title: str, *, mode: Literal["bars", "area"] = "bars", versus: str = "",
                 colors: Pair | None = None, name: str | None = None, id: str | None = None,
                 classes: str | None = None) -> None:
        self.stat = StatValue(versus=versus)
        self.chart = Telemetry(mode=mode, colors=colors)
        super().__init__(self.stat, self.chart, title=title, accent=colors[0] if colors else None,
                         accent_2=colors[1] if colors else None, name=name, id=id, classes=classes)

    def set(self, value: str | float, delta: float | None, series: Sequence[float], ghost: Sequence[float] = (),
            labels: Sequence[str] = (), today: int = -1, better: Literal["up", "down"] = "up",
            scale_max: float | None = None) -> None:
        self.stat.set(str(value), delta, better)
        self.chart.set_series(series, ghost, labels, today, scale_max=scale_max)


class ProgressArc(PixelWidget):
    """Progress as a full ring with the percentage inside, and a label, an
    optional sublabel and optional detail parts underneath.

    ``set(fraction, label, sublabel="", detail=())``: ``fraction`` None shows
    nothing; ``detail`` is a line of ``(text, style)`` parts ("2 open · 1
    blocked"), shown from 8 rows up. Without glass the ring is a row of blocks.

    Args:
        colors: The arc's gradient (default: the theme's primary colour,
            shaded; success to primary from ``NEARLY_DONE`` on).
    """

    #: From this fraction on the default arc turns to the theme's success colour.
    NEARLY_DONE: ClassVar[float] = 0.85

    DEFAULT_CSS = """
    ProgressArc { width: 1fr; height: 1fr; min-height: 4; }
    """

    def __init__(self, *, colors: Pair | None = None, name: str | None = None, id: str | None = None,
                 classes: str | None = None) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.tones = colors
        self.fraction: float | None = None
        self.label = ""
        self.sublabel = ""
        self.detail: tuple[Part, ...] = ()

    def set(self, fraction: float | None, label: str = "", sublabel: str = "", detail: Sequence[Part] = ()) -> None:
        new = (fraction, label, sublabel, tuple(detail))
        if new != (self.fraction, self.label, self.sublabel, self.detail):
            self.fraction, self.label, self.sublabel, self.detail = new
            self.refresh()

    def arc_colours(self, palette: Palette) -> Pair:
        if self.tones:
            return self.tones
        if (self.fraction or 0.0) >= self.NEARLY_DONE:
            return palette.success, palette.primary
        return border_pair(palette.primary)

    def _text_lines(self, palette: Palette, width: int) -> list[list[Part]]:
        """The lines under the ring, top to bottom."""
        room = max(1, width - 2)  # a gap between neighbouring arcs
        lines: list[list[Part]] = []
        if self.label:
            lines.append([(ellipsize(self.label, room).center(width), f"bold {tone(palette, BRIGHT)}")])
        if self.sublabel:
            lines.append([(ellipsize(self.sublabel, room).center(width), tone(palette, DIM))])
        if self.detail and self.size.height >= 8:
            used = sum(len(t) for t, _ in self.detail)
            lines.append([(" " * max(0, (width - used) // 2), ""), *self.detail])
        return lines

    def _ring_rows(self) -> int:
        h = self.size.height
        rows = h - (1 if self.label else 0) - (1 if self.sublabel else 0) - (1 if self.detail and h >= 8 else 0)
        return max(1, rows if rows % 2 else rows - 1)

    def render_line(self, y: int) -> Strip:
        w, h = self.size
        if self.fraction is None:
            return Strip.blank(w)
        pal = self.theme_palette
        lines = self._text_lines(pal, w)
        first = h - len(lines)
        if y >= first:
            return strip(lines[y - first], w)
        ring = self._ring_rows()
        c1 = hex_colour(self.arc_colours(pal)[0])
        if y == ring // 2:
            return strip([(f"{round(self.fraction * 100)}%".center(w), f"bold {c1}")], w)
        if y == ring // 2 + 1 and ring >= 3 and not self.glass_on:
            return strip([(text_meter(self.fraction, min(10, w - 2)).center(w), c1)], w)
        return Strip.blank(w)

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        r = self.content_region
        if self.fraction is None or not r.width:
            return ()
        rows = self._ring_rows()
        if rows < 3:
            return ()
        c1, c2 = self.arc_colours(ctx.palette)
        return (Ring((float(r.x), float(r.y), float(r.width), float(rows)), self.fraction, c1, c2, sweep=1.0,
                     ticks=0, thickness=0.2, ident=f"arc:{id(self)}", clip=self.visible_clip()),)


class SpriteView(PixelWidget):
    """An animation the terminal plays (a sprite) filling the widget, and a
    glyph instead without glass (and until the upload lands).

    ``maker(width_px, height_px) -> (rgba_frames, gap_ms)`` draws it at the
    widget's pixel size, in the worker thread; frames must be transparent at
    their border (``sprites.radial_mask``). Sprites with the same ``key`` and
    size share one terminal image, so keep the set of keys small and finite.

    Args:
        fallback: The glyph without glass, centred.
        fallback_color: Its colour (default: the theme's primary colour).
    """

    DEFAULT_CSS = """
    SpriteView { width: 1fr; height: 1fr; }
    """

    def __init__(self, key: tuple[Any, ...], maker: SpriteMaker, *, fallback: str = "◉",
                 fallback_color: Colour | None = None, name: str | None = None, id: str | None = None,
                 classes: str | None = None) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.key = key
        self.maker = maker
        self.fallback = fallback
        self.fallback_color = fallback_color

    def set_sprite(self, key: tuple[Any, ...], maker: SpriteMaker, fallback: str | None = None) -> None:
        """Show another sprite (``key`` names it; the same key is the same picture)."""
        fallback = self.fallback if fallback is None else fallback
        if (key, fallback) != (self.key, self.fallback):
            self.key, self.maker, self.fallback = key, maker, fallback
            self.refresh()

    def render_line(self, y: int) -> Strip:
        w, h = self.size
        engine = self.glass
        image_id = engine.sprite(self.key, w, h, self.maker, waiter=self) if engine is not None else None
        if image_id is not None:
            return strip([placeholder_parts(image_id, y, w)], w)
        if y == h // 2:
            colour = self.fallback_color if self.fallback_color is not None else self.theme_palette.primary
            return strip([(self.fallback.center(w), f"bold {hex_colour(colour)}")], w)
        return Strip.blank(w)


class InfoCard(PixelWidget):
    """A headline, an optional subtitle, label/value fields and a paragraph: a
    preview of whatever is highlighted, or any small record. Long text wraps.

    ``set_card(headline, fields=(), body="", subtitle="")``: plain strings get
    the theme's text colours; Rich ``Text`` keeps its own styles.
    """

    DEFAULT_CSS = """
    InfoCard { height: 1fr; }
    """

    def __init__(self, *, name: str | None = None, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.card: tuple[Any, ...] = ("", (), "", "")
        self._wrapped: tuple[tuple[Any, ...], list[Strip]] | None = None

    def set_card(self, headline: str | Text, fields: Sequence[tuple[str, str | Text]] = (), body: str = "",
                 subtitle: str | Text = "") -> None:
        card = (headline, tuple(fields), body, subtitle)
        if card != self.card:
            self.card = card
            self._wrapped = None
            self.refresh()

    def _text(self, pal: Palette) -> Text:
        headline, fields, body, subtitle = self.card
        out = Text()
        if headline:
            bright = f"bold {tone(pal, BRIGHT)}"
            out.append_text(headline if isinstance(headline, Text) else Text(headline, style=bright))
            out.append("\n")
        if subtitle:
            out.append_text(subtitle if isinstance(subtitle, Text) else Text(subtitle, style=tone(pal, SOFT)))
            out.append("\n")
        width = max((len(k) for k, _ in fields), default=0) + 2
        for key, value in fields:
            out.append(f"{key.upper():<{width}}", style=tone(pal, FAINT))
            out.append_text(value if isinstance(value, Text) else Text(str(value), style=tone(pal)))
            out.append("\n")
        if body:
            out.append(body, style=tone(pal, DIM))
        return out

    def render_line(self, y: int) -> Strip:
        w = self.size.width
        pal = self.theme_palette
        key = (w, pal)
        if self._wrapped is None or self._wrapped[0] != key:
            console = self.app.console
            lines = self._text(pal).wrap(console, max(1, w))
            self._wrapped = (key, [Strip(list(line.render(console))).adjust_cell_length(w) for line in lines])
        strips = self._wrapped[1]
        return strips[y] if y < len(strips) else Strip.blank(w)
