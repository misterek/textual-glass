"""Scene items: what the pixel layers are made of. No Textual imports.

Every item is a frozen dataclass in screen cells (see ``render``). Stock-widget
chrome first (the CSS scene builds these): ``Panel``, ``Surface``,
``HeaderStrip``, ``Rows``, ``Pill``, ``Badge``, ``Progress``, ``Thumb``. Then
instruments for custom widgets: ``HeaderBar``, ``Rule``, ``Ring``, ``Signal``,
``Meter``, ``Area``, ``Bars``, ``Lane``, ``Dot``, ``Spine``.

**An item's light must end inside its own bounding box.** A glow that is still
faintly lit at the box is cut off there, and the cut shows as a lighter
rectangle on a dark backdrop. Every glow here has its own falloff that reaches
zero inside the box and follows the item's shape (radial for dots, rounded for
pills and bars), and each box leaves :meth:`Item.margin` pixels free for the
safety fade. ``tests/test_units.py`` renders every item class alone and
checks the outermost ring of its box; a new item must pass it.

Colours are ``RGB`` tuples or ``"#rrggbb"`` strings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import ClassVar

import numpy as np

from .palette import RGB, darken, lighten
from .render import (
    F32,
    Array,
    Box,
    Canvas,
    Colour,
    FRect,
    Grid,
    Item,
    Rect,
    box_fade,
    col,
    rgb_of,
    sd_rrect,
    smoothstep,
    x_gradient,
)

# Edge styles (per side of a panel).
NONE, LINE, HEAVY, BAR = "", "line", "heavy", "bar"

WHITE = np.ones(3, F32)
BLACK = np.zeros(3, F32)


def _glass(c: Canvas, view: Array, ys: Array, top: float, bottom: float, tint: RGB, coverage: Array,
           alpha: float, dark: bool) -> None:
    """A vertical glass gradient based on ``tint``."""
    if dark:
        hi, lo = col(lighten(tint, 0.075)), col(darken(tint, 0.05))
    else:
        hi, lo = col(lighten(tint, 0.35)), col(lighten(tint, 0.12))
    tv = np.clip((ys - top) / max(1.0, bottom - top), 0, 1)[..., None]
    c.over(view, hi * (1 - tv) + lo * tv, coverage * alpha)


def _grow(box: Box, px: float) -> Box:
    return (box[0] - px, box[1] - px, box[2] + px, box[3] + px)


# ====================================================== stock-widget chrome ==


@dataclass(frozen=True)
class Panel(Item):
    """A bordered widget: glass, a gradient border in its own colour, title
    notches with small ticks, and a brighter border with a halo when focused.

    The border runs through the middle of the outer row and column, so Textual's
    border title sits in the notch. ``edges`` are the styles of the top, right,
    bottom and left sides (``LINE``, ``HEAVY``, ``BAR`` or ``NONE``); with fewer
    than three the panel draws single edges (a toast's accent bar, a rule under
    a heading). ``fill`` is the glass tint (None: border only). ``titles`` and
    ``subtitles`` are the cell spans of the border labels. ``strength`` scales
    the border's brightness.
    """

    rect: Rect
    edges: tuple[str, str, str, str]  # top, right, bottom, left
    c1: Colour
    c2: Colour
    fill: Colour | None
    fill_alpha: float = 0.74
    titles: tuple[tuple[float, float], ...] = ()
    subtitles: tuple[tuple[float, float], ...] = ()
    focused: bool = False
    dialog: bool = False
    dark: bool = True
    strength: float = 1.0
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    #: How far the inner focus glow reaches, in rows (it is gone by then, so a
    #: focus change redraws four strips, not the whole interior).
    INNER: ClassVar[float] = 1.6

    @property
    def full(self) -> bool:
        return sum(1 for e in self.edges if e) >= 3

    def geometry(self, g: Grid) -> tuple[float, float, float, float, float]:
        x, y, w, h = self.rect
        has_top, has_right, has_bottom, has_left = self.edges
        left = (x + (0.5 if has_left else 0.1)) * g.sx
        right = (x + w - (0.5 if has_right else 0.1)) * g.sx
        top = (y + (0.5 if has_top else 0.08)) * g.sy
        bottom = (y + h - (0.5 if has_bottom else 0.08)) * g.sy
        rad = (0.55 if self.full else 0.3) * g.sy
        rad = max(0.0, min(rad, (right - left) / 2, (bottom - top) / 2))
        return left, top, right, bottom, rad

    def reach(self, g: Grid) -> float:
        """How far the halo (and a bar's glow) reaches outside the border."""
        return (1.6 if self.dialog else 1.25) * g.sy

    def bbox(self, g: Grid) -> Box:
        left, top, right, bottom, _ = self.geometry(g)
        m = self.reach(g) + self.margin(g)
        return (left - m, top - m, right + m, bottom + m + 0.4 * g.sy)

    def diff_boxes(self, old: Item, g: Grid) -> list[Box] | None:
        if not isinstance(old, Panel):
            return None
        fixed = (self.rect, self.edges, self.fill, self.fill_alpha, self.dialog, self.dark, self.depth, self.clip)
        if fixed != (old.rect, old.edges, old.fill, old.fill_alpha, old.dialog, old.dark, old.depth, old.clip):
            return None
        left, top, right, bottom, _ = self.geometry(g)
        if (self.focused, self.c1, self.c2, self.strength) != (old.focused, old.c1, old.c2, old.strength):
            # the border, its halo and the inner glow: four strips, not the whole interior
            out = self.reach(g) + 2
            inner = self.INNER * g.sy + 0.4 * g.sx + 3
            boxes = [(left - out, top - out, right + out, top + inner),
                     (left - out, bottom - inner, right + out, bottom + out),
                     (left - out, top + inner, left + inner, bottom - inner),
                     (right - inner, top + inner, right + out, bottom - inner)]
            return [b for b in boxes if b[2] > b[0] and b[3] > b[1]]
        boxes = []
        for new_spans, old_spans, edge_y in ((self.titles, old.titles, top), (self.subtitles, old.subtitles, bottom)):
            for x0, x1 in sorted(set(new_spans) ^ set(old_spans)):
                boxes.append(((x0 - 0.5) * g.sx - 3, edge_y - 0.6 * g.sy - 2, (x1 + 0.5) * g.sx + 3,
                              edge_y + 0.6 * g.sy + 2))
        return boxes

    def draw_shadow(self, c: Canvas, g: Grid) -> None:
        left, top, right, bottom, rad = self.geometry(g)
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        off = (0.45 if self.dialog else 0.32) * g.sy
        d = sd_rrect(xs, ys, (left + right) / 2, (top + bottom) / 2 + off, (right - left) / 2, (bottom - top) / 2, rad)
        strength = (0.6 if self.dialog else 0.4) if self.dark else (0.28 if self.dialog else 0.16)
        spread = (1.5 if self.dialog else 1.0) * g.sy
        c.over(view, BLACK, strength * (1 - smoothstep(-0.7 * g.sy, spread, d)))

    def draw(self, c: Canvas, g: Grid) -> None:
        left, top, right, bottom, rad = self.geometry(g)
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        sy = g.sy
        cx, cy, hw, hh = (left + right) / 2, (top + bottom) / 2, (right - left) / 2, (bottom - top) / 2
        d = sd_rrect(xs, ys, cx, cy, hw, hh, rad)
        inside = np.clip(0.5 - d, 0, 1)
        border = x_gradient(xs, left, right, self.c1, self.c2)
        if self.fill is not None:
            _glass(c, view, ys, top, bottom, rgb_of(self.fill), inside, self.fill_alpha, self.dark)
            # a faint sheen from the top-left, and a highlight just inside the top edge
            sheen = smoothstep(0.6, 0.0, ((xs - left) + (ys - top) * 1.8) / max(1.0, right - left))
            c.glow(view, WHITE, sheen * inside * (0.035 if self.dark else 0.12))
            hl = np.clip(1.2 - np.abs(d + 1.6), 0, 1) * smoothstep(cy, top, ys)
            c.glow(view, WHITE, hl * (0.07 if self.dark else 0.2))
        if self.focused or self.dialog:
            outside = np.maximum(d, 0)
            reach = self.reach(g)
            halo = (np.exp(-outside / (0.45 * sy)) * smoothstep(reach, 0.5 * reach, outside)
                    * (d > -1.0) * (0.3 if self.focused else 0.16))
            if self.focused:
                depth = np.maximum(-d, 0)
                inner = np.exp(-depth / (0.45 * sy)) * smoothstep(self.INNER * sy, self.INNER * sy * 0.62, depth)
                halo = halo + inner * (d <= 0) * 0.06
            c.glow(view, border, halo * self.strength)
        if self.full:
            self._draw_ring(c, g, view, xs, ys, d, border, (cx, cy, hw, hh), top, bottom)
        else:
            self._draw_edges(c, g, view, xs, ys, border, (left, top, right, bottom))

    def _strength(self) -> float:
        return (1.0 if self.focused else (0.85 if self.dialog else 0.62)) * self.strength

    def _line_width(self, sx: float, style: str) -> float:
        if style == BAR:
            return max(2.0, 0.34 * sx)
        base = max(1.0, 0.11 * sx)
        return base * (1.7 if style == HEAVY else 1.0) * (1.35 if self.focused else 1.0)

    def _draw_ring(self, c: Canvas, g: Grid, view: Array, xs: Array, ys: Array, d: Array, border: Array,
                   centre: tuple[float, float, float, float], top: float, bottom: float) -> None:
        cx, cy, hw, hh = centre
        style = max(self.edges, key=lambda e: (e == BAR, e == HEAVY, e == LINE))
        lw = self._line_width(g.sx, style)
        band = np.clip(lw / 2 + 0.5 - np.abs(d), 0, 1)
        # gaps in the top and bottom lines where Textual draws the title texts
        for spans, edge_y in ((self.titles, top), (self.subtitles, bottom)):
            for x0, x1 in spans:
                g0, g1 = x0 * g.sx + 1, x1 * g.sx - 1
                gap = (smoothstep(g0 - 2, g0 + 1, xs) * smoothstep(g1 + 2, g1 - 1, xs)
                       * (np.abs(ys - edge_y) < 0.6 * g.sy))
                band = band * (1 - gap)
                for gx in (g0 - 1.5, g1 + 1.5):  # small ticks either side of the title
                    tick = np.clip(lw / 2 + 0.5 - np.abs(xs - gx), 0, 1) * (np.abs(ys - edge_y) < 0.28 * g.sy)
                    band = np.maximum(band, tick)
        corner = ((np.abs(xs - cx) > hw - 2.6 * g.sx) & (np.abs(ys - cy) > hh - 1.1 * g.sy)).astype(F32)
        strength = self._strength() + corner * (0.0 if self.focused else 0.22 * self.strength)
        c.over(view, border, np.clip(band * strength, 0, 1))
        if self.focused:  # a thin bright core on the line
            core = np.clip(lw / 2 - np.abs(d), 0, 1)
            c.glow(view, WHITE, core * 0.18 * self.strength)

    def _draw_edges(self, c: Canvas, g: Grid, view: Array, xs: Array, ys: Array, border: Array,
                    box: tuple[float, float, float, float]) -> None:
        """Single edges (e.g. a toast's accent bar, or a rule under a heading)."""
        left, top, right, bottom = box
        edge_top, edge_right, edge_bottom, edge_left = self.edges
        strength = self._strength()
        for style, vertical, pos, a0, a1 in (
            (edge_top, False, top, left, right), (edge_bottom, False, bottom, left, right),
            (edge_left, True, left, top, bottom), (edge_right, True, right, top, bottom),
        ):
            if not style:
                continue
            lw = self._line_width(g.sx, style)
            along, across = (ys, xs) if vertical else (xs, ys)
            half, mid = (a1 - a0) / 2, (a0 + a1) / 2
            # a capsule along the edge; thin lines fade out over their last cells
            dist_across = np.abs(across - pos)
            dist_along = np.maximum(np.abs(along - mid) - (half - lw / 2), 0)
            dd = np.sqrt(dist_across ** 2 + dist_along ** 2)
            band = np.clip(lw / 2 + 0.5 - dd, 0, 1)
            if style != BAR:
                band = band * smoothstep(half, half - 3 * (g.sy if vertical else g.sx), np.abs(along - mid))
            c.over(view, border, band * strength)
            if style == BAR:
                out = np.maximum(dd - lw / 2, 0)
                c.glow(view, border, np.exp(-out / (0.45 * g.sx)) * smoothstep(1.2 * g.sy, 0.5 * g.sy, out)
                       * (dd > lw / 2) * 0.22 * self.strength)


@dataclass(frozen=True)
class Surface(Item):
    """An unbordered widget with its own neutral background: a soft glass sheet."""

    rect: Rect
    color: Colour
    alpha: float = 0.55
    radius: float = 0.35
    dialog: bool = False
    rim: Colour | None = None
    dark: bool = True
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def geometry(self, g: Grid) -> tuple[float, float, float, float, float]:
        x, y, w, h = self.rect
        left, right = x * g.sx + 0.5, (x + w) * g.sx - 0.5
        top, bottom = y * g.sy + 0.5, (y + h) * g.sy - 0.5
        rad = max(0.0, min(self.radius * g.sy, (right - left) / 2, (bottom - top) / 2))
        return left, top, right, bottom, rad

    def bbox(self, g: Grid) -> Box:
        left, top, right, bottom, _ = self.geometry(g)
        m = (1.5 * g.sy if self.dialog else 0.5) + self.margin(g)  # a dialog's rim glow, or the sheet's edge
        return (left - m, top - m, right + m, bottom + m + (0.45 * g.sy if self.dialog else 0))  # + shadow

    def draw_shadow(self, c: Canvas, g: Grid) -> None:
        if not self.dialog:
            return
        left, top, right, bottom, rad = self.geometry(g)
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        d = sd_rrect(xs, ys, (left + right) / 2, (top + bottom) / 2 + 0.45 * g.sy, (right - left) / 2,
                     (bottom - top) / 2, rad)
        c.over(view, BLACK, (0.6 if self.dark else 0.28) * (1 - smoothstep(-0.7 * g.sy, 1.5 * g.sy, d)))

    def draw(self, c: Canvas, g: Grid) -> None:
        left, top, right, bottom, rad = self.geometry(g)
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        d = sd_rrect(xs, ys, (left + right) / 2, (top + bottom) / 2, (right - left) / 2, (bottom - top) / 2, rad)
        inside = np.clip(0.5 - d, 0, 1)
        _glass(c, view, ys, top, bottom, rgb_of(self.color), inside, self.alpha, self.dark)
        hl = np.clip(1.2 - np.abs(d + 1.2), 0, 1) * smoothstep((top + bottom) / 2, top, ys)
        c.glow(view, WHITE, hl * (0.05 if self.dark else 0.15))
        if self.rim is not None:
            rim = col(self.rim)
            c.over(view, rim, np.clip(1.0 - np.abs(d + 0.5), 0, 1) * 0.55)
            if self.dialog:
                out = np.maximum(d, 0)
                fade = smoothstep(1.5 * g.sy, 0.7 * g.sy, out)
                c.glow(view, rim, np.exp(-out / (0.5 * g.sy)) * fade * (d > 0) * 0.18)


@dataclass(frozen=True)
class HeaderStrip(Item):
    """A DataTable header: a slightly brighter band with a gradient underline."""

    rect: Rect
    color: Colour
    c1: Colour
    c2: Colour
    dark: bool = True
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def geometry(self, g: Grid) -> tuple[float, float, float, float]:
        x, y, w, h = self.rect
        return (x + 0.15) * g.sx, y * g.sy + 1, (x + w - 0.15) * g.sx, (y + h) * g.sy - 1

    def bbox(self, g: Grid) -> Box:
        return _grow(self.geometry(g), 0.5 + self.margin(g))

    def draw(self, c: Canvas, g: Grid) -> None:
        left, top, right, bottom = self.geometry(g)
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        d = sd_rrect(xs, ys, (left + right) / 2, (top + bottom) / 2, (right - left) / 2, (bottom - top) / 2,
                     min(0.35 * g.sy, (bottom - top) / 2))
        inside = np.clip(0.5 - d, 0, 1)
        tint = rgb_of(self.color)
        band = col(lighten(tint, 0.05) if self.dark else darken(tint, 0.04))
        c.over(view, band, inside * 0.55)
        u = np.clip((xs - left) / max(1.0, right - left), 0, 1)
        fade = np.clip(np.sin(u * math.pi), 0, 1) ** 0.5
        line = np.clip(1.0 - np.abs(ys - (bottom - 0.5)), 0, 1) * fade
        c.over(view, x_gradient(xs, left, right, self.c1, self.c2), line * 0.55)


@dataclass(frozen=True)
class Rows(Item):
    """Zebra stripes: very faint bands behind alternate rows."""

    dynamic: ClassVar[bool] = True
    edge: ClassVar[float] = 0.0  # each band ends a pixel inside its row; no fade needed

    x: int
    w: int
    rows: tuple[tuple[int, int], ...]  # (y, height) in cells
    color: Colour
    alpha: float
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def bbox(self, g: Grid) -> Box:
        if not self.rows:
            return (0, 0, 0, 0)
        y0 = min(y for y, _ in self.rows)
        y1 = max(y + h for y, h in self.rows)
        return g.box((self.x, y0, self.w, y1 - y0))

    def draw(self, c: Canvas, g: Grid) -> None:
        tone = col(self.color)
        for y, h in self.rows:
            box = ((self.x + 0.3) * g.sx, y * g.sy + 1, (self.x + self.w - 0.3) * g.sx, (y + h) * g.sy - 1)
            win = c.window(box)
            if win is None:
                continue
            view, xs, ys = win
            d = sd_rrect(xs, ys, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, (box[2] - box[0]) / 2,
                         (box[3] - box[1]) / 2, min(0.3 * g.sy, (box[3] - box[1]) / 2))
            c.over(view, tone, np.clip(0.5 - d, 0, 1) * self.alpha)


@dataclass(frozen=True)
class Pill(Item):
    """The glowing selection pill behind a cursor row or highlighted option."""

    dynamic: ClassVar[bool] = True

    rect: FRect  # cells; y may be fractional while it slides
    color: Colour
    focused: bool = True
    solid: bool = False  # the cursor's text colour needs the cursor colour behind it
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def geometry(self, g: Grid) -> tuple[float, float, float, float]:
        x, y, w, h = self.rect
        return (x + 0.2) * g.sx, (y + 0.06) * g.sy, (x + w - 0.2) * g.sx, (y + h - 0.06) * g.sy

    def bbox(self, g: Grid) -> Box:
        return _grow(self.geometry(g), 0.6 * g.sy + self.margin(g))  # the focus glow ends at 0.6 rows

    def draw(self, c: Canvas, g: Grid) -> None:
        left, top, right, bottom = self.geometry(g)
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        rad = min(0.45 * g.sy, (bottom - top) / 2)
        d = sd_rrect(xs, ys, (left + right) / 2, (top + bottom) / 2, (right - left) / 2, (bottom - top) / 2, rad)
        a = col(self.color)
        s = 1.0 if self.focused else 0.5
        u = np.clip((xs - left) / max(1.0, right - left), 0, 1)
        inside = np.clip(0.5 - d, 0, 1)
        if self.solid:
            c.over(view, a, inside * (0.94 if self.focused else 0.8))
        else:
            c.over(view, a, inside * (0.42 - 0.16 * u) * s)
        c.over(view, a * 0.55 + 0.45, np.clip(1.0 - np.abs(d + 0.5), 0, 1) * (0.55 - 0.3 * u) * s)
        # a bright bar at the left end
        bx = left + 0.45 * g.sx
        bd = sd_rrect(xs, ys, bx, (top + bottom) / 2, 0.12 * g.sx + 0.5, max(1.0, (bottom - top) / 2 - 0.22 * g.sy),
                      0.12 * g.sx + 0.5)
        c.over(view, a * 0.5 + 0.5, np.clip(0.5 - bd, 0, 1) * s)
        if self.focused:
            reach = smoothstep(0.6 * g.sy, 0.45 * g.sy, d)  # rounded, and gone before the box ends
            c.glow(view, a, np.exp(-np.maximum(d, 0) / (0.14 * g.sy)) * (d > 0) * 0.3 * (1 - 0.7 * u) * reach)


@dataclass(frozen=True)
class Badge(Item):
    """A small pill under a key in the footer."""

    rect: Rect
    color: Colour
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def geometry(self, g: Grid) -> tuple[float, float, float, float]:
        x, y, w, h = self.rect
        return (x + 0.05) * g.sx, (y + 0.1) * g.sy, (x + w - 0.05) * g.sx, (y + h - 0.1) * g.sy

    def bbox(self, g: Grid) -> Box:
        return _grow(self.geometry(g), 0.5 + self.margin(g))

    def draw(self, c: Canvas, g: Grid) -> None:
        left, top, right, bottom = self.geometry(g)
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        d = sd_rrect(xs, ys, (left + right) / 2, (top + bottom) / 2, (right - left) / 2, (bottom - top) / 2,
                     min((bottom - top) / 2, (right - left) / 2))
        a = col(self.color)
        c.over(view, a, np.clip(0.5 - d, 0, 1) * 0.2)
        c.over(view, a, np.clip(1.0 - np.abs(d + 0.5), 0, 1) * 0.5)


@dataclass(frozen=True)
class Progress(Item):
    """A smooth glowing progress bar."""

    dynamic: ClassVar[bool] = True

    rect: Rect
    fraction: float
    c1: Colour
    c2: Colour
    dark: bool = True
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def geometry(self, g: Grid) -> tuple[float, float, float, float]:
        """Left, right, centre line and half height of the bar."""
        x, y, w, h = self.rect
        return (x + 0.15) * g.sx, (x + w - 0.15) * g.sx, (y + h / 2) * g.sy, max(1.5, 0.2 * g.sy)

    def bbox(self, g: Grid) -> Box:
        left, right, cy, hh = self.geometry(g)
        m = 0.65 * g.sy + self.margin(g)  # where the glow ends
        return (left - hh - m, cy - hh - m, right + m, cy + hh + m)

    def draw(self, c: Canvas, g: Grid) -> None:
        left, right, cy, hh = self.geometry(g)
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        track = sd_rrect(xs, ys, (left + right) / 2, cy, (right - left) / 2, hh, hh)
        c.over(view, WHITE if self.dark else BLACK, np.clip(0.5 - track, 0, 1) * 0.1)
        frac = min(1.0, max(0.0, self.fraction))
        if frac <= 0:
            return
        end = left + (right - left) * frac
        fill = sd_rrect(xs, ys, (left + end) / 2, cy, max(hh, (end - left) / 2), hh, hh)
        grad = x_gradient(xs, left, right, self.c1, self.c2)
        c.over(view, grad, np.clip(0.5 - fill, 0, 1))
        reach = smoothstep(0.65 * g.sy, 0.35 * g.sy, fill)
        c.glow(view, grad, np.exp(-np.maximum(fill, 0) / (0.12 * g.sy)) * (fill > 0) * 0.35 * reach)
        core = np.clip(0.5 - np.abs(ys - (cy - hh * 0.35)), 0, 1) * np.clip(0.5 - fill, 0, 1)
        c.glow(view, WHITE, core * 0.25)


@dataclass(frozen=True)
class Thumb(Item):
    """A slim rounded scrollbar thumb on a faint track.

    Its box is the scrollbar's cells, which leave no room for the safety fade,
    so the thumb's glow carries its own fade to the edges of the bar instead.
    """

    dynamic: ClassVar[bool] = True
    edge: ClassVar[float] = 0.0

    rect: Rect  # the scrollbar's cells
    start: float  # thumb position along the bar, in cells
    length: float
    vertical: bool
    color: Colour
    active: bool = False
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def bbox(self, g: Grid) -> Box:
        return g.box(self.rect)

    def draw(self, c: Canvas, g: Grid) -> None:
        x, y, w, h = self.rect
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        a = col(self.color)
        if self.vertical:
            cx = (x + w / 2) * g.sx
            half = max(1.2, 0.17 * g.sx * w) * (1.35 if self.active else 1.0)
            t0, t1 = (y + self.start) * g.sy + 2, (y + self.start + self.length) * g.sy - 2
            track = sd_rrect(xs, ys, cx, (y + h / 2) * g.sy, half * 0.6, h * g.sy / 2 - 2, half * 0.6)
            thumb = sd_rrect(xs, ys, cx, (t0 + t1) / 2, half, max(half, (t1 - t0) / 2), half)
        else:
            cy = (y + h / 2) * g.sy
            half = max(1.2, 0.12 * g.sy * h) * (1.35 if self.active else 1.0)
            t0, t1 = (x + self.start) * g.sx + 2, (x + self.start + self.length) * g.sx - 2
            track = sd_rrect(xs, ys, (x + w / 2) * g.sx, cy, w * g.sx / 2 - 2, half * 0.6, half * 0.6)
            thumb = sd_rrect(xs, ys, (t0 + t1) / 2, cy, max(half, (t1 - t0) / 2), half, half)
        c.over(view, a, np.clip(0.5 - track, 0, 1) * 0.12)
        c.over(view, a, np.clip(0.5 - thumb, 0, 1) * (0.95 if self.active else 0.75))
        fade = box_fade(xs, ys, self.bbox(g), 1.5)
        c.glow(view, a, np.exp(-np.maximum(thumb, 0) / 2.0) * (thumb > 0) * 0.2 * fade)


# ============================================================ instruments ==


@dataclass(frozen=True)
class HeaderBar(Item):
    """A header strip: a darker band and a gradient hairline with a glow."""

    rect: FRect
    c1: Colour
    c2: Colour
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def bbox(self, g: Grid) -> Box:
        return g.box(self.rect, 0.0, 0.5)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        x, y, w, h = self.rect
        line_y = (y + h) * g.sy - 1.0
        u = (xs - x * g.sx) / max(1.0, w * g.sx)
        fade = np.sin(np.clip(u, 0, 1) * math.pi) ** 0.6
        c.over(view, col("#050813"), 0.4 * (ys < line_y))
        band = np.clip(0.9 - np.abs(ys - line_y), 0, 1)
        grad = col(self.c1) * (1 - u[..., None]) + col(self.c2) * u[..., None]
        c.over(view, grad, band * fade * 0.8)
        reach = smoothstep(0.5 * g.sy - 1, 0.2 * g.sy, np.abs(ys - line_y))  # zero before the box ends
        c.glow(view, grad, np.exp(-np.abs(ys - line_y) / (0.25 * g.sy)) * fade * 0.18 * reach)


@dataclass(frozen=True)
class Rule(Item):
    """A faint horizontal hairline (section separators), fading at both ends."""

    edge: ClassVar[float] = 0.0  # it fades along its length by itself, and is thinner than its box

    rect: FRect  # the line runs through the vertical middle
    color: Colour
    alpha: float = 0.35
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def bbox(self, g: Grid) -> Box:
        return g.box(self.rect)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        x, y, w, h = self.rect
        cy = (y + h / 2) * g.sy
        u = np.clip((xs - x * g.sx) / max(1.0, w * g.sx), 0, 1)
        fade = smoothstep(0.0, 0.08, u) * smoothstep(1.0, 0.75, u)
        c.over(view, col(self.color), np.clip(0.8 - np.abs(ys - cy), 0, 1) * fade * self.alpha)


@dataclass(frozen=True)
class Ring(Item):
    """A ring gauge (270 degrees, gap at the bottom) or a full progress ring.

    The number inside is Textual text; this draws the track, the lit arc with
    a glow, tick marks and a bright end cap.
    """

    dynamic: ClassVar[bool] = True

    rect: FRect
    value: float
    c1: Colour
    c2: Colour
    sweep: float = 0.75  # fraction of the circle the gauge spans
    ticks: int = 10
    thickness: float = 0.17  # fraction of the radius
    scale: float = 0.78  # radius as a fraction of the rect's half-size
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def bbox(self, g: Grid) -> Box:
        return g.box(self.rect)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        x, y, w, h = self.rect
        cx, cy = (x + w / 2) * g.sx, (y + h / 2) * g.sy
        R = min(w * g.sx, h * g.sy) / 2 * self.scale
        if R < 3:
            return
        T = max(2.0, R * self.thickness)
        dx, dy = xs - cx, ys - cy
        rr = np.sqrt(dx * dx + dy * dy)
        sweep = self.sweep
        start = (1 - sweep) / 2
        ang = (np.arctan2(dx, -dy) + math.pi) / (2 * math.pi)  # 0 at the bottom, clockwise
        frac = (ang - start) / sweep
        arc_len = sweep * 2 * math.pi * rr
        in_span = np.clip(np.minimum(frac, 1 - frac) * arc_len + 0.5, 0, 1) if sweep < 1 else 1.0
        ring = np.clip(T / 2 + 0.5 - np.abs(rr - R), 0, 1)
        c.over(view, WHITE, ring * in_span * 0.1)
        value = float(np.clip(self.value, 0, 1))
        t = np.clip(frac, 0, 1)[..., None]
        grad = col(self.c1) * (1 - t) + col(self.c2) * t
        lit = np.clip((value - frac) * arc_len + 0.5, 0, 1) * (frac > -0.02)
        if value <= 0:
            lit = lit * 0
        c.over(view, grad, ring * lit)
        glow = np.exp(-np.abs(rr - R) / (T * 0.55)) * np.clip(1 - np.abs(rr - R) / (0.3 * R), 0, 1) * lit * 0.45
        c.glow(view, grad, glow)
        if self.ticks:
            tick_pos = (frac * self.ticks) % 1
            ticks = (np.abs(tick_pos - 0.5) > 0.5 - 0.012 * self.ticks) * (frac >= -0.01) * (frac <= 1.01)
            tband = np.clip(1.0 - np.abs(rr - (R + T * 1.25)) / (T * 0.35), 0, 1)
            c.over(view, WHITE, ticks * tband * 0.22)
        if value > 0:
            ea = (start + sweep * value) * 2 * math.pi - math.pi
            ex, ey = cx + R * math.sin(ea), cy - R * math.cos(ea)
            ed = np.sqrt((xs - ex) ** 2 + (ys - ey) ** 2)
            end = col(self.c2)
            c.over(view, end * 0.5 + 0.5, np.clip(T * 0.55 + 0.5 - ed, 0, 1))
            c.glow(view, end, np.exp(-ed / (T * 0.8)) * np.clip(1 - ed / (0.35 * R), 0, 1) * 0.45)


@dataclass(frozen=True)
class Signal(Item):
    """A signal-strength meter for a 0-10 score: five rising bars, two points
    each; an odd score half-lights the next bar."""

    dynamic: ClassVar[bool] = True
    edge: ClassVar[float] = 0.15

    rect: FRect
    score: float
    color: Colour
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def bbox(self, g: Grid) -> Box:
        return g.box(self.rect, 0.3, 0.0)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        x, y, w, h = self.rect
        left, right = (x + 0.15) * g.sx, (x + w - 0.1) * g.sx
        bottom, top = (y + h - 0.2) * g.sy, (y + 0.2) * g.sy
        n = 5
        pitch = (right - left) / n
        bw = max(1.5, pitch * 0.62)
        a = col(self.color)
        for k in range(n):
            bx0 = left + k * pitch
            bh = (bottom - top) * (0.3 + 0.7 * (k + 1) / n)
            d = sd_rrect(xs, ys, bx0 + bw / 2, bottom - bh / 2, bw / 2, bh / 2, min(1.0, bw / 3))
            cover = np.clip(0.5 - d, 0, 1)
            level = self.score - 2 * k
            if level >= 2:
                c.over(view, a, cover * 0.95)
                reach = smoothstep(0.2 * g.sy - 1, 0.06 * g.sy, d)
                c.glow(view, a, np.exp(-np.maximum(d, 0) / (0.12 * g.sy)) * (d > 0) * 0.22 * reach)
            elif level >= 1:
                c.over(view, a, cover * 0.45)
            else:
                c.over(view, WHITE, cover * 0.1)


@dataclass(frozen=True)
class Meter(Item):
    """A slim glowing bar for a fraction (progress, days in status...)."""

    dynamic: ClassVar[bool] = True
    edge: ClassVar[float] = 0.15

    rect: FRect
    fraction: float
    c1: Colour
    c2: Colour
    track: bool = True
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def bbox(self, g: Grid) -> Box:
        return g.box(self.rect, 0.7, 0.3)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        x, y, w, h = self.rect
        cy = (y + h / 2) * g.sy
        hh = max(1.5, 0.16 * g.sy)
        left, right = (x + 0.1) * g.sx, (x + w - 0.1) * g.sx
        if self.track:
            track = sd_rrect(xs, ys, (left + right) / 2, cy, (right - left) / 2, hh * 0.7, hh * 0.7)
            c.over(view, WHITE, np.clip(0.5 - track, 0, 1) * 0.09)
        frac = min(1.0, max(0.0, self.fraction))
        if frac <= 0:
            return
        end = left + max(2 * hh, (right - left) * frac)
        fill = sd_rrect(xs, ys, (left + end) / 2, cy, (end - left) / 2, hh, hh)
        grad = x_gradient(xs, left, right, self.c1, self.c2)
        c.over(view, grad, np.clip(0.5 - fill, 0, 1))
        reach = smoothstep(0.3 * g.sy, 0.1 * g.sy, fill)
        c.glow(view, grad, np.exp(-np.maximum(fill, 0) / (0.13 * g.sy)) * (fill > 0) * 0.3 * reach)
        core = np.clip(0.5 - np.abs(ys - (cy - hh * 0.35)), 0, 1) * np.clip(0.5 - fill, 0, 1)
        c.glow(view, WHITE, core * 0.22)


@dataclass(frozen=True)
class Area(Item):
    """A glowing area chart, values 0..1, oldest first."""

    dynamic: ClassVar[bool] = True

    rect: FRect
    values: tuple[float, ...]
    c1: Colour
    c2: Colour
    grid_lines: int = 3
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def bbox(self, g: Grid) -> Box:
        return g.box(self.rect)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        if win is None or len(self.values) < 2:
            return
        view, xs, ys = win
        x, y, w, h = self.rect
        sx, sy = g.sx, g.sy
        left, right = (x + 0.5) * sx, (x + w - 0.5) * sx
        top, bottom = (y + 0.4) * sy, (y + h - 0.3) * sy
        for k in range(1, self.grid_lines + 1):
            gy = top + (bottom - top) * k / (self.grid_lines + 1)
            dash = ((xs // max(2, int(sx * 0.6))) % 2 == 0) & (xs > left) & (xs < right)
            c.over(view, WHITE, np.clip(1 - np.abs(ys - gy), 0, 1) * dash * 0.06)
        c.over(view, WHITE, np.clip(1 - np.abs(ys - bottom), 0, 1) * (xs > left) * (xs < right) * 0.16)
        n = len(self.values)
        px_ = left + (right - left) * np.arange(n, dtype=F32) / (n - 1)
        py_ = bottom - (bottom - top) * np.clip(np.asarray(self.values, F32), 0, 1)
        line = np.interp(xs[0], px_, py_).astype(F32)[None, :]
        l0 = np.interp(xs[0] - 0.5, px_, py_).astype(F32)[None, :]
        l1 = np.interp(xs[0] + 0.5, px_, py_).astype(F32)[None, :]
        lo, hi = np.minimum(l0, l1), np.maximum(l0, l1)
        inx = ((xs >= left) & (xs <= right)).astype(F32)
        grad = x_gradient(xs, left, right, self.c1, self.c2)
        below = np.clip(ys - line + 0.5, 0, 1) * (ys <= bottom) * inx
        fill = smoothstep(bottom, top, ys) * 0.4 + 0.04
        c.over(view, grad, below * fill)
        d = np.maximum(0, np.maximum(lo - ys, ys - hi))
        lw = max(1.2, 0.15 * sx)
        c.over(view, grad * 0.6 + 0.4, np.clip(lw / 2 + 0.5 - d, 0, 1) * inx)
        c.glow(view, grad, np.exp(-d / (0.3 * sy)) * 0.3 * inx * smoothstep(0.4 * sy - 1, 0.15 * sy, d))
        ed = np.sqrt((xs - px_[-1]) ** 2 + (ys - py_[-1]) ** 2)
        c.over(view, WHITE, np.clip(lw * 1.6 + 0.5 - ed, 0, 1))
        c.glow(view, col(self.c2), np.exp(-ed / (0.4 * sy)) * 0.5 * smoothstep(0.5 * sx - 1, 0.2 * sx, ed))


@dataclass(frozen=True)
class Bars(Item):
    """Glowing per-day bars, with last period's values as faint ghost outlines.

    ``values`` and ``ghost`` are raw counts; both share one scale, ``scale_max``
    (0: max(1, the largest value or ghost)). ``today`` is the index of the
    current day (drawn brighter), or -1.
    """

    dynamic: ClassVar[bool] = True

    rect: FRect
    values: tuple[float, ...]
    c1: Colour
    c2: Colour
    ghost: tuple[float, ...] = ()
    today: int = -1
    scale_max: float = 0.0  # 0: max(1, the largest value or ghost)
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def bbox(self, g: Grid) -> Box:
        return g.box(self.rect)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        n = len(self.values)
        if win is None or n == 0:
            return
        view, xs, ys = win
        x, y, w, h = self.rect
        sx, sy = g.sx, g.sy
        left, right = (x + 0.3) * sx, (x + w - 0.3) * sx
        top, bottom = (y + 0.25) * sy, (y + h - 0.3) * sy
        peak = self.scale_max or max([1.0, *self.values, *self.ghost])
        pitch = (right - left) / n
        bw = max(2.0, min(pitch * 0.58, 2.2 * sx))
        c.over(view, WHITE, np.clip(1 - np.abs(ys - bottom), 0, 1) * (xs > left) * (xs < right) * 0.14)
        for k in range(n):
            cx = left + (k + 0.5) * pitch
            t = k / max(1, n - 1)
            colour = col(self.c1) * (1 - t) + col(self.c2) * t
            if k < len(self.ghost) and self.ghost[k] > 0:
                gh = (bottom - top) * min(1.0, self.ghost[k] / peak)
                d = sd_rrect(xs, ys, cx, bottom - gh / 2, bw / 2 + 1, gh / 2, min(2.0, bw / 3))
                c.over(view, WHITE, np.clip(1.0 - np.abs(d + 0.5), 0, 1) * 0.22)
            v = self.values[k]
            if v <= 0:
                c.over(view, colour, np.clip(1.2 - np.abs(ys - (bottom - 1.0)), 0, 1)
                       * (np.abs(xs - cx) < bw / 2) * 0.5)
                continue
            bh = max(2.0, (bottom - top) * min(1.0, v / peak))
            d = sd_rrect(xs, ys, cx, bottom - bh / 2, bw / 2, bh / 2, min(2.0, bw / 3))
            cover = np.clip(0.5 - d, 0, 1)
            vt = np.clip((bottom - ys) / max(1.0, bh), 0, 1)[..., None]
            bright = 1.0 if (self.today < 0 or k == self.today) else 0.72
            c.over(view, colour * (0.55 + 0.45 * vt), cover * bright)
            reach = smoothstep(0.25 * sy - 1, 0.08 * sy, d)
            c.glow(view, colour, np.exp(-np.maximum(d, 0) / (0.22 * sy)) * (d > 0) * 0.28 * bright * reach)
            cap = np.clip(1.2 - np.abs(ys - (bottom - bh + 1.0)), 0, 1) * (np.abs(xs - cx) < bw / 2 - 0.5)
            c.glow(view, WHITE, cap * 0.35 * bright)


@dataclass(frozen=True)
class Lane(Item):
    """One stage of a track: a faint guide line that brightens towards the
    right, ending in a chevron that points at the next stage."""

    edge: ClassVar[float] = 0.0  # the box is padded instead (the line starts at the rect)

    rect: FRect  # the line runs through the vertical middle
    color: Colour
    chevron: bool = True
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def bbox(self, g: Grid) -> Box:
        return g.box(self.rect, 0.3, 0.0)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        x, y, w, h = self.rect
        cy = (y + h / 2) * g.sy
        left, right = x * g.sx, (x + w - 0.6) * g.sx
        u = np.clip((xs - left) / max(1.0, right - left), 0, 1)
        a = col(self.color)
        dash = ((xs // max(2.0, g.sx * 0.5)) % 2 == 0).astype(F32) * 0.6 + 0.4
        line = np.clip(0.8 - np.abs(ys - cy), 0, 1) * (xs >= left) * (xs <= right) * dash
        c.over(view, a, line * (0.12 + 0.2 * u))
        if self.chevron:
            tip = right + 0.45 * g.sx
            arm = 0.32 * g.sy
            dd = np.abs(np.abs(ys - cy) - (tip - xs) * (arm / (0.4 * g.sx)))
            chev = np.clip(1.0 - dd, 0, 1) * (xs <= tip) * (xs >= tip - 0.45 * g.sx)
            c.over(view, a, chev * 0.5)


@dataclass(frozen=True)
class Dot(Item):
    """A glowing dot with an optional comet trail to its left (trail length
    encodes age). ``ring`` draws a selection ring around it."""

    dynamic: ClassVar[bool] = True

    x: float  # centre, in cells
    y: float
    color: Colour
    trail: float = 0.0  # cells
    radius: float = 0.32  # in rows
    ring: bool = False
    hollow: bool = False
    body: bool = True  # False: just the trail (a sprite draws the dot)
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def bbox(self, g: Grid) -> Box:
        r = (self.radius + 0.7) * g.sy
        return (self.x * g.sx - r - self.trail * g.sx, self.y * g.sy - r, self.x * g.sx + r, self.y * g.sy + r)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        cx, cy = self.x * g.sx, self.y * g.sy
        r = self.radius * g.sy
        a = col(self.color)
        if self.trail > 0:
            tl = self.trail * g.sx
            along = np.clip((cx - xs) / tl, 0, 1)
            width = r * (1 - along) * 0.75 + 0.6
            tr = np.clip(width - np.abs(ys - cy), 0, 1) * (xs <= cx) * (xs >= cx - tl) * (1 - along) ** 1.6
            c.glow(view, a, tr * 0.5)
        d = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
        reach = smoothstep((self.radius + 0.7) * g.sy - 1, r + 0.35 * g.sy, d)  # a round glow, gone by the box
        if not self.body:
            pass
        elif self.hollow:
            c.over(view, a, np.clip(1.0 - np.abs(d - r * 0.8), 0, 1) * 0.8)
        else:
            c.over(view, a * 0.55 + 0.45, np.clip(r + 0.5 - d, 0, 1))
            c.glow(view, a, np.exp(-np.maximum(d - r, 0) / (0.25 * g.sy)) * (d > r) * 0.55 * reach)
        if self.ring:
            c.over(view, WHITE, np.clip(1.0 - np.abs(d - r * 2.0), 0, 1) * 0.85)


@dataclass(frozen=True)
class Spine(Item):
    """A vertical timeline line (for event logs)."""

    edge: ClassVar[float] = 0.0  # thinner than its box, which is padded at both ends

    x: float  # cells, centre of the line
    y0: float
    y1: float
    c1: Colour
    c2: Colour
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def bbox(self, g: Grid) -> Box:
        return (self.x * g.sx - 3, self.y0 * g.sy - 2, self.x * g.sx + 3, self.y1 * g.sy + 2)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        t = np.clip((ys - self.y0 * g.sy) / max(1.0, (self.y1 - self.y0) * g.sy), 0, 1)[..., None]
        grad = col(self.c1) * (1 - t) + col(self.c2) * t
        fade = smoothstep(self.y1 * g.sy, (self.y1 - 2) * g.sy, ys) * (ys >= self.y0 * g.sy)
        c.over(view, grad, np.clip(0.9 - np.abs(xs - self.x * g.sx), 0, 1) * 0.35 * fade)
