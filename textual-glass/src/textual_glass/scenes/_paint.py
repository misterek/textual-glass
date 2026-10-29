"""What the space scenes draw with: canvases, noise, spheres, shapes and a ship
painter. Pure numpy, no Textual imports. Exploratory.

Every picture is drawn into a :class:`Paint`, the flourishes' ``Light`` with a
border fade of its own size (rule 17) and, for pictures that stay on screen
all day (a planet), colour kept at full depth where it is opaque.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from ..flourishes import Stage as _Canvas
from ..flourishes._draw import Light, border
from ..render import F32, Array, smoothstep
from . import Actor, Route, Stage

TAU = 2 * math.pi
WHITE = np.ones(3, F32)
BLACK = np.zeros(3, F32)


# ================================================================ colour ==


def ink(stage: Stage, role: str) -> Array:
    """A palette role as float RGB (0 to 1)."""
    return np.asarray(stage.colors[role], F32) / 255


def blend(a: Array, b: Array, t: float | Array) -> Array:
    w = np.asarray(t, F32)[..., None] if np.ndim(t) else F32(t)
    return a * (1 - w) + b * w


def lum(c: Array) -> float:
    return float(0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2])


# ================================================================ canvas ==


def _packing(opaque_bits: int) -> Array:
    """Per alpha byte, the mask for a pixel's four bytes: like the flourishes' packing (faint light keeps few
    colour bits, so it compresses small), with ``opaque_bits`` for nearly opaque pixels."""
    table = np.zeros((256, 4), np.uint8)
    for alpha, bits in ((0, 2), (12, 3), (24, 4), (48, 5), (96, 6), (240, opaque_bits)):
        table[alpha:, :3] = 0xFF << (8 - bits) & 0xFF
    table[:, 3] = 0xFE
    table[250:, 3] = 0xFF
    return table.view(np.uint32)[:, 0]


_COARSE, _FINE = _packing(6), _packing(8)


class Paint(Light):
    """One frame: premultiplied colour and alpha, ``over``, ``add`` and ``shine`` as in the flourishes.
    ``frame()`` fades the outermost ``margin`` pixels to exactly zero and packs the frame."""

    def __init__(self, width: int, height: int, dark: bool, margin: float = 2.0, fine: bool = False,
                 paint: float = 0.92) -> None:
        canvas = _Canvas(width, height, 1.0, (2 * margin, 2 * margin), (0.0, 0.0, float(width), float(height)),
                         None, (255, 255, 255), (255, 255, 255), dark, (False, False, False, False), 0)
        super().__init__(canvas, paint)
        self.margin, self.fine = margin, fine
        self.xs = np.arange(width, dtype=F32)[None, :] + F32(0.5)
        self.ys = np.arange(height, dtype=F32)[:, None] + F32(0.5)

    @property
    def width(self) -> int:
        return self.stage.width

    @property
    def height(self) -> int:
        return self.stage.height

    def frame(self) -> bytes:
        a = (self.a * border(self.stage, self.margin)).reshape(-1)
        lit = np.flatnonzero(a >= 3.5 / 255)
        s = self.rgb.reshape(-1, 3)[lit] / np.maximum(self.a.reshape(-1)[lit], 1e-4)[:, None]
        if self.stage.dark:  # light beyond full brightness turns white
            peak = s.max(axis=1)
            s = s / np.maximum(peak, 1.0)[:, None] + (np.clip(peak - 1.0, 0, None) * 0.6)[:, None]
        px = np.empty((lit.size, 4), np.uint8)
        px[:, :3] = np.clip(s, 0, 1) * 255 + 0.5
        px[:, 3] = np.minimum(a[lit], 1) * 255 + 0.5
        px.view(np.uint32)[:, 0] &= (_FINE if self.fine else _COARSE)[px[:, 3]]
        out = np.zeros((a.size, 4), np.uint8)
        out[lit] = px
        return out.tobytes()


@dataclass
class Sprite:
    """Frames of one picture, before it is put on a route."""

    frames: list[bytes]
    width: int
    height: int
    gaps: list[int]
    loop: bool
    #: Stretched over this many cells, or None for terminal pixels.
    cells: tuple[int, int] | None = None
    #: Where the picture's own centre of interest sits, from the image centre, in terminal pixels.
    offset: tuple[float, float] = (0.0, 0.0)
    name: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    def actor(self, route: Route, enter: float = 0.0, leave: float = math.inf, z: int = 0) -> Actor:
        if self.offset != (0.0, 0.0):  # the route is for the centre of interest; actors are placed by their centre
            ox, oy = self.offset
            route = Route(tuple((t, x - ox, y - oy) for t, x, y in route.keys))
        gaps = self.gaps if len(set(self.gaps)) > 1 else self.gaps[:1]
        return Actor(self.frames, self.width, self.height, route, tuple(gaps), self.loop, enter, leave, z,
                     self.cells)


def still_sprite(sprite: Sprite) -> Sprite:
    """Only the last frame (``minimal`` motion shows the end)."""
    return Sprite([sprite.frames[-1]], sprite.width, sprite.height, [1000], sprite.loop, sprite.cells,
                  sprite.offset, sprite.name, sprite.extra)


def stretched_size(stage: Stage, cols: int, rows: int, max_px: int) -> tuple[int, int, float]:
    """The image size for a picture stretched over ``cols`` by ``rows`` cells: square pixels, at most
    ``max_px`` of them, never more than the terminal's own. Returns width, height and image pixels per
    terminal pixel."""
    cw, ch = stage.cell
    w, h = cols * cw, rows * ch
    s = min(1.0, math.sqrt(max_px / max(1.0, w * h)))
    return max(4, round(w * s)), max(4, round(h * s)), s


# ================================================================= noise ==


class Noise:
    """Smooth value noise in three dimensions, periodic every ``n`` units, so it can wrap a sphere."""

    def __init__(self, seed: int, n: int = 32) -> None:
        self.n = n
        self.v = np.random.default_rng(seed).random(n ** 3, dtype=F32)

    def __call__(self, x: Array, y: Array, z: Array) -> Array:
        n = self.n
        xf, yf, zf = np.floor(x), np.floor(y), np.floor(z)
        fx, fy, fz = (x - xf).astype(F32), (y - yf).astype(F32), (z - zf).astype(F32)
        fx, fy, fz = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy), fz * fz * (3 - 2 * fz)
        xi, yi, zi = xf.astype(np.int64) % n, yf.astype(np.int64) % n, zf.astype(np.int64) % n
        x1, y1, z1 = (xi + 1) % n, (yi + 1) % n, (zi + 1) % n
        v = self.v

        def g(a: Array, b: Array, c: Array) -> Array:
            return v[(a * n + b) * n + c]

        c00 = g(xi, yi, zi) * (1 - fz) + g(xi, yi, z1) * fz
        c01 = g(xi, y1, zi) * (1 - fz) + g(xi, y1, z1) * fz
        c10 = g(x1, yi, zi) * (1 - fz) + g(x1, yi, z1) * fz
        c11 = g(x1, y1, zi) * (1 - fz) + g(x1, y1, z1) * fz
        return (c00 * (1 - fy) + c01 * fy) * (1 - fx) + (c10 * (1 - fy) + c11 * fy) * fx

    def fbm(self, x: Array, y: Array, z: Array, octaves: int = 4, gain: float = 0.5) -> Array:
        acc = np.zeros(np.broadcast_shapes(np.shape(x), np.shape(y), np.shape(z)), F32)
        amp, total, f = 1.0, 0.0, 1.0
        for o in range(octaves):
            acc += amp * self(x * f + 17.3 * o, y * f + 5.1 * o, z * f + 11.7 * o)
            total += amp
            amp *= gain
            f *= 2.03
        return acc / total


# =============================================================== spheres ==


def rot_x(a: float) -> Array:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], F32)


def rot_z(a: float) -> Array:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], F32)


def unit(*v: float) -> Array:
    a = np.array(v, F32)
    return a / np.linalg.norm(a)


@dataclass
class Disc:
    """A sphere's pixels on a canvas: x right, y up, z towards the viewer, in radii."""

    inside: Array  # flat indices of pixels whose centre is on the disc (or within ``pad`` radii of it)
    nx: Array
    ny: Array
    nz: Array
    r: Array  # distance from the centre in radii
    cover: Array  # anti-aliased coverage of the disc


def disc(width: int, height: int, cx: float, cy: float, radius: float, pad: float = 0.0) -> Disc:
    xs = (np.arange(width, dtype=F32)[None, :] + F32(0.5) - cx) / radius
    ys = (cy - (np.arange(height, dtype=F32)[:, None] + F32(0.5))) / radius
    rr = (xs * xs + ys * ys).reshape(-1)
    flat = np.flatnonzero(rr < (1 + pad) ** 2)
    x = np.broadcast_to(xs, (height, width)).reshape(-1)[flat]
    y = np.broadcast_to(ys, (height, width)).reshape(-1)[flat]
    r = np.sqrt(rr[flat])
    nz = np.sqrt(np.clip(1 - r * r, 0, 1))
    cover = np.clip((1 - r) * radius + 0.5, 0, 1)
    return Disc(flat, x, y, nz, r, cover)


# ================================================================ shapes ==


def sd_circle(u: Array, v: Array, cu: float, cv: float, r: float) -> Array:
    return np.sqrt((u - cu) ** 2 + (v - cv) ** 2) - r


def sd_ellipse(u: Array, v: Array, cu: float, cv: float, ru: float, rv: float) -> Array:
    """Roughly the distance to an ellipse (exact on its axes): enough for shading and edges."""
    k = np.sqrt(((u - cu) / ru) ** 2 + ((v - cv) / rv) ** 2)
    return (k - 1) * min(ru, rv)


def sd_box(u: Array, v: Array, cu: float, cv: float, hu: float, hv: float, r: float = 0.0) -> Array:
    qu = np.abs(u - cu) - (hu - r)
    qv = np.abs(v - cv) - (hv - r)
    return np.sqrt(np.maximum(qu, 0) ** 2 + np.maximum(qv, 0) ** 2) + np.minimum(np.maximum(qu, qv), 0) - r


def sd_capsule(u: Array, v: Array, u0: float, v0: float, u1: float, v1: float, r: float) -> Array:
    du, dv = u1 - u0, v1 - v0
    t = np.clip(((u - u0) * du + (v - v0) * dv) / max(1e-9, du * du + dv * dv), 0, 1)
    return np.sqrt((u - u0 - t * du) ** 2 + (v - v0 - t * dv) ** 2) - r


def sd_poly(u: Array, v: Array, pts: Sequence[tuple[float, float]]) -> Array:
    """Signed distance to a polygon (negative inside)."""
    d = (u - pts[0][0]) ** 2 + (v - pts[0][1]) ** 2
    s = np.ones_like(d)
    n = len(pts)
    for i in range(n):
        (ui, vi), (uj, vj) = pts[i], pts[i - 1]
        eu, ev = uj - ui, vj - vi
        wu, wv = u - ui, v - vi
        t = np.clip((wu * eu + wv * ev) / max(1e-9, eu * eu + ev * ev), 0, 1)
        bu, bv = wu - eu * t, wv - ev * t
        d = np.minimum(d, bu * bu + bv * bv)
        c1, c2, c3 = v >= vi, v < vj, eu * wv > ev * wu
        s = np.where((c1 & c2 & c3) | (~c1 & ~c2 & ~c3), -s, s)
    return s * np.sqrt(d)


def cover(d: Array, aa: float = 0.6) -> Array:
    """Anti-aliased coverage of a shape from its signed distance in pixels."""
    return np.clip(0.5 - d / (2 * aa), 0, 1)


def soft_glow(d: Array, radius: float) -> Array:
    """A glow around a point (``d`` its distance), 1 at the centre and exactly 0 at ``radius``."""
    q = d / max(1e-6, radius)
    return np.exp(-4.0 * q * q) * smoothstep(1.0, 0.6, q)


def star(xs: Array, ys: Array, x: float, y: float, size: float, spike: float, reach: float,
         angle: float = 0.0, arms: int = 4) -> Array:
    """A soft point with thin spikes (``arms`` of them), gone by ``reach``."""
    dx, dy = xs - x, ys - y
    r = np.sqrt(dx * dx + dy * dy)
    shape = np.exp(-(r / max(0.3, size)) ** 2)
    if spike > 0:
        phi = np.arctan2(dy, dx) - angle
        for k in range(arms // 2):
            a = phi + k * TAU / arms
            across = np.abs(r * np.sin(a))
            shape = shape + np.exp(-across / (0.28 * size + 0.25)) * np.exp(-r / (0.35 * spike)) * 0.8
    return shape * smoothstep(reach, reach * 0.6, r)
