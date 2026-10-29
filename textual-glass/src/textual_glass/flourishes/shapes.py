"""Shapes: flourishes that follow the anchor's outline or sky. No Textual imports.

Each one is drawn around the anchor's panel border, the rounded rectangle glass
draws through the middle of the anchor's outermost cells (:func:`_outline`);
over the whole window, the eclipse and the constellation move into its sky.
Light goes into small windows of the stage where it can, and every frame is
packed to upload small (:func:`_frame`).
"""

from __future__ import annotations

import math

import numpy as np

from ..render import F32, Array, col, sd_rrect, smoothstep
from . import Flourish, Stage
from ._draw import Light, blank, border, bump, ease_in_out, ease_out, gaps, glow, mix, rng, segment, unit

WHITE = np.ones(3, F32)
BLACK = np.zeros(3, F32)
TAU = 2 * math.pi

#: A window of the stage: its slices, and its pixel centres (a row and a column).
Win = tuple[tuple[slice, slice], Array, Array]
_ROUND = np.array([0xFC, 0xFC, 0xFC, 0xFF], np.uint8)  # colours to 6 bits, alpha kept


# ================================================================ helpers ==


def _window(stage: Stage, x0: float, y0: float, x1: float, y1: float) -> Win | None:
    """The part of the stage between ``x0, y0`` and ``x1, y1``, or None if it is off the stage."""
    i0, i1 = max(0, int(y0)), min(stage.height, math.ceil(y1))
    j0, j1 = max(0, int(x0)), min(stage.width, math.ceil(x1))
    if i0 >= i1 or j0 >= j1:
        return None
    xs = np.arange(j0, j1, dtype=F32)[None, :] + F32(0.5)
    ys = np.arange(i0, i1, dtype=F32)[:, None] + F32(0.5)
    return (slice(i0, i1), slice(j0, j1)), xs, ys


def _add(light: Light, where: tuple[slice, slice], color: Array, amount: Array) -> None:
    """``Light.add`` into one window of the frame."""
    k = np.clip(amount, 0, None)
    light.rgb[where] += color * k[..., None]
    a = light.a[where]
    a[...] = 1 - (1 - a) * (1 - np.minimum(k, 1))


def _outline(stage: Stage) -> tuple[float, float, float, float, float]:
    """The anchor's panel border: centre, half sizes and corner radius, where glass draws it."""
    x, y, w, h = stage.at
    cw, ch = stage.cell
    left, top = x + 0.5 * cw, y + 0.5 * ch
    right, bottom = max(left, x + w - 0.5 * cw), max(top, y + h - 0.5 * ch)
    hw, hh = (right - left) / 2, (bottom - top) / 2
    return (left + right) / 2, (top + bottom) / 2, hw, hh, min(0.55 * ch, hw, hh)


def _room(stage: Stage, x0: float, y0: float, x1: float, y1: float, margin: float,
          edge: float = 0.0) -> tuple[float, float, float, float]:
    """A box clamped to the stage: ``margin`` inside each side, or ``edge`` inside
    the sides on the window's edge (light may reach those)."""
    top, right, bottom, left = stage.window_edges
    lo_x, lo_y = (edge if left else margin), (edge if top else margin)
    hi_x, hi_y = stage.width - (edge if right else margin), stage.height - (edge if bottom else margin)
    x0, x1 = min(max(x0, lo_x), hi_x), max(min(x1, hi_x), lo_x)
    y0, y1 = min(max(y0, lo_y), hi_y), max(min(y1, hi_y), lo_y)
    return x0, y0, max(x0, x1), max(y0, y1)


class _Loop:
    """A rounded rectangle walked clockwise by arc length, from the middle of its top-left corner."""

    def __init__(self, cx: float, cy: float, hw: float, hh: float, r: float) -> None:
        self.cx, self.cy = cx, cy
        self.hw, self.hh = hw, hh
        self.r = r = max(0.0, min(r, hw, hh))
        self.a, self.b = hw - r, hh - r
        self.q = q = 0.5 * math.pi * r
        self.length = max(1e-3, 4 * q + 4 * self.a + 4 * self.b)

    def corners(self) -> list[float]:
        """Where the middles of the four corners are: top left, top right, bottom right, bottom left."""
        q, a, b = self.q, self.a, self.b
        return [0.0, q + 2 * a, 2 * q + 2 * a + 2 * b, 3 * q + 4 * a + 2 * b]

    def point(self, s: float) -> tuple[float, float, float, float]:
        """The point at arc length ``s``, and the outward normal there."""
        q, a, b, r = self.q, self.a, self.b, self.r
        s = s % self.length
        # the pieces in order: half the top-left corner, the top, the top-right corner, the right side...
        pieces: list[tuple[float, float, float, float]] = [
            (q / 2, -a, -b, 1.25 * math.pi), (2 * a, -a, -self.hh, 0.0), (q, a, -b, 1.5 * math.pi),
            (2 * b, self.hw, -b, 0.0), (q, a, b, 0.0), (2 * a, a, self.hh, 0.0), (q, -a, b, 0.5 * math.pi),
            (2 * b, -self.hw, b, 0.0), (q / 2, -a, -b, math.pi)]
        sides = {1: (1.0, 0.0, 0.0, -1.0), 3: (0.0, 1.0, 1.0, 0.0), 5: (-1.0, 0.0, 0.0, 1.0), 7: (0.0, -1.0, -1.0, 0.0)}
        for i, (length, px, py, start) in enumerate(pieces):
            if s <= length or i == len(pieces) - 1:
                if i in sides:
                    tx, ty, nx, ny = sides[i]
                    return self.cx + px + tx * s, self.cy + py + ty * s, nx, ny
                angle = start + (s / r if r > 0 else 0.0)
                nx, ny = math.cos(angle), math.sin(angle)
                return self.cx + px + r * nx, self.cy + py + r * ny, nx, ny
            s -= length
        raise AssertionError  # pragma: no cover

    def param(self, xs: Array, ys: Array) -> Array:
        """Each pixel's arc length to the nearest point of the outline (0 to ``length``)."""
        q, a, b, r = self.q, self.a, self.b, self.r
        px, py = np.broadcast_arrays(xs - self.cx, ys - self.cy)
        qx, qy = np.clip(px, -a, a), np.clip(py, -b, b)
        ox, oy = px - qx, py - qy
        # sides: the one that is nearer, left or right against top or bottom
        lr = (self.hw - np.abs(px)) < (self.hh - np.abs(py))
        s_lr = np.where(px > 0, 1.5 * q + 2 * a + (qy + b), 3.5 * q + 4 * a + 2 * b + (b - qy))
        s_tb = np.where(py < 0, 0.5 * q + qx + a, 2.5 * q + 2 * a + 2 * b + (a - qx))
        s = np.where(lr, s_lr, s_tb)
        corner = (ox != 0) & (oy != 0)
        if r > 0 and corner.any():
            th = np.mod(np.arctan2(oy, ox), TAU)
            s_c = np.select([th < 0.5 * math.pi, th < math.pi, th < 1.5 * math.pi],
                            [1.5 * q + 2 * a + 2 * b + r * th, 2.5 * q + 4 * a + 2 * b + r * (th - 0.5 * math.pi),
                             np.mod(self.length - 0.5 * q + r * (th - math.pi), self.length)],
                            0.5 * q + 2 * a + r * (th - 1.5 * math.pi))
            s = np.where(corner, s_c, s)
        return s.astype(F32)

    def distance(self, xs: Array, ys: Array) -> Array:
        """Signed distance to the outline (negative inside)."""
        return sd_rrect(xs, ys, self.cx, self.cy, self.hw, self.hh, self.r)


def _over(light: Light, where: tuple[slice, slice], color: Array, alpha: Array) -> None:
    """``Light.over`` into one window of the frame."""
    a = np.clip(alpha, 0, 1)
    rgb, have = light.rgb[where], light.a[where]
    rgb[...] = color * a[..., None] + rgb * (1 - a)[..., None]
    have[...] = a + have * (1 - a)


def _shine(light: Light, where: tuple[slice, slice], rgb: Array, alpha: Array) -> None:
    """Add light already multiplied by its colour (``rgb``), covering ``alpha``, into one window of the frame."""
    light.rgb[where] += rgb
    a = light.a[where]
    a[...] = 1 - (1 - a) * (1 - np.clip(alpha, 0, 1))


def _shine_flat(light: Light, where: tuple[slice, slice], idx: Array, rgb: Array, alpha: Array) -> None:
    """``_shine`` into the pixels ``idx`` of one window, flattened (a band around a shape)."""
    view_rgb, view_a = light.rgb[where], light.a[where]
    flat_rgb, flat_a = view_rgb.reshape(-1, 3), view_a.reshape(-1)
    flat_rgb[idx] += rgb
    flat_a[idx] = 1 - (1 - flat_a[idx]) * (1 - np.clip(alpha, 0, 1))
    view_rgb[...] = flat_rgb.reshape(view_rgb.shape)
    view_a[...] = flat_a.reshape(view_a.shape)


def _blob(light: Light, stage: Stage, x: float, y: float, radius: float, color: Array, amount: float) -> None:
    """A round glow of ``radius`` at ``x, y``, drawn in its own small window."""
    win = _window(stage, x - radius - 1, y - radius - 1, x + radius + 1, y + radius + 1)
    if win is not None and amount > 0:
        where, xs, ys = win
        _add(light, where, color, glow(xs, ys, x, y, radius) * amount)


def _frame(light: Light, mask: Array, rows: int = 1) -> bytes:
    """``Light.frame``, packed to upload small: pairs of rows averaged (``rows=2``, for
    soft light), the faintest pixels dropped and colours rounded to 6 bits."""
    if rows == 2:
        n = light.a.shape[0] // 2
        for plane in (light.rgb, light.a):
            pair = (plane[0:2 * n:2] + plane[1:2 * n:2]) * F32(0.5)
            plane[0:2 * n:2] = pair
            plane[1:2 * n:2] = pair
    px = np.frombuffer(light.frame(mask), np.uint8).reshape(-1, 4)
    px = (px & _ROUND) * (px[:, 3:] >= 3).astype(np.uint8)
    return px.tobytes()


def _pairs(plane: Array) -> Array:
    """Pairs of columns averaged: soft light changes little across two pixels, and uploads much smaller."""
    n = plane.shape[1] // 2 * 2
    pair = (plane[:, 0:n:2] + plane[:, 1:n:2]) * F32(0.5)
    plane[:, 0:n:2] = pair
    plane[:, 1:n:2] = pair
    return plane


def _fan(loop: _Loop, xs: Array, ys: Array, out: Array, lean: float) -> tuple[Array, Array]:
    """Where each pixel outside the outline came from, if light leaves the outline
    leaning ``lean`` (0 to 1) of the way from straight out to straight away from
    the centre: streamers that follow it fan out like a corona's."""
    px, py = np.broadcast_arrays(xs - loop.cx, ys - loop.cy)
    nx, ny = px - np.clip(px, -loop.a, loop.a), py - np.clip(py, -loop.b, loop.b)
    nl = np.maximum(np.sqrt(nx * nx + ny * ny), 1e-3)
    rl = np.maximum(np.sqrt(px * px + py * py), 1e-3)
    dx, dy = nx / nl * (1 - lean) + px / rl * lean, ny / nl * (1 - lean) + py / rl * lean
    dl = np.maximum(np.sqrt(dx * dx + dy * dy), 1e-3)
    return xs - dx / dl * out, ys - dy / dl * out


def _sparkle(xs: Array, ys: Array, cx: float, cy: float, reach: tuple[float, float, float, float],
             width: float) -> Array:
    """A four-point star's spikes, thin and tapering to exactly 0 at their ``reach``
    (left, right, up, down: they can differ, to fit the stage)."""
    dx, dy = xs - cx, ys - cy
    left, right, up, down = (max(1e-3, r) for r in reach)
    along_x = np.where(dx < 0, -dx / left, dx / right)
    along_y = np.where(dy < 0, -dy / up, dy / down)
    h = np.exp(-((dy / width) ** 2)) * (1 - np.minimum(along_x, 1)) ** 3
    v = np.exp(-((dx / width) ** 2)) * (1 - np.minimum(along_y, 1)) ** 3
    return h + v


def _star(light: Light, stage: Stage, x: float, y: float, size: float, spikes: float, core: Array, halo: Array,
          amount: float, width: float = 1.0) -> None:
    """A star of radius ``size`` (pixels): a hot core, a halo and four spikes
    ``spikes`` times its size (longer up and down), each cut short to end inside the stage."""
    if amount <= 0.004 or size <= 0:
        return
    on_top, on_right, on_bottom, on_left = stage.window_edges
    edge = 0.5 * min(stage.cell) + 1
    room = (x - (0 if on_left else edge), stage.width - x - (0 if on_right else edge), y - (0 if on_top else edge),
            stage.height - y - (0 if on_bottom else edge))
    long = size * spikes
    left, right, up, down = (max(1.0, min(w, r)) for w, r in zip((long, long, 1.25 * long, 1.25 * long), room,
                                                                    strict=True))
    win = _window(stage, x - left - 1, y - up - 1, x + right + 1, y + down + 1)
    if win is None:
        return
    where, xs, ys = win
    w = max(0.45, 0.05 * size) * width
    core_r = min(size, left, right, up, down)
    outer = _sparkle(xs, ys, x, y, (left, right, up, down), w * 2.4)
    inner = _sparkle(xs, ys, x, y, (0.8 * left, 0.8 * right, 0.8 * up, 0.8 * down), w)
    _add(light, where, halo, (glow(xs, ys, x, y, core_r) * 0.55 + outer * 0.3) * amount)
    _add(light, where, core, (glow(xs, ys, x, y, core_r * 0.32) * 1.6 + inner) * amount)


def _ramp(t: float, start: float, end: float) -> float:
    """0 before ``start`` and 1 after ``end``, smooth in between: ``smoothstep`` for one number."""
    return ease_in_out((t - start) / (end - start))


def _hot(stage: Stage, color: Array) -> Array:
    """What a core is drawn in: white (it burns) on a dark theme, the colour itself, deepened, on a light one."""
    return WHITE if stage.dark else color * 0.75


def _times(frames: int, total_ms: int) -> tuple[list[float], list[int]]:
    """The time (in seconds) of each frame, the last one at the very end, and the gaps."""
    return [f / (frames - 1) * total_ms / 1000 for f in range(frames)], gaps(frames, total_ms)


def _timeline(*spans: tuple[float, int]) -> tuple[list[float], list[int]]:
    """Frame times (in seconds) and gaps (ms) for spans that each run until a time
    at a frame gap of their own; the last frame is at the end of the last span."""
    times: list[float] = []
    out: list[int] = []
    t = 0.0
    for until, gap in spans:
        while t < until - 1e-6:
            times.append(t)
            out.append(gap)
            t += gap / 1000
    times[-1] = spans[-1][0]
    return times, out


# ================================================================ eclipse ==


def eclipse(stage: Stage) -> tuple[list[bytes], list[int]]:
    """A dark moon slides over the anchor and dims it. The last of the rim it covers
    flashes as a diamond ring, the corona flares around the outline with
    streamers, then the moon slides off and the corona fades. A small anchor
    gets a round eclipse, and the whole window one in the middle of its sky."""
    cw, ch = stage.cell
    u = unit(stage)
    dark = stage.dark
    color = col(stage.color)
    hot = _hot(stage, color)
    near = mix(stage.accent, stage.color, 0.45) if dark else color
    cx, cy, hw, hh, rad = _outline(stage)
    zone = ch  # how big the corona is drawn
    if all(stage.window_edges) and hw > 0.4 * stage.width and hh > 0.4 * stage.height:
        r = max(1.2 * ch, 0.2 * min(stage.width, stage.height))
        cx, cy, hw, hh, rad = stage.width / 2, stage.height / 2, r, r, r
        zone = max(ch, r / 2.4)
    elif min(hw, hh) < 0.8 * ch:
        hw, hh = max(hw, 0.8 * ch), max(hh, 0.8 * ch)
        rad = min(hw, hh)
    loop = _Loop(cx, cy, hw, hh, rad)
    small = min(1.0, math.sqrt(loop.length / (25 * ch)))  # 1, or less round a small outline
    reach_max = 3.4 * zone
    win = _window(stage, cx - hw - reach_max, cy - hh - reach_max, cx + hw + reach_max, cy + hh + reach_max)
    if win is None:
        return [blank(stage)] * 2, [16, 16]
    where, xs, ys = win
    fade = border(stage, 1.1 * ch)[where]
    d = loop.distance(xs, ys)
    out = np.maximum(d, 0)
    s = loop.param(*_fan(loop, xs, ys, out, 0.55))
    outside = smoothstep(-0.25 * ch, 0.25 * ch, d) * fade
    R = rng(stage)

    # the corona: a glow hugging the rim, and streamers that taper outwards, one at each corner
    streams = np.zeros_like(d)
    corners = loop.corners()
    for k in range(6 + int(loop.length / (6 * zone))):
        at = corners[k] + R.uniform(-0.4, 0.4) * loop.q if k < 4 else R.uniform(0, loop.length)
        w, reach = R.uniform(0.35, 0.9) * zone * small, R.uniform(1.2, 3.0) * zone
        left = np.clip(1 - out / reach, 0, 1)
        gap = np.abs(s - at)
        gap = np.minimum(gap, loop.length - gap) / (w * left ** 0.8 + 0.3)
        streams += R.uniform(0.6, 1.4) * np.exp(-gap * gap) * left ** 1.4
    rays = (0.75 + 0.25 * np.sin(s * (TAU / (0.9 * zone * small)) + 2.5 * np.sin(s * (TAU / (4.7 * zone)))))
    rays = rays * outside
    inner = np.exp(-out / (0.45 * zone)) * 0.6 * rays
    streams *= 1.2 * rays
    tint = _pairs(smoothstep(0.0, 0.6 * zone, out))[..., None]
    corona_rgb = near * (1 - tint) + color * tint
    # the rim: a thin hot line in a narrow halo
    rim = np.exp(-((d / (0.6 * u + 0.4)) ** 2)) * fade
    rim_halo = np.exp(-np.abs(d) / (0.12 * ch)) * fade
    rim_rgb = near * (rim_halo * 0.5)[..., None] + hot * (rim * (0.75 if dark else 0.6))[..., None]
    rim_a = np.clip(rim_halo * 0.5 + rim * 0.75, 0, 1)
    lit_rgb = near * (rim * 0.5 + rim_halo * 0.22)[..., None] + hot * (rim * 0.5)[..., None]
    lit_a = np.clip(rim + rim_halo * 0.22, 0, 1)
    # a few prominences: small hot knots on the rim
    proms = []
    for _ in range(int(R.integers(2, 5))):
        px, py, nx, ny = loop.point(R.uniform(0, loop.length))
        size = R.uniform(0.22, 0.38) * ch * small
        proms.append((px + nx * size * 0.7, py + ny * size * 0.7, size, R.uniform(0, TAU)))

    # the moon: a disc just big enough to cover the outline, passing left to right
    moon_r = math.hypot(loop.a, loop.b) + loop.r + 0.5 * cw + 0.45 * ch
    travel = moon_r + hw + 0.5 * ch
    panel = smoothstep(0.5 * cw, -0.2 * cw, d) * (0.58 if dark else 0.22)
    # the diamond: on the top-right corner, the last of the rim the moon covers
    gx, gy, nx, ny = loop.point(corners[1])
    gx, gy = gx + nx * 0.5 * u, gy + ny * 0.5 * u
    top, right, _, _ = stage.window_edges
    flare = zone * (0.5 + 0.5 * small)
    bloom = max(1.0, min(1.8 * flare, stage.width - gx - (0 if right else 0.6 * ch), gy - (0 if top else 0.6 * ch)))
    mask = border(stage)

    times, gap_ms = _timeline((0.5, 45), (0.76, 38), (1.1, 100), (1.8, 52))
    frames = []
    for t in times:
        light = Light(stage)
        if t < 0.56:
            mx = cx - travel * (1 - t / 0.56) ** 2  # slowing as it covers
        else:
            mx = cx + travel * ease_in_out((t - 1.08) / 0.5) ** 1.6
        moon = smoothstep(moon_r + 0.45 * ch, moon_r - 0.45 * ch, np.sqrt((xs - mx) ** 2 + (ys - cy) ** 2))
        _over(light, where, BLACK, panel * moon)
        # before totality, the part of the rim the moon has not reached yet still shines
        lit = _ramp(t, 0.0, 0.12) * (1 - _ramp(t, 0.5, 0.6))
        if lit > 0:
            _shine(light, where, lit_rgb * (lit * (1 - moon))[..., None], lit_a * lit * (1 - moon))
        corona = _ramp(t, 0.45, 0.56) * (1 + 0.45 * bump(t - 0.5, 0.06, 0.45)) * (1 - _ramp(t, 1.1, 1.72))
        if corona > 0.002:
            front = (0.8 + 2.4 * ease_out((t - 0.47) / 0.7)) * zone
            flow = (0.85 + 0.15 * np.sin(out * (TAU / (0.9 * zone)) - t * 9)) * smoothstep(front + 0.8 * zone,
                                                                                          front - 0.8 * zone, out)
            amount = _pairs((inner + streams * flow) * corona)
            _shine(light, where, corona_rgb * amount[..., None] + rim_rgb * corona,
                   1 - (1 - np.minimum(amount, 1)) * (1 - np.minimum(rim_a * corona, 1)))
            for px, py, size, ph in proms:
                _blob(light, stage, px, py, size, color, 1.1 * corona * (0.8 + 0.2 * math.sin(t * 11 + ph)))
        flash = bump(t - 0.4, 0.09, 0.5)
        if flash > 0:
            _blob(light, stage, gx, gy, bloom, near, 0.7 * flash)
            _star(light, stage, gx, gy, 1.1 * flare * (0.5 + 0.5 * flash), 4.0 * (0.6 + 0.4 * flash), hot,
                  near, 2.0 * flash, 1.5)
        frames.append(_frame(light, mask, rows=2))
    return frames, gap_ms


# ================================================================= aurora ==


def aurora(stage: Stage) -> tuple[list[bytes], list[int]]:
    """Curtains of light rise above the anchor's top edge and wave: ``color`` at
    their bright lower edge into ``accent`` at the top, with fine vertical rays
    and folds that drift along them. With no room above the anchor, they hang
    inside its top (over the whole window, across the top of the sky)."""
    cw, ch = stage.cell
    dark = stage.dark
    color, accent = col(stage.color), col(stage.accent)
    ax, ay, aw, ah = stage.at
    edge = ay + 0.5 * ch  # the panel's top border
    lim = 0.0 if stage.window_edges[0] else 0.5 * ch
    space = edge - 0.05 * ch - lim
    x0, _, x1, _ = _room(stage, ax - 1.5 * cw, 0, ax + aw + 1.5 * cw, 0, 0.6 * ch)
    span = max(1.0, x1 - x0)
    if space >= 1.8 * ch:
        base0, height, wave = edge - 0.05 * ch, min(space, 4.6 * ch), 0.18 * ch
    else:
        base0 = min(lim + max(2.4 * ch, 0.26 * stage.height), ay + max(0.5 * ch, 0.7 * ah))
        height = base0 - lim
        wave = max(0.18 * ch, 0.1 * height)
        base0 -= wave
    # drawn in blocks of 2 by 2 pixels (2 by 1 on a stage drawn small): it is soft, and uploads much smaller
    bw = 2 if stage.scale > 0.75 else 1
    win = _window(stage, bw * int(x0 / bw), 2 * int((base0 - height - wave - 2) / 2), x1, base0 + wave + 0.8 * ch)
    if win is None:
        return [blank(stage)] * 2, [16, 16]
    (rows, cols), xs, ys = win
    xs, ys = xs[:, 0::bw] + F32(0.5 * (bw - 1)), ys[0::2] + F32(0.5)
    ends = smoothstep(x0, x0 + min(5 * cw, span / 3), xs) * smoothstep(x1, x1 - min(5 * cw, span / 3), xs)
    R = rng(stage, 1)
    # two curtains, a bright one in front of a fainter one; each folds, and seen edge on a fold
    # is a bright band, so two waves of folds drift past each other along it
    curtains = []
    for depth in range(2):
        folds = [(TAU / (R.uniform(12, 22) * cw), R.uniform(0.32, 0.42), R.uniform(0.2, 0.45) * R.choice([-1, 1]),
                  R.uniform(0, TAU)) for _ in range(2)]
        curtains.append((depth, R.uniform(0, TAU, 6), folds))
    lap, swell = TAU / max(10 * cw, span / 2.5), R.uniform(0, TAU)  # the lower edge's wave
    strength = 1.0 if dark else 0.8  # paint on a light theme: a little goes a long way
    mask = border(stage)

    times, gap_ms = _times(32 if bw == 2 else 27, 2000)  # it moves slowly: fewer frames on a big stage
    frames = []
    for t in times:
        light = Light(stage)
        gain = _ramp(t, 0.0, 0.6) * (1 - _ramp(t, 1.3, 2.0))
        if gain > 0.002:
            rgb = np.zeros((ys.shape[0], xs.shape[1], 3), F32)
            alpha = np.zeros((ys.shape[0], xs.shape[1]), F32)
            for depth, ph, folds in curtains:
                tall = height * (0.45 + 0.55 * ease_out(t / 1.1)) * (1.0 if depth == 0 else 0.9)
                # where along the curtain each column is, and how bunched up the curtain is there
                u = xs.copy()
                bunch = np.zeros_like(xs)
                for k, amp, speed, p0 in folds:
                    arg = k * xs - speed * t * TAU + p0
                    u = u - amp / k * np.sin(arg)
                    bunch = bunch + amp * np.cos(arg)
                bright = np.minimum(0.6 / np.maximum(1 - bunch, 0.2), 3.0 if dark else 1.5)
                base = base0 - (0.2 + 0.3 * depth) * ch - wave * np.sin(u * lap - t * 1.8 + swell + 0.5 * depth)
                hgt = np.maximum(base - ys, -tall)  # above the curtain's lower edge
                rel = hgt / max(1.0, tall)
                X = u + cw * 0.7 * np.clip(rel, 0, 1) * np.sin(u * (TAU / (9 * cw)) - t * 2.2 + ph[1])
                rays = ((0.5 + 0.5 * np.cos(X * (TAU / (0.7 * cw)) + ph[2]))
                        * (0.5 + 0.5 * np.cos(X * (TAU / (1.45 * cw)) + ph[3])) * 0.6
                        + 0.45 + 0.2 * np.cos(X * (TAU / (3.3 * cw)) + ph[4]))
                reach = 0.7 + 0.3 * np.cos(X * (TAU / (2.1 * cw)) + ph[5])  # the rays end at different heights
                if depth == 0:
                    lower = smoothstep(-0.07 * ch, 0.05 * ch, hgt)
                else:
                    lower = smoothstep(-0.3 * ch, 0.2 * ch, hgt)
                up = np.clip(rel / reach, 0, 1)
                upper = (1 - up) ** 1.3 * (0.55 + 0.45 * np.exp(-up * 6))
                spill = np.exp(np.minimum(hgt, 0) / (0.3 * ch)) * (hgt < 0) * 0.3
                k_ = (lower * upper * rays + spill) * bright * ends * gain * ((0.95 if depth == 0 else 0.45) * strength)
                tint = smoothstep(0.1, 0.75, rel)[..., None]
                c = color * (1 - tint) + accent * tint
                if dark and depth == 0:  # the lower edge burns
                    c = c + 0.45 * (1 - tint) * np.exp(-np.abs(hgt) / (0.1 * ch))[..., None]
                rgb += c * k_[..., None]
                alpha = 1 - (1 - alpha) * (1 - np.minimum(k_, 1))
            n, m = rows.stop - rows.start, cols.stop - cols.start
            _shine(light, (rows, cols), np.repeat(np.repeat(rgb, 2, 0), bw, 1)[:n, :m],
                   np.repeat(np.repeat(alpha, 2, 0), bw, 1)[:n, :m])
        frames.append(_frame(light, mask))
    return frames, gap_ms


# ================================================================== orbit ==


def _ease_inverse(y: Array) -> Array:
    """The inverse of ``ease_in_out`` (smoothstep): when it reaches ``y``."""
    return 0.5 - np.sin(np.arcsin(np.clip(1 - 2 * y, -1, 1)) / 3)


def orbit(stage: Stage) -> tuple[list[bytes], list[int]]:
    """A small satellite makes one lap just outside the anchor's border, clockwise
    from the top left, lighting the border behind it, and ends in a glint."""
    cw, ch = stage.cell
    u = unit(stage)
    color, accent = col(stage.color), col(stage.accent)
    hot = _hot(stage, color)
    cx, cy, hw, hh, rad = _outline(stage)
    off = 0.45 * ch
    x0, y0, x1, y1 = _room(stage, cx - hw - off, cy - hh - off, cx + hw + off, cy + hh + off, 0.75 * ch, 0.35 * ch)
    path = _Loop((x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2, rad + off)
    edge = _Loop(cx, cy, hw, hh, rad)
    reach = 1.1 * ch
    win = _window(stage, x0 - reach, y0 - reach, x1 + reach, y1 + reach)
    if win is None:
        return [blank(stage)] * 2, [16, 16]
    where, xs, ys = win
    # only the pixels near the path are drawn on, as a flat list
    dp = np.abs(path.distance(xs, ys))
    near = np.flatnonzero(dp < reach)
    xs, ys = np.broadcast_to(xs, dp.shape).ravel()[near], np.broadcast_to(ys, dp.shape).ravel()[near]
    dp = dp.ravel()[near]
    fade = border(stage, 0.8 * ch)[where].ravel()[near]
    de = np.abs(edge.distance(xs, ys))
    lap = 0.86
    passed = lap * _ease_inverse(path.param(xs, ys) / path.length)  # when the satellite passes each pixel
    # the border's light: a thin line in a soft glow
    line = (np.exp(-((de / (0.9 * u + 0.3)) ** 2)) * 0.55 + np.exp(-((de / (0.28 * ch)) ** 2)) * 0.2) * fade
    gx, gy, _, _ = path.point(0.0)
    mask = border(stage)

    times, gap_ms = _times(36, 1200)
    frames = []
    for t in times:
        light = Light(stage)
        env = 1 - _ramp(t, 0.98, 1.2)
        dt = t - passed
        behind = dt >= 0
        dt = np.maximum(dt, 0)
        # the trail along the path, narrowing as it fades, from the colour to the accent
        tail = np.exp(-dt / 0.08) * behind
        w = (0.1 * ch + u) * (0.3 + 0.7 * tail)
        trail = (tail * np.exp(-((dp / w) ** 2)) * 1.2
                 + np.exp(-dt / 0.14) * behind * np.exp(-((dp / (0.45 * ch)) ** 2)) * 0.25) * fade * env
        tint = np.clip(dt / 0.2, 0, 1)[..., None]
        # the border lights up where it has passed, and all of it once at the finish
        done = bump(t - lap + 0.04, 0.08, 0.34)
        after = ((1 - np.exp(-dt / 0.03)) * np.exp(-dt / 0.35) * behind + 0.7 * done) * line * env
        rgb = (color * (1 - tint) + accent * tint) * trail[..., None] + color * after[..., None]
        _shine_flat(light, where, near, rgb, 1 - (1 - np.minimum(trail, 1)) * (1 - np.minimum(after, 1)))
        # the satellite: a hot core with short spikes, in a glow
        if t <= lap + 0.05:
            hx, hy, _, _ = path.point(ease_in_out(t / lap) * path.length)
            grow = _ramp(t, 0.0, 0.08) * (1 - _ramp(t, lap - 0.03, lap + 0.05))
            _blob(light, stage, hx, hy, 1.1 * ch, color, 0.6 * grow)
            _star(light, stage, hx, hy, 0.42 * ch, 1.7, hot, color, 1.5 * grow)
        # the glint where it finishes
        if done > 0:
            _blob(light, stage, gx, gy, 1.5 * ch, color, 0.6 * done)
            _star(light, stage, gx, gy, 0.9 * ch * (0.6 + 0.4 * done), 4.0, hot, mix(stage.color, stage.accent, 0.3),
                  2.2 * done, 1.3)
        frames.append(_frame(light, mask))
    return frames, gap_ms


# ========================================================== constellation ==


def constellation(stage: Stage) -> tuple[list[bytes], list[int]]:
    """Stars twinkle in around the anchor, at its corners and along its border;
    thin lines join them one after another, and the closed figure shimmers and
    fades. Over the whole window, the figure is drawn big in the middle of its sky."""
    cw, ch = stage.cell
    u = unit(stage)
    dark = stage.dark
    color, accent = col(stage.color), col(stage.accent)
    hot = _hot(stage, color)
    halo = accent if dark else accent * 0.85
    cx, cy, hw, hh, rad = _outline(stage)
    R = rng(stage, 2)
    zone, scatter = ch, 1.0  # how big the stars are, and how far from the outline they may be
    if all(stage.window_edges) and hw > 0.4 * stage.width and hh > 0.4 * stage.height:
        cx, cy, hw, hh = stage.width / 2, stage.height / 2, 0.3 * stage.width, 0.28 * stage.height
        rad, zone, scatter = 0.4 * min(hw, hh), max(ch, min(stage.width, stage.height) / 20), 2.5
    loop = _Loop(cx, cy, max(hw, 2 * cw), max(hh, 0.6 * ch), rad)
    count = int(np.clip(5 + int(loop.length / (14 * ch)) + R.integers(-1, 1), 5, 8))
    # a star near each corner, the rest along the sides (more on the longer ones), apart from each other
    spots = [(c + R.uniform(-0.5, 0.5) * loop.q, R.uniform(0.15, 0.8) * scatter) for c in loop.corners()]
    sides = [(0.5 * loop.q, 2 * loop.a), (1.5 * loop.q + 2 * loop.a, 2 * loop.b),
             (2.5 * loop.q + 2 * loop.a + 2 * loop.b, 2 * loop.a), (3.5 * loop.q + 4 * loop.a + 2 * loop.b, 2 * loop.b)]
    weights = np.array([max(1e-3, length) for _, length in sides])
    for _ in range(80):
        if len(spots) >= count:
            break
        start, length = sides[int(R.choice(4, p=weights / weights.sum()))]
        at = start + length * R.uniform(0.12, 0.88)
        if all(min(abs(at - o), loop.length - abs(at - o)) > 5 * cw for o, _ in spots):
            spots.append((at, R.uniform(-0.25, 1.0) * scatter))
    spots.sort()
    x0, y0, x1, y1 = _room(stage, 0, 0, stage.width, stage.height, 1.0 * ch, 0.6 * ch)
    stars = []
    for at, lift in spots:
        x, y, nx, ny = loop.point(at)
        x, y = min(max(x + nx * lift * ch, x0), x1), min(max(y + ny * lift * ch, y0), y1)
        stars.append((x, y, R.uniform(0.42, 0.85) * zone))
    count = len(stars)
    born = {int(k): 0.03 + 0.4 * i / max(1, count - 1) for i, k in enumerate(R.permutation(count))}
    phase = R.uniform(0, TAU, count)

    # the lines, each in its own window, stopping short of the stars
    lines = []
    for k in range(count):
        ax, ay, _ = stars[k]
        bx, by, _ = stars[(k + 1) % count]
        length = math.hypot(bx - ax, by - ay)
        if length < 1:
            continue
        cut = min(0.4 * ch, length / 3) / length
        sx, sy = ax + (bx - ax) * cut, ay + (by - ay) * cut
        ex, ey = bx - (bx - ax) * cut, by - (by - ay) * cut
        pad = 3 * u + 0.25 * ch
        win = _window(stage, min(sx, ex) - pad, min(sy, ey) - pad, max(sx, ex) + pad, max(sy, ey) + pad)
        if win is None:
            continue
        where, wx, wy = win
        dist, along = segment(wx, wy, sx, sy, ex, ey)
        shape = np.exp(-((dist / (0.7 * u + 0.25)) ** 2)) * 0.85 + np.exp(-((dist / (2.4 * u + 0.5)) ** 2)) * 0.22
        lines.append((where, along, shape, length * (1 - 2 * cut), (sx, sy, ex, ey), k))
    draw0, draw1 = 0.5, 1.12
    total = sum(item[3] for item in lines) or 1.0
    starts = np.cumsum([0.0] + [item[3] for item in lines]) / total
    reached = {}  # when the pen reaches each star
    for i, (_, _, _, _, _, k) in enumerate(lines):
        reached[(k + 1) % count] = draw0 + (draw1 - draw0) * _ease_inverse(np.array(starts[i + 1])).item()
    mask = border(stage)

    times, gap_ms = _times(45, 1800)
    frames = []
    for t in times:
        light = Light(stage)
        env = 1 - _ramp(t, 1.42, 1.8)
        pen = ease_in_out((t - draw0) / (draw1 - draw0))
        done = bump(t - draw1 + 0.02, 0.06, 0.42)
        for i, (where, along, shape, _, (sx, sy, ex, ey), _) in enumerate(lines):
            p = (pen - starts[i]) / max(1e-6, starts[i + 1] - starts[i])
            if p <= 0:
                continue
            if p < 1:
                lit = smoothstep(p + 0.03, p - 0.03, along)
                fresh = np.exp(-np.maximum(p - along, 0) * 5) * lit
                _add(light, where, accent, shape * (0.7 * lit + 0.7 * fresh) * env)
                _blob(light, stage, sx + (ex - sx) * p, sy + (ey - sy) * p, 0.3 * ch, hot, 1.2 * env)
            else:
                # once it is closed, a sheen runs once round the figure
                at = starts[i] + along * (starts[i + 1] - starts[i]) - _ramp(t, draw1, draw1 + 0.36) * 1.2 + 0.1
                sheen = np.exp(-((at / 0.07) ** 2)) * (t > draw1)
                shimmer = 0.85 + 0.15 * math.sin(t * 8 + 1.7 * i)
                _add(light, where, accent, shape * (0.7 * shimmer + 0.5 * done + 1.1 * sheen) * env)
        for k, (x, y, size) in enumerate(stars):
            age = t - born[k]
            if age <= 0:
                continue
            flash = bump(age, 0.06, 0.34)
            ping = bump(t - reached.get(k, 9.0) + 0.02, 0.04, 0.3)
            steady = _ramp(age, 0.0, 0.1) * (0.6 + 0.15 * math.sin(t * 7 + phase[k]))
            amount = (steady + 0.9 * flash + 0.5 * ping + 0.7 * done) * env
            spikes = 1.4 + 1.8 * flash + 0.8 * ping + 1.3 * done
            _star(light, stage, x, y, size * (0.75 + 0.25 * max(flash, done)), spikes, hot, halo, amount)
        frames.append(_frame(light, mask))
    return frames, gap_ms


KINDS = {
    "eclipse": Flourish(eclipse, grow=(5, 3), color="error", accent="warning",
                        about="something went dark: a failure, a source down"),
    "aurora": Flourish(aurora, grow=(3, 4), color="success", accent="secondary",
                       about="all clear"),
    "orbit": Flourish(orbit, grow=(3, 2), color="primary", accent="secondary",
                      about="refreshed, synced, one more lap done"),
    "constellation": Flourish(constellation, grow=(3, 2), color="foreground", accent="primary",
                              about="a set is complete: every check green, a goal reached"),
}
