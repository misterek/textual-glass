"""The smooth viewscreen: a pane of glass onto space, with a nebula, star dust, stars that twinkle, a moon
and a ringed planet drawn the way textual-glass's worlds are (a lit sphere with banded cloud, a darker limb,
an atmosphere that glows on the day side), and a glass sheen over it. Red alert tints it red and pulses a
glow round its edge. Pure numpy.

It is drawn at its true size, from about 190 x 29 px up to about 1600 x 270 px (a big Retina window), at
the output's own pixels: every edge is anti-aliased from its distance, the nebula is worked out at a coarser
grid and smoothed up (it is soft anyway), and the stars are splatted all at once. A bigger screen gets more
stars and finer detail, not a scaled-up small one. Every size has the same ``FRAMES`` frames and gap, so
tiles cut from one picture and played as separate terminal animations stay in step. Only the whole
picture's outermost ring is transparent (glass rule 17).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ._paint import F32, MIN_ALPHA, PACKING, WHITE, Array, Noise, empty_frame, mix, sd_box, smoothstep

FRAMES = 4
CALM_GAP, ALERT_GAP = 900, 650
#: The nebula's grid is at most this many cells wide (it is smoothed up to the picture's size).
NEBULA_CELLS = 420
SUN = np.array([-0.62, -0.42, 0.66], F32) / np.linalg.norm(np.array([-0.62, -0.42, 0.66], F32))


@dataclass
class _Stars:
    x: Array
    y: Array
    size: Array
    bright: Array
    phase: Array
    twinkle: Array  # bool: whether it twinkles
    spiky: Array  # bool: the few bright ones with spikes


@dataclass
class _Base:
    sky: Array  # (h, w, 3): space, nebula, planet, moon, dust
    inside: Array  # (h, w): the pane's coverage
    glass: Array  # (h, w, 3): the sheen and the lit top edge, added over everything
    bezel: Array  # (h, w): the frame's coverage
    vignette: Array  # (h, w): how near the pane's edge (for red alert)
    lit: Array  # (h, w): the bezel's light from above
    stars: _Stars


def viewscreen(colors: dict[str, Array], alert: bool, width: int, height: int) -> tuple[list[bytes], int]:
    gap = ALERT_GAP if alert else CALM_GAP
    if width < 6 or height < 5:
        return [empty_frame(width, height)] * FRAMES, gap
    base = _base(colors, width, height)
    red = colors["alert"]
    frames = []
    for f in range(FRAMES):
        img = base.sky.copy()
        img += _star_layer(base.stars, width, height, f)[..., None] * colors["star"]
        if alert:
            pulse = (1.0, 0.72, 0.45, 0.72)[f]
            lum = img @ np.array([0.3, 0.55, 0.15], F32)
            img *= 0.32
            img += red * (lum * 1.25 + 0.05 + 0.05 * pulse)[..., None]
            img += red * (base.vignette * (0.25 + 0.4 * pulse))[..., None]
        img += base.glass if not alert else base.glass * mix(WHITE, red + 0.3, 0.6)
        bez = colors["bezel"] if not alert else mix(colors["bezel"], red, 0.45 + 0.25 * (f % 2 == 0))
        bez_c = bez * (1.0 + 0.6 * base.lit)[..., None]
        k = base.bezel[..., None]
        img = img * (1 - k) + bez_c * k
        frames.append(_encode(img, np.maximum(base.inside, base.bezel * 0.95)))
    return frames, gap


def _encode(img: Array, alpha: Array) -> bytes:
    """Straight-alpha RGBA bytes, packed as the crew's frames are (``_paint.PACKING``), the outer ring clear."""
    h, w = alpha.shape
    out = np.empty((h, w, 4), np.uint8)
    out[..., :3] = np.clip(img * 255 + 0.5, 0, 255)
    out[..., 3] = np.clip(alpha * 255 + 0.5, 0, 255)
    flat = out.reshape(-1, 4)
    flat.view(np.uint32)[:, 0] &= PACKING[flat[:, 3]]
    out[0], out[-1], out[:, 0], out[:, -1] = 0, 0, 0, 0
    out[out[..., 3] < MIN_ALPHA] = 0
    return out.tobytes()


def _upsample(a: Array, h: int, w: int) -> Array:
    """``a`` (rows, cols, ...) smoothed up to (h, w): bilinear, cell centres on pixel centres."""
    rows, cols = a.shape[:2]
    ys = np.clip((np.arange(h, dtype=F32) + 0.5) * rows / h - 0.5, 0, rows - 1)
    xs = np.clip((np.arange(w, dtype=F32) + 0.5) * cols / w - 0.5, 0, cols - 1)
    y0, x0 = np.floor(ys).astype(np.intp), np.floor(xs).astype(np.intp)
    y1, x1 = np.minimum(y0 + 1, rows - 1), np.minimum(x0 + 1, cols - 1)
    fy, fx = (ys - y0)[:, None], (xs - x0)[None, :]
    if a.ndim == 3:
        fy, fx = fy[..., None], fx[..., None]
    top = a[y0][:, x0] * (1 - fx) + a[y0][:, x1] * fx
    bottom = a[y1][:, x0] * (1 - fx) + a[y1][:, x1] * fx
    return (top * (1 - fy) + bottom * fy).astype(F32)


def _base(colors: dict[str, Array], w: int, h: int) -> _Base:
    """Everything that is the same in every frame."""
    xs = np.arange(w, dtype=F32)[None, :] + 0.5
    ys = np.arange(h, dtype=F32)[:, None] + 0.5
    # the pane: a rounded rectangle a pixel in from the edge, a bezel round it
    radius = min(0.22 * h, 0.05 * w, 18.0)
    d = sd_box(xs, ys, w / 2, h / 2, w / 2 - 1.0, h / 2 - 1.0, radius)
    bw = max(1.1, h / 70)
    bezel = np.clip(0.5 - (np.abs(d + bw * 0.55) - bw * 0.55), 0, 1).astype(F32)
    inside = np.clip(0.5 - d, 0, 1).astype(F32)
    vignette = (1 - smoothstep(0.0, 1.0, np.clip(-d / max(1.0, 0.32 * h), 0, 1))).astype(F32)
    lit = np.broadcast_to(smoothstep(h * 0.6, 0.0, ys), (h, w)).astype(F32)

    # space, top to bottom, and a nebula worked out on a coarser grid
    t = np.clip(ys / h, 0, 1) ** 0.85
    sky = np.empty((h, w, 3), F32)
    sky[:] = colors["space_top"] * (1 - t[..., None]) + colors["space_bottom"] * t[..., None]
    cell = max(1, math.ceil(w / NEBULA_CELLS))
    gw, gh = math.ceil(w / cell), max(2, math.ceil(h / cell))
    gx = (np.arange(gw, dtype=F32)[None, :] + 0.5) * cell
    gy = (np.arange(gh, dtype=F32)[:, None] + 0.5) * cell
    noise = Noise(1009, n=64)
    scale = 3.2 / max(h, 1)
    n1 = noise.fbm(gx * scale, gy * scale, octaves=5)
    n2 = noise.fbm(gx * scale * 1.7 + 7.1, gy * scale * 1.7 + 3.3, octaves=4)
    cloud = smoothstep(0.42, 0.8, n1) * np.exp(-(((gx / w - 0.3) / 0.38) ** 2))
    wisp = smoothstep(0.5, 0.85, n2) * 0.6
    glow = smoothstep(0.55, 0.9, n1 * n2 * 1.8)
    neb = (colors["nebula"] * (cloud * 0.45 + wisp * 0.12)[..., None]
           + mix(colors["nebula"], colors["planet"], 0.5) * (glow * 0.12)[..., None])
    sky += _upsample(neb.astype(F32), h, w)

    # star dust: many faint points (more on a bigger screen), fixed
    rnd = np.random.default_rng(31 * w + h)
    dust_n = int(w * h / 55)
    dust = _splat(rnd.uniform(1, w - 1, dust_n), rnd.uniform(1, h - 1, dust_n),
                  np.full(dust_n, 0.45, F32), (0.04 + 0.2 * rnd.random(dust_n) ** 3).astype(F32), w, h, reach=1)
    sky += dust[..., None] * colors["star"]

    pr = max(2.5, 0.4 * h)
    pcx, pcy = w - max(2.3 * pr, 0.14 * w), h * 0.58
    _planet(sky, colors, pcx, pcy, pr)
    mr = max(1.2, 0.09 * h)
    mx, my = w * 0.12 + 2 * mr, h * 0.3
    _moon(sky, colors, mx, my, mr)

    # the stars: their places, sizes and phases; none in front of the planet, its ring or the moon
    count = max(6, int(w * h / 150))
    sx, sy = rnd.uniform(1.5, w - 1.5, count), rnd.uniform(1.5, h - 1.5, count)
    bright = rnd.random(count) ** 2.2
    keep = _clear(sx, sy, pcx, pcy, pr) & (np.hypot(sx - mx, sy - my) > 1.6 * mr)
    sx, sy, bright = sx[keep], sy[keep], bright[keep]
    px = max(1.0, h / 76)  # bigger screens have a little bigger stars, never blobs
    stars = _Stars(sx.astype(F32), sy.astype(F32), ((0.42 + 0.4 * bright) * min(px, 1.6)).astype(F32),
                   (0.25 + 0.85 * bright).astype(F32), rnd.random(len(sx)).astype(F32), bright < 0.9,
                   bright > 0.8)

    # glass: a reflection across the pane at 45 degrees, a lit top edge
    diag = (xs - w * 0.16) - ys
    sheen = (np.exp(-((diag / max(2.0, 0.35 * h)) ** 2)) * 0.05
             + np.exp(-(((diag - 0.8 * h) / max(1.0, 0.1 * h)) ** 2)) * 0.03)
    top = np.exp(-(((ys - 1.0) / max(1.2, h / 60)) ** 2)) * 0.16
    glass = ((sheen + top) * inside)[..., None] * WHITE
    return _Base(sky, inside, glass.astype(F32), bezel, vignette, lit, stars)


def _clear(x: Array, y: Array, pcx: float, pcy: float, pr: float) -> Array:
    """Whether a point is clear of the planet and its ring (so a star may show there)."""
    dx, dy = (x - pcx) / pr, (y - pcy) / pr
    ex, ey = _ring_frame(dx, dy)
    q = np.sqrt(ex * ex + (ey / 0.27) ** 2)
    return (np.hypot(dx, dy) > 1.18) & ~((q > 1.25) & (q < 2.12))


def _ring_frame(dx: Array, dy: Array) -> tuple[Array, Array]:
    tilt = -0.32
    return dx * math.cos(tilt) - dy * math.sin(tilt), dx * math.sin(tilt) + dy * math.cos(tilt)


def _planet(sky: Array, colors: dict[str, Array], pcx: float, pcy: float, pr: float) -> None:
    """The ringed planet, lit from the upper left, drawn into ``sky`` within its own box."""
    h, w = sky.shape[:2]
    x0, x1 = max(0, int(pcx - 2.25 * pr)), min(w, int(pcx + 2.25 * pr) + 1)
    y0, y1 = max(0, int(pcy - 1.35 * pr)), min(h, int(pcy + 1.35 * pr) + 1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return
    box = sky[y0:y1, x0:x1]
    dx = ((np.arange(x0, x1, dtype=F32) + 0.5 - pcx) / pr)[None, :]
    dy = ((np.arange(y0, y1, dtype=F32) + 0.5 - pcy) / pr)[:, None]
    rr = dx * dx + dy * dy
    r = np.sqrt(rr)
    disc = np.clip((1 - r) * pr + 0.5, 0, 1)
    nz = np.sqrt(np.clip(1 - rr, 0, 1))
    mu = dx * SUN[0] + dy * SUN[1] + nz * SUN[2]
    day = smoothstep(-0.12, 0.45, mu)
    lat = dy * 0.96 + dx * 0.28
    noise = Noise(77, n=64)
    detail = 2 if pr > 40 else 1  # a big planet shows finer cloud
    bands = noise.fbm(dx * 1.6 + 3.0, lat * 9.0, octaves=2 + detail)
    swirl = noise.fbm(dx * 6.0 + 1.0, lat * 22.0, octaves=2) - 0.5 if detail > 1 else 0.0
    band_t = np.clip(0.35 + 0.9 * (bands - 0.3) + 0.12 * np.sin(lat * 13.0) + 0.25 * swirl, 0, 1)
    band_c = colors["planet_dark"] * (1 - band_t[..., None]) + colors["planet"] * band_t[..., None]
    lit = band_c * (0.3 + 0.85 * np.clip(mu, 0, 1))[..., None]
    body = colors["planet_dark"] * 0.35 * (1 - day[..., None]) + lit * day[..., None]
    body = body * (0.62 + 0.38 * np.clip(nz, 0, 1) ** 0.4)[..., None]
    sky_c = mix(colors["planet"], WHITE, 0.45)
    side = smoothstep(-0.4, 0.7, -(dx * 0.62 + dy * 0.42))
    rim_in = np.exp(-(1 - np.minimum(r, 1)) / 0.07) * (r <= 1)
    body = body + (sky_c - body) * (rim_in * (0.1 + 0.55 * side))[..., None]
    halo = np.exp(-np.maximum(r - 1, 0) / 0.05) * smoothstep(1.16, 1.0, r) * (r > 1)
    ex, ey = _ring_frame(dx, dy)
    q = np.sqrt(ex * ex + (ey / 0.27) ** 2)
    fine = min(70.0, 0.9 * pr)  # more ringlets when there are pixels for them (never finer than ~3 px)
    ring = smoothstep(1.32, 1.42, q) * smoothstep(2.05, 1.9, q) * (0.8 + 0.2 * np.sin(q * fine))
    ring = ring * (1 - 0.7 * np.exp(-(((q - 1.68) / 0.03) ** 2)))
    ring_back = ring * ~((ey < 0) & (r < 1.0)) * (ey < 0)
    ring_front = ring * (ey >= 0)
    ring_c = colors["ring"] * (0.55 + 0.45 * smoothstep(1.0, -0.6, ex))[..., None]
    box[:] = box + (ring_c - box) * (ring_back * 0.75)[..., None]
    box += sky_c * (halo * (0.15 + 0.6 * side))[..., None]
    box[:] = box + (body - box) * disc[..., None]
    # the planet's shadow on the near ring: away from the sun, within a radius of the line through its centre
    soft = 2.0 / pr
    away = smoothstep(-soft, soft, dx * 0.83 + dy * 0.56)
    within = smoothstep(1.0 + soft, 1.0 - soft, np.abs(dx * -0.56 + dy * 0.83))
    shade = 1 - 0.6 * away * within
    box[:] = box + (ring_c * shade[..., None] - box) * (ring_front * 0.8)[..., None]


def _moon(sky: Array, colors: dict[str, Array], mx: float, my: float, mr: float) -> None:
    h, w = sky.shape[:2]
    x0, x1 = max(0, int(mx - mr - 2)), min(w, int(mx + mr + 3))
    y0, y1 = max(0, int(my - mr - 2)), min(h, int(my + mr + 3))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return
    box = sky[y0:y1, x0:x1]
    dx = (np.arange(x0, x1, dtype=F32)[None, :] + 0.5 - mx) / mr
    dy = (np.arange(y0, y1, dtype=F32)[:, None] + 0.5 - my) / mr
    md = np.sqrt(dx * dx + dy * dy)
    cov = np.clip((1 - md) * mr + 0.5, 0, 1)
    lit = smoothstep(-0.2, 0.6, -(dx * 0.7 + dy * 0.5))
    c = colors["planet_dark"] * 0.6 * (1 - lit[..., None]) + colors["star"] * 0.75 * lit[..., None]
    box[:] = box + (c - box) * cov[..., None]


def _splat(x: Array, y: Array, size: Array, bright: Array, w: int, h: int, reach: int = 2) -> Array:
    """Soft points at (x, y), each a little gaussian ``size`` px across, added into one (h, w) layer at once."""
    ix, iy = np.floor(x).astype(np.intp), np.floor(y).astype(np.intp)
    idx, wts = [], []
    for oy in range(-reach, reach + 1):
        for ox in range(-reach, reach + 1):
            px, py = ix + ox, iy + oy
            ok = (px >= 0) & (px < w) & (py >= 0) & (py < h)
            d2 = (px + 0.5 - x) ** 2 + (py + 0.5 - y) ** 2
            idx.append((py * w + px)[ok])
            wts.append((np.exp(-d2 / (size * size)) * bright)[ok])
    flat = np.bincount(np.concatenate(idx), np.concatenate(wts), minlength=w * h)
    return flat.reshape(h, w).astype(F32)


def _star_layer(stars: _Stars, w: int, h: int, f: int) -> Array:
    """This frame's stars: the twinklers at their own phase, the brightest with four soft spikes."""
    tw = np.where(stars.twinkle, 0.55 + 0.45 * np.cos(2 * math.pi * (f / FRAMES + stars.phase)), 1.0)
    layer = _splat(stars.x, stars.y, stars.size, (stars.bright * tw).astype(F32), w, h)
    spiky = np.flatnonzero(stars.spiky)
    for i in spiky:
        x, y, s, k = float(stars.x[i]), float(stars.y[i]), float(stars.size[i]), float(stars.bright[i] * tw[i])
        reach = max(3.0, s * 7.0)
        x0, x1 = max(0, int(x - reach)), min(w, int(x + reach) + 1)
        y0, y1 = max(0, int(y - reach)), min(h, int(y + reach) + 1)
        dx = np.arange(x0, x1, dtype=F32)[None, :] + 0.5 - x
        dy = np.arange(y0, y1, dtype=F32)[:, None] + 0.5 - y
        r = np.sqrt(dx * dx + dy * dy)
        arms = (np.exp(-np.abs(dx) / 0.45) + np.exp(-np.abs(dy) / 0.45)) * np.exp(-r / (0.9 * reach / 3))
        layer[y0:y1, x0:x1] += (arms * 0.5 * smoothstep(reach, reach * 0.55, r) * k).astype(F32)
    return layer
