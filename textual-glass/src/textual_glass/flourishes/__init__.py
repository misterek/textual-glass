"""Flourishes: short celestial animations an app plays when something happens.
No Textual imports.

A flourish is a one-shot effect (a comet, a sunburst, an eclipse...) drawn
over the glass and under the text, around a widget or across the window. Glass
never plays one by itself: the app decides when (``app.glass.flourish(...)``).

Each kind is a :class:`Flourish`: a maker that draws the frames on a
:class:`Stage`, how far the stage reaches past the anchor, and the palette
roles of its two colours. The makers live in the family modules (``streaks``,
``bursts``, ``shapes``) and are collected in :data:`FLOURISHES`.

A maker gets a :class:`Stage` and returns ``(frames, gaps)``: straight-alpha
RGBA frames as bytes (``width * height * 4`` each) and how long each frame
shows, in ms. **Every frame is fully transparent at the stage's border**,
except on sides that lie on the window's edge (``Stage.window_edges``), as
for every sprite (rule 17). The frames are rendered in glass's worker thread,
uploaded once, stepped through by glass and deleted after the last one.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from ..render import RGB

#: A rectangle in screen cells, or in image pixels: x, y, width, height.
Rect = tuple[int, int, int, int]
Box = tuple[float, float, float, float]

#: Frames are rendered at most this many pixels big; the terminal stretches them over the stage.
MAX_PX = 120_000
#: At most this many frames per flourish, and at most this long in all.
MAX_FRAMES = 48
MAX_MS = 2600


@dataclass(frozen=True)
class Stage:
    """What a maker draws on. Every length is in the image's own pixels, which
    are square; the terminal stretches the image over the stage's cells."""

    width: int
    height: int
    #: Image pixels per terminal pixel (at most 1): multiply stroke widths and radii by it.
    scale: float
    #: One cell, in image pixels (width, height).
    cell: tuple[float, float]
    #: The anchor (the widget or region the app named), x, y, width, height.
    at: Box
    #: Where it goes (a comet's target), or None.
    to: Box | None
    color: RGB
    accent: RGB
    #: A dark theme: light reads as light. On a light theme, draw in colour, not white.
    dark: bool
    #: Sides of the stage on the window's edge (top, right, bottom, left): light may reach them.
    window_edges: tuple[bool, bool, bool, bool]
    #: For any randomness: the same seed gives the same frames.
    seed: int


#: ``make(stage) -> (frames, gaps_ms)``, run in the worker thread.
Maker = Callable[[Stage], tuple[list[bytes], list[int]]]


@dataclass(frozen=True)
class Flourish:
    """One kind of flourish."""

    make: Maker
    #: Cells added on each side of the anchor (and of the target) for the stage: (columns, rows).
    grow: tuple[int, int] = (4, 2)
    #: Palette roles of the two colours (``Palette`` field names: primary, secondary, accent,
    #: success, warning, error, foreground).
    color: str = "primary"
    accent: str = "secondary"
    #: The stage is the whole window, wherever the anchor is.
    screen: bool = False
    #: One line: what it could mean on a dashboard.
    about: str = ""


def _collect() -> dict[str, Flourish]:
    from . import bursts, shapes, streaks

    found: dict[str, Flourish] = {}
    for module in (streaks, bursts, shapes):
        found.update(module.KINDS)
    return found


#: Every kind, by name.
FLOURISHES: dict[str, Flourish] = _collect()


def stage_cells(kind: Flourish, at: Rect | None, to: Rect | None, screen: tuple[int, int]) -> Rect:
    """The cells a flourish covers: the anchor (and the target) grown by
    ``kind.grow``, clamped to the window; the whole window for ``kind.screen``
    or without an anchor."""
    cols, rows = screen
    if kind.screen or at is None:
        return 0, 0, cols, rows
    x0, y0, x1, y1 = at[0], at[1], at[0] + at[2], at[1] + at[3]
    if to is not None:
        x0, y0, x1, y1 = min(x0, to[0]), min(y0, to[1]), max(x1, to[0] + to[2]), max(y1, to[1] + to[3])
    gx, gy = kind.grow
    x0, y0, x1, y1 = max(0, x0 - gx), max(0, y0 - gy), min(cols, x1 + gx), min(rows, y1 + gy)
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)


def make_stage(kind: Flourish, at: Rect | None, to: Rect | None, screen: tuple[int, int],
               cell_px: tuple[float, float], color: RGB, accent: RGB, dark: bool = True, seed: int = 0,
               max_px: int = MAX_PX) -> tuple[Rect, Stage]:
    """The stage's cells and the :class:`Stage` its maker draws on."""
    cells = stage_cells(kind, at, to, screen)
    sx, sy, sw, sh = cells
    cell_w, cell_h = cell_px
    real_w, real_h = sw * cell_w, sh * cell_h
    scale = min(1.0, math.sqrt(max_px / max(1.0, real_w * real_h)))
    width, height = max(4, round(real_w * scale)), max(4, round(real_h * scale))
    cx, cy = width / sw, height / sh

    def box(r: Rect) -> Box:
        return (r[0] - sx) * cx, (r[1] - sy) * cy, r[2] * cx, r[3] * cy

    cols, rows = screen
    edges = (sy == 0, sx + sw >= cols, sy + sh >= rows, sx == 0)
    anchor = box(at) if at is not None else (0.0, 0.0, float(width), float(height))
    stage = Stage(width, height, scale, (cx, cy), anchor, box(to) if to is not None else None, color, accent, dark,
                  edges, seed)
    return cells, stage
