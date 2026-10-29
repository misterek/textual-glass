"""Drawing helpers the flourish makers share. No Textual imports.

A frame is drawn into a :class:`Light`, premultiplied colour and alpha over
the stage: ``over`` paints (a shadow, a solid core), ``add`` adds light that
adds up (glows, trails), and ``shine`` is light that reads on either theme:
added on a dark one, where bright overlaps burn white, and painted in colour
on a light one, where white light wouldn't show. Each draws into the whole
frame, a :func:`window` of it or a list of its pixels, so the cost follows
the light, not the stage. ``Light.frame()`` fades the frame out at the
stage's border (rule 17) and packs it to upload small.

The rest: where light may go (:func:`window`, :func:`room`, :func:`border`),
colours for the theme (:func:`shade`, :func:`white_hot`, :func:`mix`),
shapes (:func:`glow`, :func:`segment`), timing (:func:`ease_out`,
:func:`ease_in_out`, :func:`ramp`, :func:`bump`, :func:`timeline`,
:func:`gaps`), and :func:`coords`, :func:`centre`, :func:`rng`,
:func:`unit` and :func:`blank`.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import numpy as np

from ..render import F32, Array, Colour, col, smoothstep
from ..sprites import edge_fade

if TYPE_CHECKING:
    from . import Box, Stage

#: Where to draw: a window of the frame (rows, columns), or the stage's pixels by flat index (each once).
Where = tuple[slice, slice] | Array
#: A window of the stage: where it is, and its pixels' centres (a row of x, a column of y).
Window = tuple[tuple[slice, slice], Array, Array]

WHITE = np.ones(3, F32)


def _colour(color: Colour | Array) -> Array:
    return np.asarray(color, F32) if isinstance(color, np.ndarray) else col(color)


def _packing() -> Array:
    """Per alpha byte, the mask for a pixel's four bytes (one 32-bit word): alpha keeps 7 bits, colour as many
    as it needs to stay within about 3 levels once multiplied by its alpha. Faint light needs few (nobody can
    tell), and the fewer, the smaller the compressed upload."""
    table = np.zeros((256, 4), np.uint8)
    for alpha, bits in ((0, 2), (12, 3), (24, 4), (48, 5), (96, 6)):
        table[alpha:, :3] = 0xFF << (8 - bits) & 0xFF
    table[:, 3] = 0xFE
    return table.view(np.uint32)[:, 0]


_PACK = _packing()


class Light:
    """One frame being drawn: premultiplied colour and alpha. Each method draws into ``where``: the whole
    frame (None), a window of it, or the stage's pixels by flat index."""

    def __init__(self, stage: Stage, paint: float = 1.0) -> None:
        self.stage = stage
        #: On a light theme, how opaque ``shine`` paints the brightest light.
        self.paint = paint
        self.rgb: Array = np.zeros((stage.height, stage.width, 3), F32)
        self.a: Array = np.zeros((stage.height, stage.width), F32)

    def _planes(self, where: Where | None) -> tuple[Array, Array, Any]:
        if where is None:
            return self.rgb, self.a, ...
        if isinstance(where, tuple):
            return self.rgb, self.a, where
        return self.rgb.reshape(-1, 3), self.a.reshape(-1), where

    def over(self, color: Colour | Array, alpha: Array | float, where: Where | None = None) -> None:
        """Paint ``color`` at ``alpha`` over what is there (source over). For a shadow, paint black (or the
        theme's background)."""
        rgb, a, at = self._planes(where)
        al = np.clip(np.asarray(alpha, F32), 0, 1)
        rgb[at] = _colour(color) * al[..., None] + rgb[at] * (1 - al)[..., None]
        a[at] = al + a[at] * (1 - al)

    def add(self, color: Colour | Array, amount: Array | float, where: Where | None = None,
            cover: Array | None = None) -> None:
        """Add light, ``color`` times ``amount``: overlapping light adds up, and on a dark theme bright overlaps
        burn towards white (a comet's head, a burst's core). It covers ``amount`` (up to 1), or ``cover``: for
        light summed from several, already multiplied by its colours (``amount`` 1)."""
        rgb, a, at = self._planes(where)
        k = np.maximum(np.asarray(amount, F32), 0)
        rgb[at] += _colour(color) * k[..., None]
        a[at] = 1 - (1 - a[at]) * (1 - (np.minimum(k, 1) if cover is None else np.clip(cover, 0, 1)))

    def shine(self, color: Colour | Array, amount: Array | float, where: Where | None = None) -> None:
        """Light that reads on either theme: ``add`` on a dark one; on a light one, ``color`` painted as opaque
        as ``amount`` (up to ``paint``), since white light doesn't show on white."""
        if self.stage.dark:
            self.add(color, amount, where)
        else:
            self.over(color, np.clip(np.asarray(amount, F32), 0, 1) * F32(self.paint), where)

    def frame(self) -> bytes:
        """Straight-alpha RGBA bytes, faded out at the stage's border (:func:`border`). Only the lit pixels are
        worked on: light under 4/255 is dropped and the rest packed (:func:`_packing`), which more than halves
        the compressed upload and changes nothing anyone can see."""
        a = (self.a * border(self.stage)).reshape(-1)
        lit = np.flatnonzero(a >= 3.5 / 255)
        s = self.rgb.reshape(-1, 3)[lit] / np.maximum(self.a.reshape(-1)[lit], 1e-4)[:, None]
        if self.stage.dark:  # light beyond full brightness turns white
            peak = s.max(axis=1)
            s = s / np.maximum(peak, 1.0)[:, None] + (np.clip(peak - 1.0, 0, None) * 0.6)[:, None]
        px = np.empty((lit.size, 4), np.uint8)
        px[:, :3] = np.clip(s, 0, 1) * 255 + 0.5
        px[:, 3] = np.minimum(a[lit], 1) * 255 + 0.5
        px.view(np.uint32)[:, 0] &= _PACK[px[:, 3]]
        out = np.zeros((a.size, 4), np.uint8)
        out[lit] = px
        return out.tobytes()


def window(stage: Stage, x0: float, y0: float, x1: float, y1: float) -> Window | None:
    """The stage's pixels between ``x0, y0`` and ``x1, y1``, or None if that is off the stage."""
    i0, i1 = max(0, math.floor(y0)), min(stage.height, math.ceil(y1))
    j0, j1 = max(0, math.floor(x0)), min(stage.width, math.ceil(x1))
    if i0 >= i1 or j0 >= j1:
        return None
    xs = np.arange(j0, j1, dtype=F32)[None, :] + F32(0.5)
    ys = np.arange(i0, i1, dtype=F32)[:, None] + F32(0.5)
    return (slice(i0, i1), slice(j0, j1)), xs, ys


def coords(stage: Stage) -> tuple[Array, Array]:
    """Pixel centres: ``xs`` is a row (1, width), ``ys`` a column (height, 1)."""
    xs = np.arange(stage.width, dtype=F32)[None, :] + F32(0.5)
    ys = np.arange(stage.height, dtype=F32)[:, None] + F32(0.5)
    return xs, ys


def room(stage: Stage, margin: float, edge: float = 0.0) -> tuple[float, float, float, float]:
    """The box light may reach, x0, y0, x1, y1: ``margin`` inside the stage's sides, but only ``edge`` inside
    the sides on the window's edge, which are not faded."""
    top, right, bottom, left = stage.window_edges
    return (edge if left else margin, edge if top else margin, stage.width - (edge if right else margin),
            stage.height - (edge if bottom else margin))


def border(stage: Stage, margin: float | None = None) -> Array:
    """1 inside, 0 in the stage's outermost pixel ring, easing over ``margin`` pixels (default: about half a
    cell); sides on the window's edge are not faded. Worked out once per stage, and read-only."""
    m = margin if margin is not None else max(2.0, 0.5 * min(stage.cell))
    return _fade(stage.width, stage.height, m, stage.window_edges)


@lru_cache(maxsize=8)
def _fade(width: int, height: int, margin: float, edges: tuple[bool, bool, bool, bool]) -> Array:
    mask = edge_fade(width, height, margin, edges)
    mask.flags.writeable = False
    return mask


def shade(stage: Stage, color: Colour | Array, deep: float) -> Array:
    """A colour as drawn: as it is on a dark theme, ``deep`` times as bright on a light one, so it shows on white."""
    c = _colour(color)
    return c if stage.dark else c * F32(deep)


def white_hot(stage: Stage, color: Colour | Array, deep: float) -> Array:
    """A core's colour: white on a dark theme (it burns), ``color`` at ``deep`` times its brightness on a light one."""
    return WHITE if stage.dark else _colour(color) * F32(deep)


def mix(a: Colour | Array, b: Colour | Array, t: Array | float) -> Array:
    """Two colours mixed: ``t`` (one number, or one per pixel) is the weight of ``b``."""
    w = np.asarray(t, F32)[..., None]
    return _colour(a) * (1 - w) + _colour(b) * w


def centre(box: Box) -> tuple[float, float]:
    x, y, w, h = box
    return x + w / 2, y + h / 2


def rng(stage: Stage, salt: int = 0) -> np.random.Generator:
    return np.random.default_rng(stage.seed * 7919 + salt)


def unit(stage: Stage) -> float:
    """A length that looks the same at any scale: about one terminal pixel, in image pixels (at least 0.5)."""
    return max(0.5, stage.scale)


def ease_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def ramp(e0: float, e1: float, x: float) -> float:
    """``smoothstep`` for one number: 0 at ``e0``, 1 at ``e1``."""
    return ease_in_out((x - e0) / (e1 - e0))


def bump(t: float, rise: float, fall: float) -> float:
    """0 at t=0, 1 at t=rise, back to 0 at t=fall (smooth); 0 outside."""
    if t <= 0 or t >= fall:
        return 0.0
    if t < rise:
        return ease_in_out(t / rise)
    return 1 - ease_in_out((t - rise) / max(1e-6, fall - rise))


def timeline(*spans: tuple[float, int]) -> tuple[list[float], list[int]]:
    """Frame times (s) and gaps (ms) for spans of (milliseconds, frames), each frame drawn at the moment it
    shows: fast frames where things move fast, slow ones where they fade."""
    times: list[float] = []
    out: list[int] = []
    now = 0
    for ms, count in spans:
        each = max(16, round(ms / count))
        times += [(now + k * each) / 1000 for k in range(count)]
        out += [each] * count
        now += each * count
    return times, out


def gaps(frames: int, total_ms: int) -> list[int]:
    """Even frame durations adding up to about ``total_ms``."""
    each = max(16, round(total_ms / max(1, frames)))
    return [each] * frames


def segment(xs: Array, ys: Array, x0: float, y0: float, x1: float, y1: float) -> tuple[Array, Array]:
    """Each pixel's distance to the segment, and where along it (0 to 1) it is nearest."""
    dx, dy = x1 - x0, y1 - y0
    length2 = max(1e-6, dx * dx + dy * dy)
    t = np.clip(((xs - x0) * dx + (ys - y0) * dy) / length2, 0, 1)
    px, py = x0 + t * dx, y0 + t * dy
    return np.sqrt((xs - px) ** 2 + (ys - py) ** 2), t


def glow(xs: Array, ys: Array, cx: float, cy: float, radius: float) -> Array:
    """A round glow, 1 at the centre, reaching exactly 0 at ``radius``."""
    d = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2) / max(1e-6, radius)
    return np.exp(-4.0 * d * d) * smoothstep(1.0, 0.6, d)


def blank(stage: Stage) -> bytes:
    """A fully transparent frame."""
    return bytes(stage.width * stage.height * 4)


__all__ = ["Light", "Where", "Window", "blank", "border", "bump", "centre", "coords", "ease_in_out", "ease_out",
           "gaps", "glow", "mix", "ramp", "rng", "room", "segment", "shade", "timeline", "unit", "white_hot",
           "window"]
