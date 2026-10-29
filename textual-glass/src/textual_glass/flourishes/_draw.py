"""Drawing helpers the flourish makers share. No Textual imports.

A frame is drawn into a :class:`Light`: premultiplied colour and alpha, with
``over`` for paint (a shadow, a solid core) and ``add`` for light that adds up
(glows, trails). ``Light.frame()`` fades it out at the stage's border (rule 17)
and returns straight-alpha RGBA bytes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from ..render import F32, Array, Colour, col, smoothstep
from ..sprites import edge_fade

if TYPE_CHECKING:
    from . import Box, Stage


def coords(stage: Stage) -> tuple[Array, Array]:
    """Pixel centres: ``xs`` is a row (1, width), ``ys`` a column (height, 1)."""
    xs = np.arange(stage.width, dtype=F32)[None, :] + F32(0.5)
    ys = np.arange(stage.height, dtype=F32)[:, None] + F32(0.5)
    return xs, ys


def border(stage: Stage, margin: float | None = None) -> Array:
    """1 inside, 0 in the stage's outermost pixel ring, easing over ``margin``
    pixels (default: about half a cell); sides on the window's edge are not faded."""
    m = margin if margin is not None else max(2.0, 0.5 * min(stage.cell))
    return edge_fade(stage.width, stage.height, m, stage.window_edges)


def centre(box: Box) -> tuple[float, float]:
    x, y, w, h = box
    return x + w / 2, y + h / 2


def rng(stage: Stage, salt: int = 0) -> np.random.Generator:
    return np.random.default_rng(stage.seed * 7919 + salt)


def mix(a: Colour, b: Colour, t: float) -> Array:
    return col(a) * (1 - t) + col(b) * t


def ease_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def bump(t: float, rise: float, fall: float) -> float:
    """0 at t=0, 1 at t=rise, back to 0 at t=fall (smooth); 0 outside."""
    if t <= 0 or t >= fall:
        return 0.0
    if t < rise:
        return ease_in_out(t / rise)
    return 1 - ease_in_out((t - rise) / max(1e-6, fall - rise))


def segment(xs: Array, ys: Array, x0: float, y0: float, x1: float, y1: float) -> tuple[Array, Array]:
    """Each pixel's distance to the segment, and where along it (0 to 1) it is nearest."""
    dx, dy = x1 - x0, y1 - y0
    length2 = max(1e-6, dx * dx + dy * dy)
    t = np.clip(((xs - x0) * dx + (ys - y0) * dy) / length2, 0, 1)
    px, py = x0 + t * dx, y0 + t * dy
    return np.sqrt((xs - px) ** 2 + (ys - py) ** 2), t


def disc(xs: Array, ys: Array, cx: float, cy: float, r: float, feather: float = 1.0) -> Array:
    """A soft-edged disc: 1 inside ``r``, 0 from ``r + feather`` out."""
    d = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
    return smoothstep(r + feather, r - feather, d)


def glow(xs: Array, ys: Array, cx: float, cy: float, radius: float) -> Array:
    """A round glow, 1 at the centre, reaching exactly 0 at ``radius``."""
    d = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2) / max(1e-6, radius)
    return np.exp(-4.0 * d * d) * smoothstep(1.0, 0.6, d)


def ring(xs: Array, ys: Array, cx: float, cy: float, r: float, width: float) -> Array:
    """A thin soft ring of radius ``r``."""
    d = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
    return np.exp(-(((d - r) / max(1e-6, width)) ** 2)) * smoothstep(r + 3 * width, r + 2 * width, d)


def gaps(frames: int, total_ms: int) -> list[int]:
    """Even frame durations adding up to about ``total_ms``."""
    each = max(16, round(total_ms / max(1, frames)))
    return [each] * frames


class Light:
    """One frame being drawn: premultiplied colour and alpha."""

    def __init__(self, stage: Stage) -> None:
        self.stage = stage
        self.rgb: Array = np.zeros((stage.height, stage.width, 3), F32)
        self.a: Array = np.zeros((stage.height, stage.width), F32)

    @staticmethod
    def _colour(color: Colour | Array) -> Array:
        return color.astype(F32) if isinstance(color, np.ndarray) else col(color)

    def over(self, color: Colour | Array, alpha: Array | float) -> None:
        """Paint ``color`` at ``alpha`` over what is there (source over). For a
        shadow, paint black (or the theme's background)."""
        a = np.clip(np.broadcast_to(np.asarray(alpha, F32), self.a.shape), 0, 1)
        c = self._colour(color)
        self.rgb = c * a[..., None] + self.rgb * (1 - a)[..., None]
        self.a = a + self.a * (1 - a)

    def add(self, color: Colour | Array, amount: Array | float) -> None:
        """Add light: overlapping light adds up, and on a dark theme bright
        overlaps burn towards white (a comet's head, a burst's core)."""
        k = np.clip(np.broadcast_to(np.asarray(amount, F32), self.a.shape), 0, None)
        self.rgb = self.rgb + self._colour(color) * k[..., None]
        self.a = 1 - (1 - self.a) * (1 - np.clip(k, 0, 1))

    def frame(self, mask: Array | None = None) -> bytes:
        """Straight-alpha RGBA bytes, faded out at the border (``mask``, default :func:`border`)."""
        m = border(self.stage) if mask is None else mask
        a = np.clip(self.a * m, 0, 1)
        s = self.rgb / np.maximum(self.a, 1e-4)[..., None]
        peak = s.max(axis=2)
        if self.stage.dark:  # light beyond full brightness turns white
            hot = np.clip(peak - 1.0, 0, None)
            s = s / np.maximum(peak, 1.0)[..., None] + (hot * 0.6)[..., None]
        data = np.concatenate([np.clip(s, 0, 1), a[..., None]], 2)
        return (data * 255 + 0.5).astype(np.uint8).tobytes()


def blank(stage: Stage) -> bytes:
    """A fully transparent frame."""
    return bytes(stage.width * stage.height * 4)


def soft_flash(stage: Stage) -> tuple[list[bytes], list[int]]:
    """A placeholder: a glow that swells and fades at the anchor (until a maker is written)."""
    xs, ys = coords(stage)
    cx, cy = centre(stage.at)
    radius = min(stage.width, stage.height) / 2
    shape = glow(xs, ys, cx, cy, radius)
    out = []
    for f in range(20):
        light = Light(stage)
        light.add(stage.color, shape * bump(f / 19, 0.3, 1.0) * 0.8)
        out.append(light.frame())
    return out, gaps(20, 900)


def unit(stage: Stage) -> float:
    """A length that looks the same at any scale: about one terminal pixel, in image pixels (at least 0.5)."""
    return max(0.5, stage.scale)


__all__ = ["Light", "blank", "border", "bump", "centre", "coords", "disc", "ease_in_out", "ease_out", "gaps",
           "glow", "mix", "rng", "ring", "segment", "soft_flash", "unit"]
