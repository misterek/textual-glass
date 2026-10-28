"""Small images the terminal shows or plays by itself: scrims, sprites and the
ambient effects of ``full`` motion. No Textual imports.

Makers return straight-alpha RGBA frames as bytes. **Every sprite frame is fully
transparent at its border.** A sprite fills whole cells; light that reaches the
edge of its canvas is cut off there and shows as a tinted square. Round sprites
are multiplied by :func:`radial_mask`, sprites that fill their box (a halo, a
glint's spikes) by :func:`edge_fade`. ``tests/test_units.py`` checks every
frame of every maker at several sizes; check a new one the same way,
mid-animation included.

A border that lies on the window's edge is the exception, because the backdrop
ends there too and nothing shows a cut: the scrims and the shimmer cover the
whole window, and a halo keeps its light on a side where it meets the window.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from .render import F32, Array, Colour, Grid, Rect, box_fade, col, rgb_of, sd_rrect, smoothstep, to_u8

BEACON_STATES = ("idle", "working", "waiting", "error", "done")


# ================================================================ scrims ==


def solid(color: Colour, alpha: float) -> bytes:
    """One RGBA pixel (scrims are a 1x1 image stretched over the window)."""
    r, g, b = rgb_of(color)
    return bytes([r, g, b, max(0, min(255, int(round(alpha * 255))))])


def solid_frames(color: Colour, alphas: Sequence[float]) -> list[bytes]:
    """1x1 RGBA frames, one per alpha: a scrim's fade steps."""
    return [solid(color, a) for a in alphas]


# =============================================================== helpers ==


def _coords(width: int, height: int) -> tuple[Array, Array]:
    xs = np.arange(width, dtype=F32)[None, :] + F32(0.5)
    ys = np.arange(height, dtype=F32)[:, None] + F32(0.5)
    return xs, ys


def _premul_to_rgba(rgb_: Array, a: Array) -> bytes:
    out = rgb_ / np.maximum(a, 1e-4)[..., None]
    data = np.concatenate([out, a[..., None]], 2)
    return (np.clip(data, 0, 1) * 255 + 0.5).astype(np.uint8).tobytes()


def _ambient_rgba(color: Array, alpha: Array) -> bytes:
    """A colour (one, or one per pixel) at per-pixel alpha, as RGBA bytes, the
    way the ambient effects have always made them: through ``to_u8``, which
    reads the colour as premultiplied, so faint pixels come out brighter."""
    h, w = alpha.shape
    rgba = np.concatenate([np.broadcast_to(color, (h, w, 3)), alpha[..., None]], 2)
    return to_u8(np.ascontiguousarray(rgba)).tobytes()


def radial_mask(width: int, height: int, inner: float = 0.72) -> Array:
    """1 in the middle, 0 from one pixel inside the canvas's inscribed circle
    outwards. Round sprites are multiplied by it: a sprite is drawn in a box of
    whole cells, and any light that reaches the box's edge is cut off there and
    shows as a tinted square."""
    xs, ys = _coords(width, height)
    d = np.sqrt((xs - width / 2) ** 2 + (ys - height / 2) ** 2)
    R = max(1.0, min(width, height) / 2 - 1.0)
    return smoothstep(R, R * inner, d).astype(F32)


def edge_fade(width: int, height: int, margin: float,
              window_edges: tuple[bool, bool, bool, bool] = (False, False, False, False)) -> Array:
    """1 inside, 0 in the outermost pixel ring, easing over ``margin`` pixels:
    for sprites that fill their box, where a round mask would cut too much.
    Sides marked in ``window_edges`` (top, right, bottom, left) lie on the
    window's edge and are not faded."""
    xs, ys = _coords(width, height)
    far = float(width + height) + margin + 2.0
    top, right, bottom, left = window_edges
    box = (-far if left else 0.0, -far if top else 0.0, width + far if right else float(width),
           height + far if bottom else float(height))
    return box_fade(xs, ys, box, margin)


def dim_frame(rgba: bytes, factor: float) -> bytes:
    """A copy of an RGBA frame with its colour scaled by ``factor`` (alpha kept):
    the frame a sprite shows under a popup's scrim."""
    pixels = np.frombuffer(rgba, np.uint8).reshape(-1, 4).astype(F32)
    pixels[:, :3] *= factor
    return (np.clip(pixels, 0, 255) + 0.5).astype(np.uint8).tobytes()


def frames_as_arrays(frames: Sequence[bytes], width: int, height: int) -> list[np.ndarray]:
    return [np.frombuffer(f, np.uint8).reshape(height, width, 4) for f in frames]


# =============================================================== sprites ==


def beacon_frames(width: int, height: int, state: str, color: Colour) -> tuple[list[bytes], int]:
    """An animated status beacon, as RGBA frames and a frame gap (ms).

    * ``idle``: a dim dot inside a faint ring, still (one frame).
    * ``working``: a bright core with a satellite on a slow orbit.
    * ``waiting``: a bright core that pulses, with expanding rings ("ping").
    * ``error``: a core with a slow throb and a cross.
    * ``done``: a bright core, still.
    """
    xs, ys = _coords(width, height)
    cx, cy = width / 2, height / 2
    R = min(width, height) / 2
    d = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
    a = col(color)
    white = np.ones(3, F32)
    frames: list[bytes] = []

    mask = radial_mask(width, height)

    def compose(layers: list[tuple[Array, Array]]) -> bytes:
        rgb_ = np.zeros((height, width, 3), F32)
        alpha = np.zeros((height, width), F32)
        for colour, cover in layers:
            cover = np.clip(cover, 0, 1)
            rgb_ = rgb_ * (1 - cover[..., None]) + np.broadcast_to(colour, rgb_.shape) * cover[..., None]
            alpha = alpha * (1 - cover) + cover
        return _premul_to_rgba(rgb_ * mask[..., None], alpha * mask)

    core_r = R * 0.3
    ring_r = R * 0.72
    if state in ("idle", "done"):
        layers = [(a, np.clip(1.0 - np.abs(d - ring_r), 0, 1) * 0.35),
                  (a, np.exp(-np.maximum(d - core_r, 0) / (R * 0.12)) * (d > core_r) * 0.25),
                  (a * 0.7 + 0.3 * white, np.clip(core_r + 0.5 - d, 0, 1) * (0.55 if state == "idle" else 0.95))]
        return [compose(layers)], 1000
    if state == "working":
        n, gap = 24, 90
        ang = np.arctan2(ys - cy, xs - cx)
        for f in range(n):
            phi = 2 * math.pi * f / n - math.pi
            sx_, sy_ = cx + ring_r * math.cos(phi), cy + ring_r * math.sin(phi)
            sd = np.sqrt((xs - sx_) ** 2 + (ys - sy_) ** 2)
            lag = (phi - ang) % (2 * math.pi)
            trail = np.clip(1.0 - np.abs(d - ring_r), 0, 1) * np.exp(-lag / 1.4)
            layers = [(a, np.clip(1.0 - np.abs(d - ring_r), 0, 1) * 0.22),
                      (a, trail * 0.8),
                      (a, np.exp(-np.maximum(d - core_r, 0) / (R * 0.12)) * (d > core_r) * 0.35),
                      (a * 0.6 + 0.4 * white, np.clip(core_r + 0.5 - d, 0, 1)),
                      (white, np.clip(R * 0.13 + 0.5 - sd, 0, 1)),
                      (a, np.exp(-sd / (R * 0.12)) * 0.6)]
            frames.append(compose(layers))
        return frames, gap
    if state == "waiting":
        n, gap = 20, 70
        for f in range(n):
            t = f / n
            pulse = 0.5 - 0.5 * math.cos(2 * math.pi * t)
            ping_r = core_r + (R * 0.98 - core_r) * t
            ping = np.clip(1.4 - np.abs(d - ping_r), 0, 1) * (1 - t) ** 1.3
            ping2_r = core_r + (R * 0.98 - core_r) * ((t + 0.5) % 1)
            ping2 = np.clip(1.4 - np.abs(d - ping2_r), 0, 1) * (1 - (t + 0.5) % 1) ** 1.3
            halo = np.exp(-np.maximum(d - core_r, 0) / (R * (0.18 + 0.12 * pulse))) * (d > core_r)
            layers = [(a, ping * 0.9), (a, ping2 * 0.9), (a, halo * (0.35 + 0.4 * pulse)),
                      (a * 0.5 + 0.5 * white, np.clip(core_r * (1 + 0.15 * pulse) + 0.5 - d, 0, 1))]
            frames.append(compose(layers))
        return frames, gap
    # error
    n, gap = 16, 110
    for f in range(n):
        pulse = 0.5 - 0.5 * math.cos(2 * math.pi * f / n)
        halo = np.exp(-np.maximum(d - core_r, 0) / (R * 0.2)) * (d > core_r)
        diagonals = (np.clip(1.2 - np.abs((xs - cx) - (ys - cy)) * 0.7071, 0, 1)
                     + np.clip(1.2 - np.abs((xs - cx) + (ys - cy)) * 0.7071, 0, 1))
        cross = diagonals * (d < ring_r * 0.95) * (d > core_r * 1.4)
        layers = [(a, np.clip(1.0 - np.abs(d - ring_r), 0, 1) * (0.25 + 0.35 * pulse)),
                  (a, np.clip(cross, 0, 1) * 0.35),
                  (a, halo * (0.2 + 0.35 * pulse)),
                  (a * 0.7 + 0.3 * white, np.clip(core_r + 0.5 - d, 0, 1))]
        frames.append(compose(layers))
    return frames, gap


def pulse_frames(width: int, height: int, color: Colour, frames: int = 16, gap: int = 85) -> tuple[list[bytes], int]:
    """A dot whose halo throbs, with a ring that swells and fades: marks
    something stuck or failing. Everything stays inside the inscribed circle."""
    xs, ys = _coords(width, height)
    d = np.sqrt((xs - width / 2) ** 2 + (ys - height / 2) ** 2)
    R = max(2.0, min(width, height) / 2 - 1.0)
    r = R * 0.36
    a = col(color)
    mask = radial_mask(width, height, inner=0.6)
    core = np.clip(r + 0.5 - d, 0, 1)
    colour = a[None, None, :] * (1 - core[..., None] * 0.45) + core[..., None] * 0.45
    out = []
    for f in range(frames):
        pulse = 0.5 - 0.5 * math.cos(2 * math.pi * f / frames)
        halo = np.exp(-np.maximum(d - r, 0) / (R * (0.14 + 0.16 * pulse))) * (d > r)
        ring = np.clip(1.2 - np.abs(d - r * (1.25 + 0.9 * pulse)), 0, 1) * (1 - pulse) * 0.8
        cover = np.clip(np.maximum(np.maximum(core, halo * (0.25 + 0.45 * pulse)), ring), 0, 1) * mask
        out.append(_premul_to_rgba(colour * cover[..., None], cover))
    return out, gap


# ================================================== ambient (full motion) ==


def halo_frames(g: Grid, rect: Rect, cols: int, rows: int, c1: Colour, c2: Colour,
                frames: int = 16, res: float = 0.5) -> tuple[Rect, int, int, list[bytes]]:
    """A pulsing halo around a focused panel, at low resolution (it is soft anyway).

    Returns the cell rectangle it covers, its pixel size, and RGBA frames.
    """
    x, y, w, h = rect
    cx0, cy0 = max(0, x - 2), max(0, y - 1)
    cx1, cy1 = min(cols, x + w + 2), min(rows, y + h + 1)
    gsx, gsy = g.sx * res, g.sy * res
    width, height = max(1, round((cx1 - cx0) * gsx)), max(1, round((cy1 - cy0) * gsy))
    xs = (np.arange(width, dtype=F32)[None, :] + 0.5) / gsx + cx0
    ys = (np.arange(height, dtype=F32)[:, None] + 0.5) / gsy + cy0
    left, right, top, bottom = x + 0.5, x + w - 0.5, y + 0.5, y + h - 0.5
    k = g.sx / g.sy  # measure in row units, so the glow is round on screen
    d = sd_rrect(xs * k, ys, (left + right) / 2 * k, (top + bottom) / 2, (right - left) / 2 * k,
                 (bottom - top) / 2, min(0.55, (right - left) / 2 * k, (bottom - top) / 2))
    # fades out over the last half row (a cell and a half at the sides) of its canvas,
    # except where the canvas meets the window's edge
    window_edges = (cy0 == 0, cx1 == cols, cy1 == rows, cx0 == 0)
    halo = np.exp(-np.maximum(d, 0) / 0.6) * (d > -0.05) * edge_fade(width, height, 0.5 * gsy, window_edges)
    u = np.clip((xs - left) / max(1.0, right - left), 0, 1)[..., None]
    color = col(c1) * (1 - u) + col(c2) * u
    out = []
    for f in range(frames):
        s = 0.5 - 0.5 * math.cos(2 * math.pi * f / frames)
        out.append(_ambient_rgba(color, halo * (0.05 + 0.3 * s)))
    return (cx0, cy0, cx1 - cx0, cy1 - cy0), width, height, out


def glint_frames(cell_w: float, cell_h: float, frames: int, phase: float,
                 color: Colour) -> tuple[int, int, list[bytes]]:
    """A small four-point glint, one cell big, that flashes once per loop."""
    width, height = max(3, round(cell_w)), max(3, round(cell_h))
    xs, ys = _coords(width, height)
    dx, dy = np.abs(xs - width / 2), np.abs(ys - height / 2)
    s = min(width, height) / 2
    core = np.exp(-(dx * dx + dy * dy) / (0.16 * s) ** 2)
    spikes = (np.exp(-dy / (0.05 * s + 0.3)) * np.exp(-dx / (0.6 * s))
              + np.exp(-dx / (0.05 * s + 0.3)) * np.exp(-dy / (0.8 * s)))
    shape = (core * 0.9 + spikes * 0.8) * edge_fade(width, height, 0.25 * s)  # the spikes end inside the cell
    c = col(color)
    out = []
    for f in range(frames):
        t = ((f / frames) - phase) % 1.0
        flash = math.exp(-((t - 0.5) / 0.07) ** 2)
        out.append(_ambient_rgba(c, np.clip(shape * flash, 0, 1)))
    return width, height, out


def shimmer_frames(cols: int, rows: int, frames: int, color: Colour, res: float = 2.0) -> tuple[int, int, list[bytes]]:
    """A broad, very faint diagonal sheen that sweeps across the window once per
    loop. Tiny (``res`` pixels per cell): the terminal stretches it over the
    whole window, so its border is the window's edge."""
    width, height = max(2, round(cols * res)), max(2, round(rows * res * 2))
    xs = (np.arange(width, dtype=F32)[None, :] + 0.5) / width
    ys = (np.arange(height, dtype=F32)[:, None] + 0.5) / height
    diag = xs * 0.8 + ys * 0.35
    c = col(color)
    out = []
    for f in range(frames):
        pos = -0.35 + 1.6 * f / max(1, frames - 1)
        out.append(_ambient_rgba(c, np.exp(-(((diag - pos) / 0.11) ** 2)) * 0.07))
    return width, height, out
