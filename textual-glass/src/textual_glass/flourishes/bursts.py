"""Bursts: flourishes that explode out of (or fall into) a point. No Textual imports.

* ``sunburst``: long and short rays shoot out of the anchor's centre.
* ``supernova``: a flash, a ragged shockwave, debris and a fading nebula.
* ``pulsar``: a lighthouse star whose rings wrap the anchor's outline.
* ``black-hole``: an accretion disc swirls into a dark core, then collapses.

Each one is radial about the anchor's centre, so a maker works out each
pixel's distance and angle from it once and reuses them in every frame. It
draws into windows of the frame, never more of it than the light can reach:
particles into small boxes round them. On a light theme the light is painted
a little deeper and never quite opaque (:data:`PAINT`).
"""

from __future__ import annotations

import math

import numpy as np

from ..render import F32, Array, smoothstep
from . import MAX_PX, Flourish, Stage
from ._draw import (
    Light,
    blank,
    bump,
    centre,
    coords,
    ease_out,
    ramp,
    rng,
    segment,
    shade,
    timeline,
    unit,
    white_hot,
    window,
)

TAU = 2 * math.pi
#: On a light theme, how opaque the brightest light is painted.
PAINT = 0.85


# =============================================================== helpers ==

NOWHERE = (slice(0, 0), slice(0, 0))


class _Polar:
    """Every pixel's offset, distance and angle from a centre, worked out once."""

    def __init__(self, stage: Stage, cx: float, cy: float) -> None:
        xs, ys = coords(stage)
        self.stage = stage
        self.cx, self.cy = cx, cy
        self.dx = np.ascontiguousarray(np.broadcast_to(xs - cx, (stage.height, stage.width)))
        self.dy = np.ascontiguousarray(np.broadcast_to(ys - cy, (stage.height, stage.width)))
        self.d = np.sqrt(self.dx * self.dx + self.dy * self.dy)
        self.phi = np.arctan2(self.dy, self.dx)

    def box(self, rx: float, ry: float | None = None) -> tuple[slice, slice]:
        """The window that holds everything within (rx, ry) of the centre, and a pixel more on the right and
        below."""
        ry = rx if ry is None else ry
        win = window(self.stage, self.cx - rx, self.cy - ry, self.cx + rx + 1, self.cy + ry + 1)
        return NOWHERE if win is None else win[0]


def _reach(stage: Stage, cx: float, cy: float) -> tuple[float, float]:
    """How far light may reach from (cx, cy), across and down: to the stage's
    nearer faded side, less the fade. Light may run off a side on the window's
    edge, so those sides don't count unless both do."""
    top, right, bottom, left = stage.window_edges
    margin = max(2.0, 0.5 * min(stage.cell)) + 1.0

    def side(lo: float, hi: float, lo_free: bool, hi_free: bool) -> float:
        lo, hi = lo - margin, hi - margin
        if lo_free and hi_free:
            return max(lo, hi)
        if lo_free:
            return hi
        if hi_free:
            return lo
        return min(lo, hi)

    return (max(1.0, side(cx, stage.width - cx, left, right)),
            max(1.0, side(cy, stage.height - cy, top, bottom)))


def _ellipse(rx: float, ry: float, angle: Array | float) -> Array:
    """The radius of an ellipse along ``angle``."""
    c, s = np.cos(angle), np.sin(angle)
    return np.asarray(rx * ry / np.sqrt((ry * c) ** 2 + (rx * s) ** 2 + 1e-9), F32)


class _Ink:
    """A burst's colours: a little deeper on a light theme, where soft haze is
    fainter too (it reads as light on a dark theme, and as a stain on a light one)."""

    def __init__(self, stage: Stage) -> None:
        self.color, self.accent = shade(stage, stage.color, 0.9), shade(stage, stage.accent, 0.9)
        self.hot = white_hot(stage, stage.color, 0.72)
        self.haze = 1.0 if stage.dark else 0.45


def _spark(light: Light, x: float, y: float, size: float, colour: Array, amount: float, spikes: float = 0.0) -> None:
    """A small star: a soft dot, with four thin spikes when ``spikes`` > 0."""
    reach = size * (3.0 + 5.0 * spikes)
    win = window(light.stage, x - reach, y - reach, x + reach, y + reach)
    if win is None or amount <= 0:
        return
    where, xs, ys = win
    ax, ay = np.abs(xs - x), np.abs(ys - y)
    d2 = ax * ax + ay * ay
    shape = np.exp(-d2 / (size * size))
    if spikes > 0:
        thin = 0.35 * size + 0.3
        long = reach * 0.45
        shape = shape + spikes * (np.exp(-ay / thin - ax / long) + np.exp(-ax / thin - ay / long))
    light.shine(colour, shape * smoothstep(reach, reach * 0.6, np.sqrt(d2)) * amount, where)


def _dash(light: Light, x0: float, y0: float, x1: float, y1: float, width: float, colour: Array,
          amount: float) -> None:
    """A soft streak from a faint tail (x0, y0) to a bright head (x1, y1)."""
    pad = width * 2.5
    win = window(light.stage, min(x0, x1) - pad, min(y0, y1) - pad, max(x0, x1) + pad, max(y0, y1) + pad)
    if win is None or amount <= 0:
        return
    where, xs, ys = win
    dist, t = segment(xs, ys, x0, y0, x1, y1)
    light.shine(colour, np.exp(-((dist / width) ** 2)) * t * t * smoothstep(pad, pad * 0.6, dist) * amount, where)


def _trail(light: Light, points: list[tuple[float, float]], levels: list[float], width: float,
           colour: Array) -> None:
    """A soft line through ``points``, as bright as ``levels`` along it: a
    curved trail, drawn once (pieces drawn one by one would double up where
    they meet)."""
    pad = width * 2.5
    xs_ = [x for x, _ in points]
    ys_ = [y for _, y in points]
    win = window(light.stage, min(xs_) - pad, min(ys_) - pad, max(xs_) + pad, max(ys_) + pad)
    if win is None or max(levels) <= 0:
        return
    where, xs, ys = win
    near = np.full(np.broadcast_shapes(xs.shape, ys.shape), np.inf, F32)
    level = np.zeros_like(near)
    for (x0, y0), (x1, y1), l0, l1 in zip(points, points[1:], levels, levels[1:], strict=False):
        dist, t = segment(xs, ys, x0, y0, x1, y1)
        closer = dist < near
        near = np.where(closer, dist, near)
        level = np.where(closer, l0 + (l1 - l0) * t, level)
    light.shine(colour, np.exp(-((near / width) ** 2)) * level * smoothstep(pad, pad * 0.6, near), where)


def _shell(d: Array, radius: float, width: float) -> Array:
    """A thin soft ring at ``radius`` (``d`` is each pixel's distance from its centre)."""
    return np.exp(-(((d - radius) / width) ** 2))


def _wobble(r: np.random.Generator, phi: Array, amount: float, low: int = 3, terms: int = 3) -> Array:
    """A smooth, random wobble around a circle (``phi`` is the angle), about +-``amount``."""
    out = np.zeros_like(phi)
    for k in range(terms):
        m = int(r.integers(low + 2 * k, low + 3 + 3 * k))
        out = out + np.cos(m * phi + r.uniform(0, TAU)) / (k + 1)
    return out * (amount / 1.8)


# ============================================================== sunburst ==


def sunburst(stage: Stage) -> tuple[list[bytes], list[int]]:
    """A warm flash at the anchor's centre, then long and short rays shoot out
    past its edges, turning a little, over a swelling halo; they fade from the
    base outwards and leave a few glints at their tips. About a second."""
    cx, cy = centre(stage.at)
    ink = _Ink(stage)
    u = unit(stage)
    cw, ch = stage.cell
    _, _, aw, ah = stage.at
    rx, ry = _reach(stage, cx, cy)
    rx, ry = min(rx, aw / 2 + 7 * cw), min(ry, ah / 2 + 3.6 * ch)
    rx = min(rx, 3.0 * ry)
    p = _Polar(stage, cx, cy)
    r = rng(stage)

    n = int(r.integers(7, 10)) * 2  # 14 to 18 rays, long and short in turn
    spacing = TAU / n
    start = r.uniform(0, TAU)
    angles = start + np.arange(n) * spacing + r.uniform(-0.22, 0.22, n) * spacing
    long = np.arange(n) % 2 == 0
    lengths = _ellipse(rx, ry, angles) * np.where(long, r.uniform(0.82, 0.92, n), r.uniform(0.4, 0.56, n))
    widths = ch * np.where(long, 0.13, 0.09) * r.uniform(0.75, 1.25, n)
    spin = float(r.choice([-1.0, 1.0])) * math.radians(r.uniform(5, 9))

    # each pixel belongs to the nearest long ray and the nearest short one
    sets = []
    for parity in (0, 1):
        idx = np.arange(parity, n, 2)
        k = np.round((p.phi - start - parity * spacing) / (2 * spacing)).astype(np.int64) % len(idx)
        ray = idx[k]
        rel = (p.phi - angles[ray] + math.pi) % TAU - math.pi
        sets.append((p.d * np.sin(rel), p.d * np.cos(rel), lengths[ray].astype(F32), widths[ray].astype(F32)))
    colours = (ink.color, ink.color * 0.6 + ink.accent * 0.4)
    # glints where some of the long rays end
    glints = [(i, float(r.uniform(0.32, 0.52)), float(r.uniform(0.8, 1.2))) for i in np.flatnonzero(long)
              if r.random() < 0.6]

    halo_r = min(math.sqrt(rx * ry) * 0.6, ry * 0.95)
    glow = ink.color * 0.75 + ink.hot * 0.25
    drawn, gap = 26, 38
    frames: list[bytes] = []
    for f in range(drawn):
        t = f / (drawn - 1)
        light = Light(stage, PAINT)
        # the halo swells behind the rays: bright and warm in the middle, gone before it can muddy the glass
        hr = halo_r * (0.45 + 0.55 * ease_out(t / 0.5))
        where = p.box(hr)
        q = p.d[where] / hr
        swell = bump(t, 0.1, 0.5)
        light.shine(glow, np.exp(-5.0 * q * q) * smoothstep(1.0, 0.5, q) * 0.85 * swell * ink.haze, where)

        grow = 0.92 * ease_out(t / 0.38) + 0.08 * t  # out fast, then drifting on
        turn = spin * ease_out(t)
        cs, sn = math.cos(turn), math.sin(turn)
        let_go = 0.95 * ramp(0.3, 1.0, t)  # the base lets go first, the tips last
        level = 1.0 - ramp(0.75, 1.0, t)
        for (perp0, along0, length, width), colour in zip(sets, colours, strict=True):
            perp = perp0 * cs - along0 * sn
            along = along0 * cs + perp0 * sn
            s = along / (length * (0.06 + 0.94 * grow))
            sc = np.clip(s, 0, 1)
            w = width * (1.0 - 0.6 * sc) + 0.35 * u
            shape = np.exp(-((perp / w) ** 2)) * smoothstep(1.0, 0.78, s) * smoothstep(-0.02, 0.05, s)
            shape = shape * smoothstep(let_go - 0.1, let_go + 0.06, s)
            light.shine(colour, shape * (1.7 - 0.9 * sc) * level)

        for i, at, size in glints:
            g = bump(t - at, 0.06, 0.42)
            if g > 0:
                a = angles[i] + turn
                tip = float(lengths[i]) * grow * 0.96
                _spark(light, cx + tip * math.cos(a), cy + tip * math.sin(a), 0.11 * ch * size,
                       ink.hot * 0.6 + ink.color * 0.4, 1.4 * g, spikes=0.7)

        # the core: a white-hot flash, then a warm glow that cools
        flash = bump(t, 0.04, 0.3)
        core_r = min(ch, 0.12 * math.sqrt(rx * ry)) * (0.45 + 1.0 * flash)
        where = p.box(core_r * 1.6)
        d = p.d[where]
        core = np.exp(-2.0 * (d / core_r) ** 2) * smoothstep(core_r * 1.6, core_r, d)
        light.shine(ink.hot, core * 2.6 * flash, where)
        light.shine(ink.color, core * 1.1 * bump(t, 0.08, 0.5), where)
        frames.append(light.frame())
    return [*frames, blank(stage)], [gap] * (len(frames) + 1)


# ============================================================= supernova ==


def supernova(stage: Stage) -> tuple[list[bytes], list[int]]:
    """The big one, in five beats: a star brightens and shrinks, flashes white,
    throws out a ragged shockwave and a spray of debris, and leaves faint,
    colourful filaments that the shock lit up as it passed. About two seconds."""
    cx, cy = centre(stage.at)
    ink = _Ink(stage)
    u = unit(stage)
    cw, ch = stage.cell
    _, _, aw, ah = stage.at
    rx, ry = _reach(stage, cx, cy)
    rx, ry = min(rx, aw / 2 + 13 * cw), min(ry, ah / 2 + 5.5 * ch)
    # the shockwave is round, as big as the shorter way allows (and a frame's pixels can pay for)
    big = min(min(rx, ry) / 1.06, 0.42 * math.sqrt(MAX_PX))
    line = min(ch, 0.11 * big)  # strokes follow the cell, but never get heavy for the size of the blast
    sc = min(ch, big / 9)  # the star's size
    p = _Polar(stage, cx, cy)
    r = rng(stage)
    hot = ink.hot

    def shock_radius(e: float) -> float:
        return big * (0.05 + 0.95 * (1 - (1 - e) ** 3.2))

    ragged = 1.0 + _wobble(r, p.phi, 0.018, low=5)
    knots = 0.8 + _wobble(r, p.phi, 0.45, low=3)  # brighter in places

    # debris: each piece flies out on its own line and slows down
    n_bits = 40
    bit_angle = r.uniform(0, TAU, n_bits)
    reach_x, reach_y = min(rx, 1.4 * big) * 0.97, min(ry, 1.4 * big) * 0.97
    bit_far = _ellipse(reach_x, reach_y, bit_angle) * r.uniform(0.3, 1.0, n_bits) ** 0.5
    bit_size = r.uniform(0.6, 1.4, n_bits)
    bit_tone = r.uniform(0, 1, n_bits)
    bit_life = r.uniform(0.75, 1.3, n_bits)

    # the remnant: thin filaments in both colours, lit as the shock passes them
    arcs = []
    for i in range(14):
        frac = r.uniform(0.45, 0.97)
        rad, a0, span = big * frac, r.uniform(0, TAU), r.uniform(0.3, 0.85)
        slope, wave = r.uniform(-0.2, 0.2), r.uniform(-0.04, 0.04)
        w = (0.05 + 0.04 * r.random()) * line + 0.4 * u
        ends = a0 + np.linspace(-span / 2, span / 2, 9)
        radii = np.array([rad - 4 * w, rad * (1 + abs(slope) * span) + 4 * w])
        xs_, ys_ = cx + np.outer(radii, np.cos(ends)), cy + np.outer(radii, np.sin(ends))
        x0, x1, y0, y1 = float(np.min(xs_)), float(np.max(xs_)), float(np.min(ys_)), float(np.max(ys_))
        win = window(stage, x0, y0, x1 + 1, y1 + 1)  # and a pixel more, where its faint edge still shows
        if win is None:
            continue
        where = win[0]
        rel = (p.phi[where] - a0 + math.pi) % TAU - math.pi
        off = p.d[where] - rad * (1 + slope * rel + wave * np.sin(rel * 9 + i))
        along = np.abs(rel) / (span / 2)
        field = (np.exp(-((off / w) ** 2)) + 0.25 * np.exp(-((off / (2 * w)) ** 2))) * smoothstep(1.0, 0.35, along)
        tone = (ink.color, ink.accent, ink.color * 0.5 + ink.accent * 0.5)[i % 3]
        lit = 0.3 + 0.95 * (1 - (1 - (frac - 0.05) / 0.95) ** (1 / 3.2))
        arcs.append((where, field.astype(F32), tone, lit))

    times, gaps = timeline((280, 6), (960, 26), (800, 8))
    frames: list[bytes] = []
    for T in times:
        light = Light(stage, PAINT)
        # 5. the remnant: each filament flares as the shock passes, then glows and fades
        fade_out = 1 - ramp(1.25, 2.05, T)
        for where, field, tone, lit in arcs:
            if lit < T:
                level = (0.32 + 0.68 * math.exp(-(T - lit) / 0.2)) * ramp(lit, lit + 0.06, T) * fade_out
                light.shine(tone, field * 0.75 * level, where)

        # the flash's own light races out ahead of the shock: one thin ring
        e = (T - 0.27) / 0.3
        if 0 < e < 1:
            R = big * 1.02 * (1 - (1 - e) ** 2)
            where = p.box(R + 4 * line)
            light.shine(hot * 0.5 + ink.accent * 0.5, _shell(p.d[where], R, 0.05 * line + 0.6 * u)
                        * 1.4 * (1 - e) ** 1.5, where)

        # 3. the shockwave: a ragged bright front with a soft wake inside it
        e = (T - 0.3) / 0.95
        if 0 < e < 1:
            R = shock_radius(e)
            front_w = (0.1 * line + 0.6 * u) * (1 + 1.2 * e)
            where = p.box(R * 1.04 + 3 * front_w)
            gap = p.d[where] - R * ragged[where]
            front = np.exp(-((gap / front_w) ** 2))
            wake = np.exp(np.minimum(gap, 0) / (0.08 * R + 2)) * smoothstep(0.5 * front_w, -1.5 * front_w, gap)
            fade = (1 - e) ** 1.6
            light.shine(ink.accent, wake * 0.34 * (1 - e) ** 2.4 * ink.haze, where)
            light.shine(ink.color, front * knots[where] * 1.3 * fade, where)
            light.shine(hot, front * knots[where] * 0.8 * fade * (1 - e) ** 2, where)

        # 4. debris with short trails
        for i in range(n_bits):
            e = (T - 0.32) / (1.3 * bit_life[i])
            if not 0 < e < 1:
                continue
            ca, sa = math.cos(bit_angle[i]), math.sin(bit_angle[i])
            dist = bit_far[i] * (1 - math.exp(-e * 5.0)) / (1 - math.exp(-5.0))
            back = bit_far[i] * (1 - math.exp(-max(0.0, e - 0.07) * 5.0)) / (1 - math.exp(-5.0))
            level = (1 - e) ** 1.4 * (0.75 + 0.25 * math.cos(37 * e + i))
            tone = ink.color * (1 - bit_tone[i]) + ink.accent * bit_tone[i]
            x1, y1 = cx + dist * ca, cy + dist * sa
            size = (0.06 * line + 0.4 * u) * bit_size[i]
            _dash(light, cx + back * ca, cy + back * sa, x1, y1, size * 1.1, tone, 1.1 * level)
            _spark(light, x1, y1, size, hot * 0.5 + tone * 0.5, 1.6 * level)

        # 1. the star brightens and shrinks, while a faint ring falls into it
        pre = min(1.0, T / 0.28)
        if T < 0.3:
            fall = 0.3 * sc + 2.6 * sc * (1 - pre) ** 1.4
            where = p.box(fall + 2 * sc)
            light.shine(ink.accent, _shell(p.d[where], fall, 0.12 * sc + 0.5 * u) * (0.25 + 0.75 * pre) * 0.7,
                        where)
        # 2. the flash, white into colour, with a cinematic streak across
        flash = bump(T - 0.27, 0.04, 0.34)
        shrink = 1 - 0.35 * pre * pre
        star = (0.35 + 1.9 * pre * pre) * (1 - ramp(0.28, 0.5, T))
        core_r = 0.32 * sc * shrink + 1.6 * sc * flash
        where = p.box(core_r * 1.8)
        d = p.d[where]
        light.shine(hot, np.exp(-2.0 * (d / core_r) ** 2) * smoothstep(core_r * 1.8, core_r, d)
                    * (star + 3.0 * flash), where)
        glow_r = 2.4 * sc * shrink ** 2 + 5.0 * sc * flash
        where = p.box(glow_r)
        d = p.d[where]
        light.shine(ink.color, np.exp(-3.0 * (d / glow_r) ** 2) * smoothstep(glow_r, glow_r * 0.5, d)
                    * (0.4 * star + 0.9 * flash) * ink.haze, where)
        spike = 0.7 * star * (1 - ramp(0.26, 0.4, T)) + 1.3 * flash
        if spike > 0.01:
            thick = (0.07 * sc + 0.5 * u) * (1 + flash)
            reach = (1.2 + 1.4 * pre) * sc + rx * 0.95 * flash
            where = p.box(reach, 6 * thick)
            ax, ay = np.abs(p.dx[where]), np.abs(p.dy[where])
            streak = np.exp(-ay / thick - ax / (reach * 0.35)) * smoothstep(reach, reach * 0.4, ax)
            light.shine(hot * 0.6 + ink.accent * 0.4, streak * spike, where)
            reach = (0.9 + 0.8 * pre) * sc + 1.2 * sc * flash
            where = p.box(6 * thick, reach)
            ax, ay = np.abs(p.dx[where]), np.abs(p.dy[where])
            streak = np.exp(-ax / thick - ay / (reach * 0.4)) * smoothstep(reach, reach * 0.4, ay)
            light.shine(hot * 0.6 + ink.color * 0.4, streak * spike * 0.6, where)
        frames.append(light.frame())
    return [*frames, blank(stage)], [*gaps, gaps[-1]]


def _smax(a: Array, b: Array, k: float) -> Array:
    """A smooth maximum of two distance fields: their intersection, with its corners rounded over ``k``."""
    h = np.clip(0.5 + 0.5 * (a - b) / k, 0, 1)
    return b + (a - b) * h + k * h * (1 - h)


# ================================================================ pulsar ==


def pulsar(stage: Stage) -> tuple[list[bytes], list[int]]:
    """A bright star at the anchor's centre sweeps two narrow beams round like
    a lighthouse, and each time they point sideways it sends out a sharp ring.
    Each ring leaves the star as a circle, flattens against the anchor's edges
    and wraps its rounded outline as it grows, so a wide panel is framed, not
    covered. Three rings, 0.4 s apart; about 1.6 seconds."""
    cx, cy = centre(stage.at)
    ink = _Ink(stage)
    u = unit(stage)
    cw, ch = stage.cell
    _, _, aw, ah = stage.at
    rx, ry = _reach(stage, cx, cy)
    # the anchor's outline, through the middle of its edge cells, as glass draws a panel's border
    hw, hh = max(1.0, aw / 2 - 0.5 * cw), max(1.0, ah / 2 - 0.5 * ch)
    rad = max(0.0, min(0.55 * ch, hw, hh))
    w_ring = 0.07 * ch + 0.5 * u
    # how far past the outline the rings go: further round a small anchor
    out = max(0.4 * ch, min(min(rx - hw, ry - hh) - 3.5 * w_ring, 2.2 * ch + max(0.0, 1.5 * ch - hh)))
    p = _Polar(stage, cx, cy)
    where = p.box(hw + out + 4 * w_ring, hh + out + 4 * w_ring)
    d, dx, dy = p.d[where], p.dx[where], p.dy[where]
    qx, qy = np.abs(dx) - (hw - rad), np.abs(dy) - (hh - rad)
    outline = (np.sqrt(np.maximum(qx, 0) ** 2 + np.maximum(qy, 0) ** 2) + np.minimum(np.maximum(qx, qy), 0) - rad)
    corner = math.hypot(hw, hh) + out
    r = rng(stage)
    hot = ink.hot
    sc = max(0.55 * ch, min(ch, 0.5 * math.sqrt(hw * hh)))  # the star's size

    spin = float(r.choice([-1.0, 1.0]))
    every = 0.4
    omega = spin * math.pi / every  # half a turn between rings: a beam points sideways at each one
    emit = [0.14 + every * k for k in range(3)]
    theta0 = -omega * emit[0]
    # the beams reach to where the rings end, fading on the way
    beam_fall = smoothstep(out * 0.8, -max(hw, hh) * 0.15, outline)
    beam_fall = beam_fall * np.exp(-d / (0.6 * math.hypot(hw + out, hh + out)))
    beam_w = 0.06 * ch + 0.4 * u + d * 0.03
    beam_hot = beam_fall * (1.0 + 2.2 * np.exp(-d / (2.5 * sc)))  # white-hot near the star

    times, gaps = timeline((1600, 38))
    frames: list[bytes] = []
    for T in times:
        light = Light(stage, PAINT)
        alive = ramp(-0.06, 0.12, T) * (1 - ramp(1.15, 1.45, T))
        theta = theta0 + omega * T

        # the rings: a circle from the star, held by the outline as it reaches it, then pushed out past it
        for t0 in emit:
            e = (T - t0) / 0.66
            if not 0 < e < 1:
                continue
            grow = 1 - (1 - e) ** 2.2
            circle = d - corner * 1.05 * min(1.0, grow / 0.7)
            push = -0.25 * ch + (out + 0.25 * ch) * ramp(0.3, 1.0, e)
            field = _smax(circle, outline - push, 0.5 * ch) / (w_ring * (1 + 0.4 * e))
            level = (1 - e) ** 1.6 * (1.2 + 1.6 * (1 - e) ** 3)  # white-hot as it leaves the star
            cool = 0.8 * ramp(0.15, 0.85, e)  # and cooling into the second tone as it spreads
            light.shine(ink.color * (1 - cool) + ink.accent * cool, np.exp(-field * field) * level, where)

        # the beams: one line through the star, bright near it
        if alive > 0:
            across = np.abs(dx * math.sin(theta) - dy * math.cos(theta)) / beam_w
            light.shine(ink.color, np.exp(-across * across) * beam_hot * alive, where)

        # the star: a white core with a halo and four spikes, a beat brighter at each ring
        beat = sum(bump(T - t0, 0.03, 0.22) for t0 in emit)
        level = alive * (0.85 + 0.6 * beat)
        _spark(light, cx, cy, 0.28 * sc * (1 + 0.25 * beat), hot, 2.4 * level, spikes=0.0)
        wh = p.box(2.2 * sc)
        dd = p.d[wh]
        light.shine(ink.color, np.exp(-3.0 * (dd / (2.2 * sc)) ** 2) * smoothstep(2.2 * sc, 1.1 * sc, dd)
                    * 0.55 * level * ink.haze, wh)
        spikes = p.box(2.6 * sc)
        ax, ay = np.abs(p.dx[spikes]), np.abs(p.dy[spikes])
        thin = 0.05 * sc + 0.4 * u
        cross = (np.exp(-ay / thin - ax / (0.7 * sc)) + np.exp(-ax / thin - ay / (0.7 * sc)))
        light.shine(hot * 0.5 + ink.color * 0.5, cross * smoothstep(2.6 * sc, 1.2 * sc, np.maximum(ax, ay))
                    * 0.9 * level, spikes)
        frames.append(light.frame())
    return [*frames, blank(stage)], [*gaps, gaps[-1]]


# ============================================================ black hole ==

#: The colour of a black hole's shadow.
VOID = np.array([0.01, 0.01, 0.03], F32)


def black_hole(stage: Stage) -> tuple[list[bytes], list[int]]:
    """Light swirls into the anchor's centre: a tilted accretion disc winds
    round a dark core, inside a thin photon ring, with the far side of the disc
    bent over the top of the shadow; sparks spiral in faster and faster. Then
    everything collapses to a point, a tiny flash, and nothing. About 1.4
    seconds."""
    cx, cy = centre(stage.at)
    ink = _Ink(stage)
    u = unit(stage)
    _, ch = stage.cell
    _, _, aw, _ = stage.at
    rx, ry = _reach(stage, cx, cy)
    tilt = 0.3
    # the shadow's radius: it follows the cell, but the disc round it never covers more than a sixth of a full frame
    hole = min(1.45 * ch, 0.42 * ry, 0.22 * rx, 0.08 * math.sqrt(MAX_PX))
    outer = min(0.92 * rx, 0.92 * ry / tilt, 5.2 * hole)  # the disc's, in its own plane
    inner = 1.5 * hole
    p = _Polar(stage, cx, cy)
    where = p.box(outer * 1.02 + 2, max(outer * tilt, 1.7 * hole) + 2)
    dx, dy, d, phi = p.dx[where], p.dy[where], p.d[where], p.phi[where]
    r = rng(stage)
    hot = ink.hot
    spin = float(r.choice([-1.0, 1.0]))

    # the disc in its own plane: radius and angle
    flat_y = dy / tilt
    rho = np.sqrt(dx * dx + flat_y * flat_y)
    psi = np.arctan2(flat_y, dx)
    log_rho = np.log(np.maximum(rho, 1.0) / inner)
    omega = (inner / np.maximum(rho, inner * 0.5)) ** 1.2  # inner gas turns faster: the arms wind up
    beaming = 1.0 + 0.55 * np.cos(psi + spin * math.pi / 2)  # the side coming towards us is brighter
    front = (dy >= 0).astype(F32)  # the near half of the disc passes in front of the shadow
    arms = int(r.integers(2, 4))
    twist = r.uniform(2.5, 3.5)
    phase = r.uniform(0, TAU)

    # sparks that fall in from beyond the disc, faster as they go
    n_sparks = 20
    # from as far as the anchor reaches across, so a wide row's light is drawn in too
    start = min(0.95 * rx, max(outer, aw / 2), 2.2 * outer)
    spark_rho = [float(v) for v in start * r.uniform(0.7, 1.0, n_sparks)]
    spark_psi = [float(v) for v in r.uniform(0, TAU, n_sparks)]
    spark_t0 = [float(v) for v in r.uniform(0.0, 0.55, n_sparks)]
    spark_life = [float(v) for v in r.uniform(0.45, 0.65, n_sparks)]
    spark_tone = [float(v) for v in r.uniform(0, 1, n_sparks)]
    # how far round each one goes: more from further out, but never so fast that a trail can't bend
    spark_turns = [min(2.6 * math.pi, 1.3 * ((v / inner) ** 1.25 - 1)) for v in spark_rho]

    def spark_at(i: int, t: float, squeeze: float) -> tuple[float, float, float]:
        """Where spark ``i`` is at time ``t``: x, y and its radius in the disc's plane."""
        e = min(1.0, max(0.0, (t - spark_t0[i]) / spark_life[i]))
        rad = spark_rho[i] * (1 - e) ** 0.75 * squeeze + inner * 0.85 * e
        turn = spark_psi[i] + spin * spark_turns[i] * e ** 1.8  # faster and faster
        return cx + rad * math.cos(turn), cy + rad * math.sin(turn) * tilt, rad

    def hidden(at: tuple[float, float, float], squeeze: float) -> bool:
        """Behind the shadow: on the far side of the disc, inside the hole's outline."""
        return at[1] < cy and math.hypot(at[0] - cx, at[1] - cy) < hole * squeeze * 1.1

    times, gaps = timeline((960, 24), (300, 9), (180, 4))
    frames: list[bytes] = []
    for T in times:
        light = Light(stage, PAINT)
        form = ramp(-0.08, 0.32, T)
        # the collapse: everything shrinks to a point, spinning faster
        fall = ramp(0.92, 1.25, T) ** 1.6
        squeeze = max(0.02, 1 - fall)
        spin_t = spin * (4.2 * T + 6.0 * fall)
        if squeeze > 0.03:
            q = rho / squeeze
            edge = inner + (outer - inner) * 0.85 * (1 - form) ** 1.5  # the gas arrives from outside
            radial = (inner / np.maximum(q, inner)) ** 1.5 * smoothstep(edge * 0.85, edge * 1.1, q)
            radial = radial * smoothstep(outer, outer * 0.5, q) * (0.3 + 0.7 * form)
            swirl = np.cos(arms * (psi - omega * spin_t) + twist * log_rho + phase)
            gas = radial * (0.55 + 0.45 * swirl) * beaming * form * (0.6 + 0.4 * squeeze)
            heat = smoothstep(inner * 2.6, inner, q)  # white-hot and the second tone near the hole
            # the far half of the disc, then the shadow over it, then the near half in front
            for half in (1 - front, None, front):
                if half is not None:
                    light.shine(ink.color, gas * half * 0.95, where)
                    light.shine(ink.accent, gas * heat * half * 1.3, where)
                    continue
                # the shadow, the photon ring round it, and the far side of the disc bent over its top
                hr = hole * squeeze * (0.4 + 0.6 * form)
                dq = d / max(hr, 0.5)
                light.over(VOID, smoothstep(1.08, 0.9, dq) * (0.88 if stage.dark else 0.7) * form, where)
                over_top = 0.3 + 0.7 * np.clip(-dy / np.maximum(d, 1.0), 0, 1)
                lens = np.exp(-(((dq - 1.3) / 0.17) ** 2)) * over_top * form * (
                    0.75 + 0.25 * np.cos(arms * (phi - spin_t * 0.8) + phase))
                light.shine(ink.color, lens * 0.9, where)
                light.shine(ink.accent, lens * np.exp(-(((dq - 1.2) / 0.08) ** 2)) * 0.8, where)
                thin = (0.5 * u + 0.025 * hr) / max(hr, 0.5)
                light.shine(hot * 0.5 + ink.accent * 0.5, np.exp(-(((dq - 1.03) / thin) ** 2)) * form
                            * (0.75 + 0.45 * np.cos(phi + spin * math.pi / 2)), where)

        # sparks spiralling in, each with a short curved trail
        for i in range(n_sparks):
            if not spark_t0[i] < T < spark_t0[i] + spark_life[i]:
                continue
            level = ramp(0.0, 0.08, T - spark_t0[i]) * 1.3
            tone = ink.color * (1 - spark_tone[i]) + ink.accent * spark_tone[i]
            size = 0.05 * ch + 0.45 * u
            head = spark_at(i, T, squeeze)
            near = ramp(inner * 0.9, inner * 1.3, head[2])  # each one fades into the disc's bright inner edge
            if level * near <= 0 or hidden(head, squeeze):
                continue
            _spark(light, head[0], head[1], size, hot * 0.5 + tone * 0.5, level * near)
            # the trail bends with the orbit: where the spark was a moment ago, and before that
            trail = [head] + [spark_at(i, T - 0.012 * k, squeeze) for k in range(1, 6)]
            _trail(light, [(x, y) for x, y, _ in trail],
                   [0.0 if hidden(at, squeeze) else level * near * (1 - k / 5) for k, at in enumerate(trail)], size,
                   tone)

        # the last of it: a tiny flash where it all went, and nothing
        pop = bump(T - 1.16, 0.04, 0.2)
        if pop > 0:
            _spark(light, cx, cy, 0.18 * hole + 0.6 * u, hot, 2.2 * pop, spikes=0.9)
            echo = hole * (0.3 + 1.6 * (T - 1.16) / 0.2)
            box = p.box(echo + 2.0 * ch)
            light.shine(ink.accent, _shell(p.d[box], echo, 0.6 * u + 0.04 * ch) * 0.7 * pop, box)
        frames.append(light.frame())
    return [*frames, blank(stage)], [*gaps, gaps[-1]]


KINDS = {
    "sunburst": Flourish(sunburst, grow=(8, 4), color="warning", accent="error",
                         about="a win, right here: merged, passed, shipped"),
    "supernova": Flourish(supernova, grow=(14, 6), color="secondary", accent="primary",
                          about="the big win: a release out, a milestone reached"),
    "pulsar": Flourish(pulsar, grow=(7, 4), color="primary", accent="secondary",
                       about="look here: this needs you"),
    "black-hole": Flourish(black_hole, grow=(6, 3), color="secondary", accent="primary",
                           about="gone: closed, archived or snoozed"),
}

__all__ = ["KINDS", "black_hole", "pulsar", "sunburst", "supernova"]
