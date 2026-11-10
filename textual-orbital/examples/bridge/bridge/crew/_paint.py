"""Smooth painting for the ``smooth`` crew style: supersampled canvases, signed-distance shapes, soft shading
and light. Pure numpy, no Textual (and so nothing from textual-glass either: its scene painters,
``textual_glass/scenes/_paint.py``, are the model this follows).

A picture is drawn in **design units** (a fixed box, 116 x 100 for a station) and fitted to whatever pixel size
it is asked for, bottom centred with a transparent pixel on every side. The canvas is ``ss`` times the output
size each way; every shape's edge is anti-aliased from its distance field at that size, and ``frame()``
box-filters it down. Colour is premultiplied float RGB (0 to 1) and alpha.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

F32 = np.float32
Array = np.ndarray
WHITE = np.ones(3, F32)


def rgb(c: Sequence[int] | Sequence[float]) -> Array:
    """An (r, g, b) of 0 to 255 as float RGB (0 to 1)."""
    return np.array(c[:3], F32) / 255


def mix(a: Array, b: Array, t: float | Array) -> Array:
    w = np.asarray(t, F32)[..., None] if np.ndim(t) else F32(t)
    return (a * (1 - w) + b * w).astype(F32)


def lum(c: Array) -> float:
    return float(0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2])


def smoothstep(e0: float, e1: float, x: Array | float) -> Array:
    t = np.clip((np.asarray(x, F32) - e0) / (e1 - e0), 0, 1)
    return (t * t * (3 - 2 * t)).astype(F32)


# ================================================================ shapes ==
# Signed distances (negative inside), in whatever units the coordinates are in.


def sd_circle(x: Array, y: Array, cx: float, cy: float, r: float) -> Array:
    return np.sqrt((x - cx) ** 2 + (y - cy) ** 2) - r


def sd_ellipse(x: Array, y: Array, cx: float, cy: float, rx: float, ry: float) -> Array:
    """Roughly the distance to an ellipse (exact on its axes, a good gradient everywhere)."""
    px, py = (x - cx) / rx, (y - cy) / ry
    k = np.sqrt(px * px + py * py)
    # scale by the local radius so the edge stays one unit wide all round
    g = np.sqrt((px / rx) ** 2 + (py / ry) ** 2)
    return (k - 1) * k / np.maximum(g, 1e-6)


def sd_box(x: Array, y: Array, cx: float, cy: float, hx: float, hy: float, r: float = 0.0) -> Array:
    qx = np.abs(x - cx) - (hx - r)
    qy = np.abs(y - cy) - (hy - r)
    return np.sqrt(np.maximum(qx, 0) ** 2 + np.maximum(qy, 0) ** 2) + np.minimum(np.maximum(qx, qy), 0) - r


def sd_segment(x: Array, y: Array, x0: float, y0: float, x1: float, y1: float) -> tuple[Array, Array]:
    """The distance to a segment and how far along it (0 to 1) the nearest point is."""
    dx, dy = x1 - x0, y1 - y0
    t = np.clip(((x - x0) * dx + (y - y0) * dy) / max(1e-9, dx * dx + dy * dy), 0, 1)
    return np.sqrt((x - x0 - t * dx) ** 2 + (y - y0 - t * dy) ** 2), t


def sd_capsule(x: Array, y: Array, x0: float, y0: float, x1: float, y1: float, r0: float,
               r1: float | None = None) -> Array:
    """A capsule, or a tapered one (radius ``r0`` at the start, ``r1`` at the end)."""
    d, t = sd_segment(x, y, x0, y0, x1, y1)
    return d - (r0 if r1 is None else r0 + (r1 - r0) * t)


def sd_poly(x: Array, y: Array, pts: Sequence[tuple[float, float]]) -> Array:
    """Signed distance to a polygon (negative inside)."""
    d = (x - pts[0][0]) ** 2 + (y - pts[0][1]) ** 2
    s = np.ones_like(d)
    n = len(pts)
    for i in range(n):
        (xi, yi), (xj, yj) = pts[i], pts[i - 1]
        ex, ey = xj - xi, yj - yi
        wx, wy = x - xi, y - yi
        t = np.clip((wx * ex + wy * ey) / max(1e-9, ex * ex + ey * ey), 0, 1)
        bx, by = wx - ex * t, wy - ey * t
        d = np.minimum(d, bx * bx + by * by)
        c1, c2, c3 = y >= yi, y < yj, ex * wy > ey * wx
        s = np.where((c1 & c2 & c3) | (~c1 & ~c2 & ~c3), -s, s)
    return s * np.sqrt(d)


def sd_arc(x: Array, y: Array, cx: float, cy: float, r: float, a0: float, a1: float, width: float) -> Array:
    """A stroke along a circular arc from angle ``a0`` to ``a1`` (radians, y down: positive is clockwise)."""
    ang = np.arctan2(y - cy, x - cx)
    mid, half = (a0 + a1) / 2, abs(a1 - a0) / 2
    rel = (ang - mid + math.pi) % (2 * math.pi) - math.pi
    on = np.abs(rel) <= half
    ring = np.abs(np.sqrt((x - cx) ** 2 + (y - cy) ** 2) - r) - width
    ends = np.minimum(sd_circle(x, y, cx + r * math.cos(a0), cy + r * math.sin(a0), width),
                      sd_circle(x, y, cx + r * math.cos(a1), cy + r * math.sin(a1), width))
    return np.where(on, np.minimum(ring, ends), ends)


def smin(a: Array, b: Array, k: float) -> Array:
    """A smooth union: the two shapes melt together over about ``k``."""
    if k <= 0:
        return np.minimum(a, b)
    h = np.clip(0.5 + 0.5 * (b - a) / k, 0, 1)
    return b * (1 - h) + a * h - k * h * (1 - h)


def union(*ds: Array) -> Array:
    out = ds[0]
    for d in ds[1:]:
        out = np.minimum(out, d)
    return out


def soft_glow(d: Array, radius: float) -> Array:
    """1 at ``d`` = 0, exactly 0 at ``radius`` (glass rule 16: light ends inside its box)."""
    q = np.maximum(d, 0) / max(1e-6, radius)
    return (np.exp(-4.0 * q * q) * smoothstep(1.0, 0.55, q)).astype(F32)


def star(x: Array, y: Array, cx: float, cy: float, size: float, spike: float, reach: float,
         angle: float = 0.0, arms: int = 4) -> Array:
    """A soft point with thin spikes, gone by ``reach`` (as in glass's scenes)."""
    dx, dy = x - cx, y - cy
    r = np.sqrt(dx * dx + dy * dy)
    shape = np.exp(-(r / max(0.2, size)) ** 2)
    if spike > 0:
        phi = np.arctan2(dy, dx) - angle
        for k in range(arms // 2):
            a = phi + k * 2 * math.pi / arms
            across = np.abs(r * np.sin(a))
            shape = shape + np.exp(-across / (0.28 * size + 0.12)) * np.exp(-r / (0.35 * spike)) * 0.8
    return (shape * smoothstep(reach, reach * 0.6, r)).astype(F32)


class Noise:
    """Smooth value noise in two dimensions (glass's ``Noise``, flattened), periodic every ``n`` units."""

    def __init__(self, seed: int, n: int = 32) -> None:
        self.n = n
        self.v = np.random.default_rng(seed).random(n * n, dtype=F32)

    def __call__(self, x: Array, y: Array) -> Array:
        n = self.n
        xf, yf = np.floor(x), np.floor(y)
        fx, fy = (x - xf).astype(F32), (y - yf).astype(F32)
        fx, fy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
        xi, yi = xf.astype(np.int64) % n, yf.astype(np.int64) % n
        x1, y1 = (xi + 1) % n, (yi + 1) % n
        v = self.v
        top = v[yi * n + xi] * (1 - fx) + v[yi * n + x1] * fx
        bottom = v[y1 * n + xi] * (1 - fx) + v[y1 * n + x1] * fx
        return (top * (1 - fy) + bottom * fy).astype(F32)

    def fbm(self, x: Array, y: Array, octaves: int = 4, gain: float = 0.5) -> Array:
        acc = np.zeros(np.broadcast_shapes(np.shape(x), np.shape(y)), F32)
        amp, total, f = 1.0, 0.0, 1.0
        for o in range(octaves):
            c, s = math.cos(0.6 * o), math.sin(0.6 * o)  # each octave turned, so no lattice lines line up
            acc += amp * self((x * c - y * s) * f + 17.3 * o, (x * s + y * c) * f + 5.1 * o)
            total += amp
            amp *= gain
            f *= 2.03
        return acc / total


# ================================================================ canvas ==


def _packing(opaque_bits: int) -> Array:
    """Per alpha byte, the mask for a pixel's four bytes (glass's scene frames do the same): faint light keeps
    few colour bits and opaque pixels ``opaque_bits``, so smooth frames compress to a fraction (zlib, as glass
    uploads them) with no banding you can see; alpha keeps seven bits below nearly opaque."""
    table = np.zeros((256, 4), np.uint8)
    for alpha, bits in ((0, 2), (12, 3), (24, 4), (48, 5), (96, 6), (240, opaque_bits)):
        table[alpha:, :3] = 0xFF << (8 - bits) & 0xFF
    table[:, 3] = 0xFE
    table[250:, 3] = 0xFF
    return table.view(np.uint32)[:, 0]


PACKING = _packing(6)
#: Alpha below this is nothing (it would only cost bytes).
MIN_ALPHA = 4


@dataclass
class Region:
    """A window of the canvas: its slices, and the design-unit coordinates of its pixel centres."""

    sl: tuple[slice, slice]
    x: Array  # (1, w)
    y: Array  # (h, 1)

    @property
    def shape(self) -> tuple[int, int]:
        return self.y.shape[0], self.x.shape[1]

    def full(self, d: Array) -> Array:
        return np.broadcast_to(d, self.shape)

    def sub(self, box: tuple[slice, slice]) -> Region:
        """The part of this region in ``box`` (slices of this region's arrays)."""
        ri, rj = box
        i0, j0 = self.sl[0].start or 0, self.sl[1].start or 0
        return Region((slice(i0 + ri.start, i0 + ri.stop), slice(j0 + rj.start, j0 + rj.stop)), self.x[:, rj],
                      self.y[ri, :])


def nonzero_box(mask: Array, pad: int = 1) -> tuple[slice, slice] | None:
    """The slices round where ``mask`` is true, ``pad`` wider (at least two each way), or None."""
    rows = np.flatnonzero(mask.any(axis=1))
    if not rows.size:
        return None
    cols = np.flatnonzero(mask.any(axis=0))
    h, w = mask.shape
    i0, i1 = max(0, int(rows[0]) - pad), min(h, int(rows[-1]) + 1 + pad)
    j0, j1 = max(0, int(cols[0]) - pad), min(w, int(cols[-1]) + 1 + pad)
    if i1 - i0 < 2:
        i0, i1 = max(0, min(i0, h - 2)), min(h, max(i1, i0 + 2))
    if j1 - j0 < 2:
        j0, j1 = max(0, min(j0, w - 2)), min(w, max(j1, j0 + 2))
    return slice(i0, i1), slice(j0, j1)


def _crop(reg: Region, amount: Array, color: Array | None = None) -> tuple[Region, Array, Array | None] | None:
    a = reg.full(amount)
    box = nonzero_box(a > 0, pad=0)
    if box is None:
        return None
    c = color
    if c is not None and np.ndim(c) == 3:
        c = np.broadcast_to(c, (*reg.shape, 3))[box]
    return reg.sub(box), a[box], c


class Canvas:
    """One frame, supersampled: premultiplied colour and alpha, drawn in design units.

    ``design`` (width, height) is fitted into ``width`` x ``height`` pixels less a one-pixel margin, bottom
    centred, at any (not only whole-number) scale. ``fit="exact"`` fits it to the whole picture instead, no
    margin (the export of a picture pack's canvas)."""

    def __init__(self, width: int, height: int, design: tuple[float, float], ss: int | None = None,
                 fit: str = "bottom") -> None:
        self.width, self.height = width, height
        dw, dh = design
        self.ss = ss if ss is not None else (3 if max(width, height) < 110 else 2)
        margin = 0 if fit == "exact" else 2
        s = max(1e-3, min((width - margin) / dw, (height - margin) / dh))
        self.scale = s  # output pixels per design unit
        ox = (width - dw * s) / 2
        oy = height - margin / 2 - dh * s if fit in ("bottom", "exact") else (height - dh * s) / 2
        ss_ = self.ss
        self.k = s * ss_  # canvas pixels per design unit
        self.ox, self.oy = ox * ss_, oy * ss_
        self.W, self.H = max(1, width * ss_), max(1, height * ss_)
        self.rgb = np.zeros((self.H, self.W, 3), F32)
        self.a = np.zeros((self.H, self.W), F32)
        self.px = 1.0 / self.k  # one canvas pixel, in design units
        self.design = design

    # -- where -------------------------------------------------------------------------------------

    def region(self, x0: float, y0: float, x1: float, y1: float) -> Region | None:
        """The window over design box (x0, y0)-(x1, y1), or None if it is off the canvas (or too thin)."""
        j0 = max(0, int(math.floor(x0 * self.k + self.ox)) - 1)
        j1 = min(self.W, int(math.ceil(x1 * self.k + self.ox)) + 1)
        i0 = max(0, int(math.floor(y0 * self.k + self.oy)) - 1)
        i1 = min(self.H, int(math.ceil(y1 * self.k + self.oy)) + 1)
        if j1 - j0 < 2 or i1 - i0 < 2:
            return None
        xs = ((np.arange(j0, j1, dtype=F32) + 0.5 - self.ox) / self.k)[None, :]
        ys = ((np.arange(i0, i1, dtype=F32) + 0.5 - self.oy) / self.k)[:, None]
        return Region((slice(i0, i1), slice(j0, j1)), xs, ys)

    def everywhere(self) -> Region:
        r = self.region(-1e4, -1e4, 1e4, 1e4)
        assert r is not None
        return r

    def cover(self, d: Array, soft: float = 1.0) -> Array:
        """Anti-aliased coverage of a shape from its distance (design units): one canvas pixel of edge, or
        ``soft`` of them."""
        return np.clip(0.5 - d / (self.px * soft), 0, 1).astype(F32)

    # -- paint -------------------------------------------------------------------------------------

    def over(self, reg: Region, color: Array, alpha: Array) -> None:
        """Paint ``color`` (RGB, or per pixel) at coverage ``alpha`` over what is there."""
        got = _crop(reg, alpha, np.asarray(color, F32))
        if got is None:
            return
        reg, alpha, color_ = got
        assert color_ is not None
        color = color_
        a = np.clip(alpha, 0, 1).astype(F32)
        dst, da = self.rgb[reg.sl], self.a[reg.sl]
        keep = 1 - a
        dst *= keep[..., None]
        dst += np.asarray(color, F32) * a[..., None]
        da *= keep
        da += a

    def blend(self, reg: Region, prgb: Array, alpha: Array) -> None:
        """A premultiplied picture (``prgb`` (h, w, 3), ``alpha`` (h, w), over the whole of ``reg``) over what is
        there."""
        dst, da = self.rgb[reg.sl], self.a[reg.sl]
        keep = 1 - np.clip(alpha, 0, 1).astype(F32)
        dst *= keep[..., None]
        dst += prgb
        da *= keep
        da += 1 - keep

    def shine(self, reg: Region, color: Array, amount: Array, opacity: float = 1.0) -> None:
        """Light: adds ``color`` x ``amount``; where the canvas is empty the light shows as a translucent glow
        (``opacity`` of its strength)."""
        got = _crop(reg, amount, np.asarray(color, F32))
        if got is None:
            return
        reg, k, color_ = got
        dst, da = self.rgb[reg.sl], self.a[reg.sl]
        dst += np.asarray(color_, F32) * k[..., None]
        gain = np.clip(k * opacity, 0, 1)
        da += (1 - da) * gain

    def light(self, reg: Region, color: Array, amount: Array) -> None:
        """Light that falls only on what is painted (it adds no alpha): a lamp's spill on the console."""
        got = _crop(reg, amount, np.asarray(color, F32))
        if got is None:
            return
        reg, k, color_ = got
        self.rgb[reg.sl] += np.asarray(color_, F32) * (k * self.a[reg.sl])[..., None]

    def darken(self, reg: Region, amount: Array) -> None:
        """A soft shadow on what is painted (colour only)."""
        got = _crop(reg, amount)
        if got is None:
            return
        reg, k, _ = got
        self.rgb[reg.sl] *= (1 - np.clip(k, 0, 1))[..., None]

    # -- out ---------------------------------------------------------------------------------------

    def image(self, edge: bool = True, pack: bool = True) -> Array:
        """The frame at its output size: (h, w, 4) uint8, straight alpha, its outermost ring transparent
        (``edge``) and its colours packed to the bits glass's frames keep (``pack``; off for a PNG to paint on)."""
        h, w, ss = self.height, self.width, self.ss
        out = np.zeros((max(0, h), max(0, w), 4), np.uint8)
        if w < 3 or h < 3:
            return out
        c = self.rgb.reshape(h, ss, w, ss, 3).mean(axis=(1, 3))
        a = np.clip(self.a.reshape(h, ss, w, ss).mean(axis=(1, 3)), 0, 1)
        lit = a >= 0.5 / 255
        s = c[lit] / a[lit][:, None]
        peak = s.max(axis=1)
        s = s / np.maximum(peak, 1.0)[:, None] + (np.clip(peak - 1.0, 0, None) * 0.55)[:, None]
        px = np.empty((s.shape[0], 4), np.uint8)
        px[:, :3] = np.floor(np.clip(s, 0, 1) * 255 + 0.5)
        px[:, 3] = np.floor(a[lit] * 255 + 0.5)
        if pack:
            px.view(np.uint32)[:, 0] &= PACKING[px[:, 3]]
        out[lit] = px
        if edge:
            out[0], out[-1], out[:, 0], out[:, -1] = 0, 0, 0, 0
        out[out[..., 3] < MIN_ALPHA] = 0
        return out

    def frame(self) -> bytes:
        return self.image().tobytes()


def empty_frame(width: int, height: int) -> bytes:
    return bytes(max(0, width) * max(0, height) * 4)


# ================================================================ light ==


def normals(reg: Region, d: Array, thick: float, px: float) -> tuple[Array, Array, Array]:
    """A surface normal for a shape from its distance field: flat in the middle, rounding over towards the
    edge across ``thick`` design units (a quarter circle), like a cushion. y is down, z towards the viewer."""
    d = reg.full(d)
    gy, gx = np.gradient(d.astype(F32))
    n = np.sqrt(gx * gx + gy * gy) + 1e-6
    gx, gy = gx / n, gy / n
    e = np.clip(-d / max(thick, px), 0, 1)
    side = 1 - e
    return gx * side, gy * side, np.sqrt(np.clip(1 - side * side, 0, 1))


@dataclass
class Light:
    """How a station is lit: a soft key light from the upper left, a cool fill from the room above, a cool
    rim from behind (so dark hair reads against a dark nebula), and the console lamp's glow from below in
    front (the screen's light on face and chest, the HD-2D trick: state shows as light)."""

    lamp: Array  # RGB 0 to 1
    lamp_at: tuple[float, float, float]  # design units, z towards the viewer
    lamp_power: float = 1.0
    key: tuple[float, float, float] = (-0.5, -0.72, 0.62)
    key_color: tuple[float, float, float] = (1.0, 0.95, 0.88)
    key_power: float = 0.74
    sky: tuple[float, float, float] = (0.36, 0.37, 0.48)
    ground: tuple[float, float, float] = (0.16, 0.14, 0.22)
    rim_color: tuple[float, float, float] = (0.62, 0.74, 1.0)
    rim_power: float = 0.5
    reach: float = 60.0  # how far (design units) the lamp's light carries

    def shade(self, reg: Region, albedo: Array, n: tuple[Array, Array, Array], wrap: float = 0.25,
              gloss: float = 0.0, shininess: float = 24.0, rim: float = 1.0, warm: float = 0.0) -> Array:
        """``albedo`` lit at normals ``n`` over ``reg``: (h, w, 3)."""
        nx, ny, nz = n
        kx, ky, kz = _unit(self.key)
        ndl = nx * kx + ny * ky + nz * kz
        key = np.clip((ndl + wrap) / (1 + wrap), 0, 1)
        up = np.clip(0.5 - 0.5 * ny, 0, 1)[..., None]
        amb = np.array(self.ground, F32) * (1 - up) + np.array(self.sky, F32) * up
        light = amb + np.array(self.key_color, F32) * (key * self.key_power)[..., None]
        # the lamp, in front of the console, below: brightest on what faces it and is near
        lx, ly, lz = self.lamp_at
        vx, vy = lx - reg.x, ly - reg.y
        dist = np.sqrt(vx * vx + vy * vy + lz * lz)
        ndv = (nx * vx + ny * vy + nz * lz) / dist
        fall = np.exp(-np.maximum(dist - lz, 0) / self.reach)
        lamp_k = np.clip((ndv + 0.2) / 1.2, 0, 1) * fall * self.lamp_power
        light = light + self.lamp[None, None, :] * lamp_k[..., None]
        out = albedo * light
        if warm > 0:  # skin: the shadows go a little warm (light through skin)
            shadow = (1 - np.clip(key, 0, 1))[..., None] * warm
            out = out + albedo * np.array([0.16, 0.03, 0.0], F32) * shadow
        edge = np.clip(1 - nz, 0, 1)
        if rim > 0:
            back = smoothstep(0.45, 1.0, edge) * np.clip(0.35 - 0.9 * ny, 0, 1) * self.rim_power * rim
            under = smoothstep(0.5, 1.0, edge) * np.clip(0.2 + 0.9 * ny, 0, 1) * fall * self.lamp_power * rim * 0.7
            out = out + np.array(self.rim_color, F32) * back[..., None] + self.lamp * under[..., None]
        if gloss > 0:
            hx, hy, hz = _unit((kx, ky, kz + 1.0))
            spec = np.clip(nx * hx + ny * hy + nz * hz, 0, 1) ** shininess * gloss
            out = out + np.array(self.key_color, F32) * spec[..., None]
        return out.astype(F32)

    def flat(self, reg: Region, color: Array, amount: float = 1.0) -> Array:
        """A decal's colour (eyes, a mouth, a badge) as lit on a surface facing the viewer: the same light,
        no shape of its own."""
        n = (np.zeros(reg.shape, F32), np.full(reg.shape, -0.15, F32), np.full(reg.shape, 0.99, F32))
        lit = self.shade(reg, np.asarray(color, F32) * np.ones((*reg.shape, 1), F32), n, rim=0.0)
        return mix(np.asarray(color, F32) * np.ones((*reg.shape, 1), F32), lit, amount)


def _unit(v: tuple[float, float, float]) -> tuple[float, float, float]:
    n = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    return v[0] / n, v[1] / n, v[2] / n


def fill(cv: Canvas, light: Light | None, reg: Region, d: Array, albedo: Array, thick: float = 1.0,
         n: tuple[Array, Array, Array] | None = None, alpha: float | Array = 1.0, soft: float = 1.0,
         **shade: float) -> None:
    """Paint the shape at distance ``d`` (design units, over ``reg``) in ``albedo`` (RGB, or per pixel),
    shaded by ``light`` at its own cushion normals (rounding over ``thick``) or at normals ``n`` (arrays over
    ``reg``); unlit if ``light`` is None. Only the shape's own box is worked on."""
    cov = cv.cover(reg.full(d), soft) * alpha
    box = nonzero_box(cov > 0)
    if box is None:
        return
    sub, c = reg.sub(box), cov[box]
    alb = np.asarray(albedo, F32)
    alb = np.broadcast_to(alb, (*reg.shape, 3))[box] if alb.ndim == 3 else alb * np.ones((*sub.shape, 1), F32)
    if light is None:
        cv.over(sub, alb, c)
        return
    nn = normals(sub, reg.full(d)[box], thick, cv.px) if n is None else (n[0][box], n[1][box], n[2][box])
    cv.over(sub, light.shade(sub, alb, nn, **shade), c)
