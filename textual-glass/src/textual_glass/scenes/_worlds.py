"""The worlds the space scenes play around: a banded giant with a ring, a
world at night, an eclipsed planet. Pure numpy, no Textual imports.
Exploratory.

Each world is a few pictures that never move: a loop the terminal plays (bands
that flow, clouds that drift, a corona that shimmers) and stills drawn once
(a ring, a sharp limb). Nothing that stays on screen rides a spinning surface,
so once the show ends glass sends nothing (rule 9).
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass

import numpy as np

from ..render import F32, Array, smoothstep
from . import Stage
from ._paint import TAU, WHITE, Noise, Paint, Sprite, blend, disc, ink, lum, rot_x, rot_z, star, stretched_size, unit

#: A loop's frames (the terminal plays them) and how long each shows.
LOOP_FRAMES, LOOP_GAP = 32, 160
#: Pixels per frame of a stretched loop, and of a still drawn once.
LOOP_PX, STILL_PX = 60_000, 400_000
#: The halo's orbits at most: drawn at the terminal's own pixels up to this, stretched beyond it.
ORBITS_PX = 1_500_000


@dataclass(frozen=True)
class Place:
    """A stretched picture's cells on the stage, and where the world's centre is, in terminal pixels."""

    col: int
    row: int
    cols: int
    rows: int
    cx: float
    cy: float

    def centre(self, stage: Stage) -> tuple[float, float]:
        cw, ch = stage.cell
        return (self.col + self.cols / 2) * cw, (self.row + self.rows / 2) * ch

    def offset(self, stage: Stage) -> tuple[float, float]:
        """Where the world's centre is from the picture's centre (``Sprite.offset``)."""
        x, y = self.centre(stage)
        return self.cx - x, self.cy - y


def place(stage: Stage, cx: float, cy: float, half_w: float, half_h: float) -> Place:
    """Whole cells holding a picture centred near (cx, cy) that reaches ``half_w`` and ``half_h`` pixels out."""
    cw, ch = stage.cell
    col0 = max(0, math.floor((cx - half_w) / cw))
    row0 = max(0, math.floor((cy - half_h) / ch))
    col1 = min(stage.cols, math.ceil((cx + half_w) / cw))
    row1 = min(stage.rows, math.ceil((cy + half_h) / ch))
    return Place(col0, row0, col1 - col0, row1 - row0, cx, cy)


def _box(stage: Stage, cx: float, cy: float, x0: float, y0: float, x1: float, y1: float) -> Place:
    """Whole cells holding the pixels from (x0, y0) to (x1, y1), for a world centred at (cx, cy)."""
    cw, ch = stage.cell
    col0, row0 = max(0, math.floor(x0 / cw)), max(0, math.floor(y0 / ch))
    col1, row1 = min(stage.cols, math.ceil(x1 / cw)), min(stage.rows, math.ceil(y1 / ch))
    return Place(col0, row0, col1 - col0, row1 - row0, cx, cy)


def world_centre(stage: Stage) -> tuple[float, float]:
    """The middle of the stage, moved to a cell corner, so a world drawn on whole cells is centred on it."""
    cw, ch = stage.cell
    return round(stage.cols / 2) * cw, round(stage.rows / 2) * ch


# ============================================================ the giant ==


@dataclass(frozen=True)
class Giant:
    """A banded gas giant with a ring, all of it worked out from the stage: its centre and radius in
    terminal pixels, its axis, the sun, and the ring's and the docking rail's radii (in planet radii)."""

    cx: float
    cy: float
    radius: float
    axis: Array  # the planet's frame: columns are its x, y (north) and z axes, in view space
    sun: Array
    ring: tuple[float, float] = (1.34, 2.12)
    rail: float = 2.34

    @classmethod
    def on(cls, stage: Stage) -> Giant:
        cx, cy = world_centre(stage)
        radius = min(0.29 * stage.height, 0.85 * stage.width / (2 * 2.42))
        axis = rot_z(math.radians(9)) @ rot_x(math.radians(27))
        return cls(cx, cy, radius, axis, unit(-0.55, 0.52, 0.66))

    @property
    def normal(self) -> Array:
        return self.axis[:, 1]

    def ring_point(self, r: float, phi: float) -> tuple[float, float, float]:
        """A point of the ring plane (``r`` radii out, ``phi`` round from the planet's x axis towards the
        viewer) as stage pixels, and its depth (positive: in front of the planet's centre)."""
        u, w = self.axis[:, 0], self.axis[:, 2]
        p = r * (math.cos(phi) * u + math.sin(phi) * w)
        return self.cx + float(p[0]) * self.radius, self.cy - float(p[1]) * self.radius, float(p[2])

    def berth_edges(self, days: int = 7) -> list[float]:
        """The rail's day boundaries, as angles round the ring from its left tip to its right one, spaced
        evenly along the rail as it looks on screen (the ring's tips are foreshortened)."""
        return [self.rail_angle(k / days) for k in range(days + 1)]

    def rail_angle(self, s: float) -> float:
        """The angle round the ring of the point ``s`` (0 to 1) of the way along the front of the rail, on
        screen, from its left tip."""
        phis, length = self._rail
        return float(np.interp(s * length[-1], length, phis))

    @property
    def rail_length(self) -> float:
        """How long the front of the rail is on screen, tip to tip, in terminal pixels."""
        return float(self._rail[1][-1])

    @functools.cached_property
    def _rail(self) -> tuple[Array, Array]:
        phis = np.linspace(math.radians(170), math.radians(10), 160)
        pts = np.array([self.ring_point(self.rail, float(a))[:2] for a in phis])
        return phis, np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])

    def ring_density(self, r: Array, seed: int) -> Array:
        """How opaque the ring is ``r`` radii out: ringlets, a dark gap, a faint inner ring."""
        rnd = np.random.default_rng(seed)
        r0, r1 = self.ring
        lanes = np.zeros_like(r)
        for f in rnd.uniform(28, 140, 9):
            lanes = lanes + np.sin(r * f + rnd.uniform(0, TAU)) / 9
        body = smoothstep(r0 - 0.02, r0 + 0.06, r) * smoothstep(r1 + 0.01, r1 - 0.07, r)
        gap = 1 - 0.92 * np.exp(-(((r - 1.83) / 0.028) ** 2))
        thin = 1 - 0.75 * np.exp(-(((r - 2.03) / 0.007) ** 2))
        faint = 0.35 + 0.65 * smoothstep(1.52, 1.6, r)
        return np.clip(body * gap * thin * faint * (0.72 + 0.9 * lanes), 0, 1)


def _giant_inks(stage: Stage) -> list[Array]:
    """The giant's band colours from the theme: zone, belt, teal, tan, polar hood, storm."""
    bg, fg = ink(stage, "background"), ink(stage, "foreground")
    primary, secondary, accent = ink(stage, "primary"), ink(stage, "secondary"), ink(stage, "accent")

    def calm(c: Array, k: float = 0.3) -> Array:  # a little less saturated
        return blend(c, np.full(3, lum(c), F32), k)

    if stage.dark:
        zone = calm(blend(fg, accent, 0.2), 0.4) * 0.62
        belt = calm(blend(bg, secondary, 0.45), 0.35) * 0.85
        teal = calm(blend(bg, primary, 0.38), 0.3) * 0.85
        tan = calm(blend(zone, accent, 0.4), 0.3) * 0.85
        polar = calm(blend(bg, blend(primary, secondary, 0.5), 0.3), 0.2)
        storm = blend(zone, WHITE, 0.15)
    else:
        zone = blend(bg, accent, 0.14)
        belt = calm(blend(secondary, bg, 0.5), 0.45)
        teal = calm(blend(primary, bg, 0.5), 0.45)
        tan = calm(blend(bg, accent, 0.35), 0.3)
        polar = calm(blend(blend(primary, secondary, 0.5), bg, 0.3), 0.45)
        storm = blend(bg, WHITE, 0.6)
    return [zone, belt, teal, tan, polar, storm]


def _band_table(stage: Stage, inks: list[Array], n: int = 1024) -> tuple[Array, Array, Array]:
    """The giant's bands as a table over latitude (-pi/2 to pi/2): colour, and how sheared each latitude is
    (most at the edges between bands, where the turbulence lives)."""
    zone, belt, teal, tan, polar, _ = inks
    rnd = np.random.default_rng(stage.seed * 17 + 3)
    lat = np.linspace(-math.pi / 2, math.pi / 2, n, dtype=F32)
    # bands from the equator out: a wide pale zone, dark equatorial belts, then narrower ones
    edges = [0.0, 0.13]
    while edges[-1] < 1.6:
        edges.append(edges[-1] + float(rnd.uniform(0.035, 0.17)))
    edges_arr = np.array(edges, F32)
    picks = [zone, belt, tan, teal, zone, belt, teal, tan, zone, belt, teal, zone, belt, teal, tan, belt]
    colour = np.zeros((n, 3), F32)
    for hemi in (1, -1):
        a = np.abs(lat) * (np.sign(lat) == hemi)
        k = np.clip(np.searchsorted(edges_arr, a) - 1, 0, len(picks) - 1)
        tone = np.array([blend(picks[(i + (0 if hemi > 0 else 1) * (i > 1)) % len(picks)], zone,
                               float(rnd.uniform(0.15, 0.55))) for i in range(len(picks))])
        bright = rnd.uniform(0.86, 1.08, len(picks)).astype(F32)
        rows = (np.sign(lat) == hemi) | ((lat == 0) & (hemi > 0))
        colour[rows] = (tone[k] * bright[k][:, None])[rows]
    # soft edges, fine striations, a darker polar hood
    kernel = np.exp(-np.linspace(-3, 3, 13) ** 2).astype(F32)
    kernel /= kernel.sum()
    soft = np.stack([np.convolve(np.pad(colour[:, i], 6, mode="edge"), kernel, "valid") for i in range(3)], 1)
    stria = np.zeros(n, F32)
    for f in rnd.uniform(60, 180, 6):
        stria += np.sin(lat * f + rnd.uniform(0, TAU)).astype(F32) / 6
    soft = soft * (1 + 0.06 * stria)[:, None]
    hood = smoothstep(1.0, 1.35, np.abs(lat))
    soft = blend(soft, polar, hood * 0.85)
    shear = np.abs(np.gradient(soft.sum(1)))
    shear = np.clip(shear / max(1e-6, float(np.percentile(shear, 97))), 0, 1)
    return lat, soft.astype(F32), np.convolve(shear, kernel, "same").astype(F32)


def giant_loop(stage: Stage, giant: Giant, frames: int = LOOP_FRAMES, max_px: int = LOOP_PX) -> Sprite:
    """The giant itself, stretched over its cells: bands that flow both ways (each wave travels a whole
    period per loop, so the loop is seamless), turbulence along their edges, a storm whose swirl turns, the ring's
    shadow across it and a thin atmosphere. It never turns, so nothing on it has to move."""
    reach = giant.radius * 1.09
    spot = place(stage, giant.cx, giant.cy, reach, reach)
    w, h, s = stretched_size(stage, spot.cols, spot.rows, max_px)
    ox, oy = spot.cx - spot.col * stage.cell[0], spot.cy - spot.row * stage.cell[1]
    R = giant.radius * s
    d = disc(w, h, ox * s, oy * s, R, pad=0.09)
    n = np.stack([d.nx, d.ny, d.nz])
    p = giant.axis.T @ n  # planet frame
    lat = np.arcsin(np.clip(p[1], -1, 1))
    lon = np.arctan2(p[0], p[2])
    noise = Noise(stage.seed * 31 + 5)
    rnd = np.random.default_rng(stage.seed * 13 + 1)
    inks = _giant_inks(stage)
    table_lat, table, shear_table = _band_table(stage, inks)
    storm_c = inks[5]

    # light: the sun, a soft terminator, a darker limb; the ring's shadow
    mu = giant.sun @ n
    day = smoothstep(-0.12, 0.42, mu) * (0.62 + 0.38 * np.clip(mu, 0, 1))
    ambient = 0.05 if stage.dark else 0.32
    limb = np.clip(d.nz, 0, 1) ** 0.28
    shade = (ambient + (1 - ambient) * day) * (0.55 + 0.45 * limb)
    nrm = giant.normal
    denom = float(giant.sun @ nrm)
    t_hit = -(nrm @ n) / denom
    hit = n + giant.sun[:, None] * t_hit
    shadow = np.where(t_hit > 0, giant.ring_density(np.sqrt((hit * hit).sum(0)), stage.seed), 0)
    shade = shade * (1 - 0.72 * shadow)

    # zonal jets: waves along the bands, each band's own way, calm towards the poles
    calm_poles = np.cos(lat) ** 3
    jets = [(int(rnd.integers(6, 14)), float(rnd.uniform(0.006, 0.014)), float(rnd.uniform(-1.1, 1.1)),
             float(rnd.uniform(0.08, 0.2)), int(rnd.choice([-1, 1])), float(rnd.uniform(0, TAU)))
            for _ in range(7)]
    envelopes = [np.exp(-(((lat - at) / width) ** 2)) * calm_poles for _, _, at, width, _, _ in jets]
    shear = np.interp(lat, table_lat, shear_table)
    storm_lon, storm_lat = -0.38, 0.28
    dl = (lon - storm_lon) * math.cos(storm_lat) / 0.2
    dp = (lat - storm_lat) / 0.085
    q = np.sqrt(dl * dl + dp * dp)
    swirl_at = np.arctan2(dp, dl)
    out: list[bytes] = []
    for f in range(frames):
        t = f / frames
        ang = TAU * t
        cs, sn = 0.3 * math.cos(ang), 0.3 * math.sin(ang)
        broad = noise.fbm(p[0] * 2.2 + cs, p[1] * 2.2 + sn, p[2] * 2.2, octaves=3) - 0.5
        fine = noise.fbm(p[0] * 7.0 - sn * 2, p[1] * 7.0 + cs * 2, p[2] * 7.0, octaves=4) - 0.5
        eddy = noise.fbm(p[0] * 15.0 + sn * 3, p[1] * 15.0, p[2] * 15.0 + cs * 3, octaves=2) - 0.5
        y = lat + 0.045 * broad + (0.01 + 0.075 * shear) * fine + 0.022 * shear * eddy
        for (k, amp, _, _, direction, phase), env in zip(jets, envelopes, strict=True):
            y = y + amp * np.sin(k * lon + direction * ang + phase) * env
        # the storm drags the bands round it: a swirl that turns half a turn per loop
        pull = smoothstep(1.6, 0.2, q)
        y = y + 0.03 * pull * np.sin(swirl_at + 2.5 * (1.4 - q) - ang)  # one turn a loop: seamless
        c = np.stack([np.interp(y, table_lat, table[:, i]) for i in range(3)], 1)
        arms = 0.5 + 0.5 * np.sin(2 * swirl_at + 4.0 * q - ang)
        eye = smoothstep(1.0, 0.45, q)
        collar = np.exp(-(((q - 1.05) / 0.18) ** 2))
        c = c * (1 - 0.35 * collar)[:, None]
        c = blend(c, storm_c * (0.82 + 0.25 * arms[:, None]), eye * 0.9)

        paint = Paint(w, h, stage.dark, margin=1.5)  # it moves: 6 bits of colour are plenty
        rgb = paint.rgb.reshape(-1, 3)
        a = paint.a.reshape(-1)
        body = d.cover
        lit = c * shade[:, None]
        # a thin atmosphere: a glow over the limb, lit on the sun's side
        primary = ink(stage, "primary")
        rim = np.exp(-np.abs(d.r - 1.0) / 0.018) * smoothstep(1.085, 1.0, d.r)
        rim_lit = rim * (0.12 + 0.6 * smoothstep(-0.3, 0.6, giant.sun[0] * d.nx + giant.sun[1] * d.ny))
        haze = blend(primary, WHITE, 0.3) * (0.55 if stage.dark else 0.8)
        rgb[d.inside] = lit * body[:, None] + haze * rim_lit[:, None] * (1 - body[:, None] * 0.6)
        a[d.inside] = np.clip(body + rim_lit * (1 - body), 0, 1)
        out.append(paint.frame())
    return Sprite(out, w, h, [LOOP_GAP], True, (spot.cols, spot.rows),
                  offset=spot.offset(stage), name="giant")


def giant_ring(stage: Stage, giant: Giant, front: bool, days: int = 7, max_px: int = STILL_PX) -> Sprite:
    """One half of the ring, a still: the back half goes under the giant, the front half over it. The
    front half carries the docking rail, cut into ``days`` berths, where the ships dock. The giant's
    shadow falls across the back half."""
    rail = giant.rail
    u, wv = giant.axis[:, 0], giant.axis[:, 2]
    # the half's box: the ring and the rail sampled round that half
    phis = np.linspace(0, math.pi, 64) + (0 if front else math.pi)
    pts = [giant.ring_point(r_, a) for r_ in (giant.ring[0], rail + 0.05) for a in phis]
    xs_, ys_ = [x for x, _, _ in pts], [y for _, y, _ in pts]
    spot = _box(stage, giant.cx, giant.cy, min(xs_) - 4, min(ys_) - 4, max(xs_) + 4, max(ys_) + 4)
    w, h, s = stretched_size(stage, spot.cols, spot.rows, max_px)
    cw, ch = stage.cell
    ox, oy = (spot.cx - spot.col * cw) * s, (spot.cy - spot.row * ch) * s
    R = giant.radius * s
    paint = Paint(w, h, stage.dark, margin=1.5)
    X = (paint.xs - ox) / R
    Y = (oy - paint.ys) / R
    nrm = giant.normal
    Z = -(nrm[0] * X + nrm[1] * Y) / nrm[2]
    X, Y = np.broadcast_to(X, Z.shape), np.broadcast_to(Y, Z.shape)
    r = np.sqrt(X * X + Y * Y + Z * Z)
    side = (Z > 0) if front else (Z <= 0)
    density = giant.ring_density(r, stage.seed) * side

    # colour: bluer inside, creamier outside; the lit face; the giant's shadow
    fg, primary, accent = ink(stage, "foreground"), ink(stage, "primary"), ink(stage, "accent")
    bg = ink(stage, "background")
    r0, r1 = giant.ring
    tone = smoothstep(r0, r1, r)
    if stage.dark:
        c = blend(blend(fg, primary, 0.3), blend(fg, accent, 0.3), tone) * 0.78
    else:
        c = blend(blend(primary, bg, 0.45), blend(accent, bg, 0.5), tone) * 0.92
    tt = X * giant.sun[0] + Y * giant.sun[1] + Z * giant.sun[2]
    dist = np.sqrt(np.maximum(r * r - tt * tt, 0))
    in_shadow = (tt < 0) * smoothstep(0.97, 1.03, dist)
    lit = (0.25 + 0.75 * (1 - (tt < 0) + in_shadow).clip(0, 1))
    face = 0.55 + 0.45 * abs(float(giant.sun @ nrm))
    paint.over(c * (lit * face)[..., None], density * 0.68)

    # the docking rail: a thin bright band just outside the ring, in berths with gaps between days
    phi = np.arctan2(X * wv[0] + Y * wv[1] + Z * wv[2], X * u[0] + Y * u[1] + Z * u[2])
    px_r = 1 / R  # one image pixel, in radii
    band = np.exp(-(((r - rail) / (1.4 * px_r + 0.005)) ** 2)) * smoothstep(0.03, 0.0, np.abs(r - rail))
    sheen = np.exp(-(((r - rail) / 0.022) ** 2)) * smoothstep(0.06, 0.03, np.abs(r - rail))
    rail_c = blend(fg, primary, 0.25) if stage.dark else blend(primary, fg, 0.5)
    if front:
        edges = giant.berth_edges(days)
        cut = np.ones_like(phi)
        for e in edges:
            cut = cut * smoothstep(0.004, 0.014, np.abs(phi - e))
        cut = cut * (phi > edges[-1] - 0.005) * (phi < edges[0] + 0.005)
        paint.over(rail_c, band * cut * 0.9 * side)
        paint.shine(rail_c, sheen * cut * side * 0.12)
        # end caps: a small pad at each day's edge
        for e in edges:
            x, y, _ = giant.ring_point(rail, e)
            px, py = (x - spot.col * cw) * s, (y - spot.row * ch) * s
            dd = np.sqrt((paint.xs - px) ** 2 + (paint.ys - py) ** 2)
            paint.over(rail_c * 0.8, np.clip(1.6 * s + 0.6 - dd, 0, 1) * 0.9)
    else:
        paint.over(rail_c * 0.7, band * side * 0.45)
    return Sprite([paint.frame()], w, h, [1000], False, (spot.cols, spot.rows),
                  offset=spot.offset(stage), name="ring-front" if front else "ring-back")


def berth_edges(days: int) -> list[float]:
    """The rail's day boundaries, as angles round the ring from the left tip to the right one."""
    lo, hi = math.radians(8), math.radians(172)
    return [hi - (hi - lo) * k / days for k in range(days + 1)]


# ============================================================= the night ==


@dataclass(frozen=True)
class Terra:
    """A blue world seen mostly at night, the sun beyond its right limb: its centre and radius in terminal
    pixels and the sun's direction (view space: x right, y up, z towards the viewer)."""

    cx: float
    cy: float
    radius: float
    sun: Array

    @classmethod
    def on(cls, stage: Stage) -> Terra:
        cx, cy = world_centre(stage)
        return cls(cx, cy, min(0.43 * stage.height, 0.3 * stage.width), unit(0.93, 0.24, -0.28))

    def point(self, lat: float, lon: float) -> tuple[float, float, Array]:
        """A point of the near side (degrees; longitude 0 faces the viewer) as stage pixels, and its normal."""
        la, lo = math.radians(lat), math.radians(lon)
        n = np.array([math.cos(la) * math.sin(lo), math.sin(la), math.cos(la) * math.cos(lo)], F32)
        return self.cx + float(n[0]) * self.radius, self.cy - float(n[1]) * self.radius, n


def _terra_frame(stage: Stage, terra: Terra, max_px: int) -> tuple[Place, int, int, float]:
    reach = terra.radius * 1.07
    spot = place(stage, terra.cx, terra.cy, reach, reach)
    w, h, s = stretched_size(stage, spot.cols, spot.rows, max_px)
    return spot, w, h, s


def _terra_land(stage: Stage, p: Array) -> tuple[Array, Array, Array]:
    """Land (0 to 1), its roughness and the ice, from where a point is on the world (planet frame)."""
    noise = Noise(stage.seed * 7 + 11)
    h = noise.fbm(p[0] * 1.5 + 3.1, p[1] * 1.5, p[2] * 1.5 + 0.7, octaves=6)
    land = smoothstep(0.545, 0.575, h)
    rough = noise.fbm(p[0] * 6 + 9, p[1] * 6, p[2] * 6, octaves=3)
    ice = smoothstep(0.8, 0.86, np.abs(p[1]) + 0.06 * (rough - 0.5))
    return land, rough, ice


def terra_base(stage: Stage, terra: Terra, days: int = 7, max_px: int = STILL_PX) -> Sprite:
    """The world, a still: oceans and continents, a crescent of day with a sunset along the terminator and
    the sun's glint on the sea, the night side dark but readable, faint meridians between the days of the
    week (where the cities are) and a thin atmosphere that glows on the day side."""
    spot, w, h, s = _terra_frame(stage, terra, max_px)
    cw, ch = stage.cell
    R = terra.radius * s
    d = disc(w, h, (spot.cx - spot.col * cw) * s, (spot.cy - spot.row * ch) * s, R, pad=0.07)
    n = np.stack([d.nx, d.ny, d.nz])
    tilt = rot_x(math.radians(-18)) @ rot_z(math.radians(23))
    p = tilt.T @ n
    land, rough, ice = _terra_land(stage, p)
    bg, fg = ink(stage, "background"), ink(stage, "foreground")
    primary, success, accent, error = (ink(stage, r) for r in ("primary", "success", "accent", "error"))
    deep = blend(bg, primary, 0.32) * 0.75
    shallow = blend(bg, primary, 0.55)
    green = blend(blend(bg, success, 0.4), accent, 0.12)
    tan = blend(bg, accent, 0.5)
    coast = smoothstep(0.47, 0.545, Noise(stage.seed * 7 + 11).fbm(p[0] * 1.5 + 3.1, p[1] * 1.5, p[2] * 1.5 + 0.7,
                                                                 octaves=6))
    sea = blend(deep, shallow, coast * 0.6)
    ground = blend(green, tan, smoothstep(0.35, 0.7, rough))
    day_c = blend(blend(sea, ground, land), np.full(3, 0.92, F32), ice)
    mu = terra.sun @ n
    day = smoothstep(-0.03, 0.3, mu)
    dusk = np.exp(-(((mu - 0.05) / 0.07) ** 2))
    warm = blend(accent, error, 0.35)
    # the night: land a shade lighter than the sea, so the cities sit on continents
    if stage.dark:
        ground_c = blend(blend(bg, fg, 0.06), primary, 0.03)
        night = blend(blend(bg, primary, 0.05) * 0.55, ground_c, land * 0.9 + ice * 0.4)
    else:
        night = blend(blend(fg, primary, 0.25) * 0.9, blend(fg, bg, 0.22), land * 0.8 + ice * 0.4)
    c = blend(night, day_c * (0.35 + 0.75 * np.clip(mu, 0, 1))[:, None], day)
    c = c + warm * (dusk * 0.1 * day * (1 - land * 0.3))[:, None]
    # the sun's glint on the sea
    half = terra.sun + np.array([0, 0, 1], F32)
    half = half / np.linalg.norm(half)
    glint = np.clip(half @ n, 0, 1) ** 60 * (1 - land) * day
    c = c + blend(WHITE, accent, 0.3) * (glint * 0.8)[:, None]
    # meridians between the days, on the night side only: dotted, faint
    lon = np.degrees(np.arctan2(d.nx, d.nz))
    lat = np.degrees(np.arcsin(np.clip(d.ny, -1, 1)))
    rule = np.zeros_like(lon)
    for e in day_edges(days):
        rule = np.maximum(rule, np.exp(-(((lon - e) * np.cos(np.radians(lon)) * R / 57.3) ** 2) / 0.5))
    dots = 0.5 + 0.5 * np.cos(np.radians(lat) * 70)
    rule = rule * dots * smoothstep(-0.02, -0.2, mu) * smoothstep(64, 52, np.abs(lat))
    c = blend(c, blend(fg, primary, 0.4), rule * (0.16 if stage.dark else 0.25))
    # a darker limb, then the atmosphere: a bright rim on the day side, a thin blue line at night
    limb = np.clip(d.nz, 0, 1) ** 0.35
    c = c * (0.6 + 0.4 * limb)[:, None]
    sky = blend(primary, WHITE, 0.35)
    side = smoothstep(-0.35, 0.5, terra.sun[0] * d.nx + terra.sun[1] * d.ny)
    rim_in = np.exp(-(1 - np.minimum(d.r, 1)) / 0.05) * (d.r <= 1)
    c = blend(c, sky, rim_in * (0.12 + 0.55 * side * day))
    halo = np.exp(-np.maximum(d.r - 1, 0) / 0.014) * smoothstep(1.07, 1.0, d.r) * (d.r > 1)
    glow = halo * (0.3 + 0.9 * side)
    paint = Paint(w, h, stage.dark, margin=1.5, fine=True)
    rgb, a = paint.rgb.reshape(-1, 3), paint.a.reshape(-1)
    body = d.cover
    rgb[d.inside] = c * body[:, None] + sky * (glow * (1 - body))[:, None]
    a[d.inside] = np.clip(body + glow * (1 - body), 0, 1)
    return Sprite([paint.frame()], w, h, [1000], False, (spot.cols, spot.rows), offset=spot.offset(stage),
                  name="terra")


def terra_clouds(stage: Stage, terra: Terra, frames: int = LOOP_FRAMES, max_px: int = LOOP_PX) -> Sprite:
    """The clouds, a loop the terminal plays: they drift and churn in a circle of noise (so the loop is
    seamless) without the world turning, white on the day side, faint and moonlit at night, with a few
    flickers of lightning in the storms on the night side."""
    spot, w, h, s = _terra_frame(stage, terra, max_px)
    cw, ch = stage.cell
    R = terra.radius * s
    d = disc(w, h, (spot.cx - spot.col * cw) * s, (spot.cy - spot.row * ch) * s, R, pad=0.0)
    n = np.stack([d.nx, d.ny, d.nz])
    noise = Noise(stage.seed * 5 + 2)
    rnd = np.random.default_rng(stage.seed * 3 + 8)
    mu = terra.sun @ n
    day = smoothstep(-0.05, 0.3, mu)
    bg, fg, primary = ink(stage, "background"), ink(stage, "foreground"), ink(stage, "primary")
    moon = blend(bg, fg, 0.2) if stage.dark else blend(fg, bg, 0.35)
    bands = 0.5 + 0.5 * np.sin(np.arcsin(np.clip(d.ny, -1, 1)) * 5.0)
    storms = [(float(rnd.uniform(-0.75, -0.1)), float(rnd.uniform(-0.6, 0.6)), int(rnd.integers(0, frames)))
              for _ in range(3)]
    out: list[bytes] = []
    for f in range(frames):
        ang = TAU * f / frames
        ox, oy = 0.22 * math.cos(ang), 0.22 * math.sin(ang)
        c = noise.fbm(n[0] * 2.3 + ox, n[1] * 3.2 + oy, n[2] * 2.3 - ox, octaves=5)
        wisp = noise.fbm(n[0] * 7 - oy, n[1] * 9 + ox, n[2] * 7, octaves=3)
        dens = smoothstep(0.52, 0.72, c * 0.85 + 0.15 * bands + 0.12 * (wisp - 0.5))
        colour = blend(moon[None, :] * np.ones((dens.size, 1), F32), blend(WHITE, fg, 0.15), day)
        alpha = dens * (0.2 + 0.68 * day) * d.cover * (0.5 + 0.5 * np.clip(d.nz, 0, 1) ** 0.5)
        paint = Paint(w, h, stage.dark, margin=1.5)
        rgb, a = paint.rgb.reshape(-1, 3), paint.a.reshape(-1)
        rgb[d.inside] = colour * alpha[:, None]
        a[d.inside] = alpha
        for sx, sy, at in storms:  # lightning: two frames of a soft flash inside a cloud
            k = (f - at) % frames
            if k < 2:
                dx, dy = d.nx - sx, d.ny - sy
                flash = np.exp(-(dx * dx + dy * dy) / 0.0004) * (0.8 if k == 0 else 0.3) * smoothstep(0.0, -0.1, mu)
                flash = flash * dens
                rgb[d.inside] += blend(WHITE, primary, 0.3) * (flash * 0.7)[:, None]
                a[d.inside] = 1 - (1 - a[d.inside]) * (1 - np.clip(flash * 0.7, 0, 1))
        out.append(paint.frame())
    return Sprite(out, w, h, [LOOP_GAP], True, (spot.cols, spot.rows), offset=spot.offset(stage), name="clouds")


def day_edges(days: int = 7) -> list[float]:
    """Longitudes (degrees) between the days on the night side, Monday at the left limb: evenly spaced on
    screen, not on the sphere, so every day gets the same width."""
    lo, hi = -0.88, 0.12
    return [math.degrees(math.asin(lo + (hi - lo) * k / days)) for k in range(days + 1)]


# ============================================================== the halo ==


@dataclass(frozen=True)
class Eclipse:
    """A planet in front of its sun, ringed by the week's orbits: its centre and radius in terminal pixels,
    the sun (view space, behind and above right), and the orbits' half-heights in planet radii, the
    ratio of their widths to their heights and their tilt (radians)."""

    cx: float
    cy: float
    radius: float
    sun: Array
    orbits: tuple[float, ...] = (1.32, 1.6, 1.88, 2.16, 2.44)
    ratio: float = 1.42
    tilt: float = -0.12

    @classmethod
    def on(cls, stage: Stage) -> Eclipse:
        cx, cy = world_centre(stage)
        radius = min(0.178 * stage.height, 0.95 * stage.width / (2 * 2.44 * 1.42 + 0.3))
        return cls(cx, cy, radius, unit(0.42, 0.34, -0.84))

    def orbit_point(self, k: int, angle: float) -> tuple[float, float]:
        """A point of orbit ``k`` at ``angle`` (radians, 0 at the right, clockwise on screen)."""
        b = self.orbits[k] * self.radius
        x, y = self.ratio * b * math.cos(angle), b * math.sin(angle)
        c, s = math.cos(self.tilt), math.sin(self.tilt)
        return self.cx + x * c - y * s, self.cy + x * s + y * c


def _eclipse_frame(stage: Stage, ec: Eclipse, reach: float, max_px: int) -> tuple[Place, int, int, float, float,
                                                                                    float]:
    spot = place(stage, ec.cx, ec.cy, reach, reach)
    w, h, s = stretched_size(stage, spot.cols, spot.rows, max_px)
    cw, ch = stage.cell
    return spot, w, h, s, (spot.cx - spot.col * cw) * s, (spot.cy - spot.row * ch) * s


def eclipse_base(stage: Stage, ec: Eclipse, max_px: int = STILL_PX) -> Sprite:
    """The planet, a still: a dark disc with the faintest night-side land, a thin crescent of light round
    the limb towards the sun, the blue of its atmosphere all round and a thin line of airglow above it."""
    spot, w, h, s, ox, oy = _eclipse_frame(stage, ec, ec.radius * 1.12, max_px)
    R = ec.radius * s
    d = disc(w, h, ox, oy, R, pad=0.1)
    n = np.stack([d.nx, d.ny, d.nz])
    land, rough, _ = _terra_land(stage, rot_x(0.4).T @ n)
    bg, fg = ink(stage, "background"), ink(stage, "foreground")
    primary, success = ink(stage, "primary"), ink(stage, "success")
    if stage.dark:
        body_c = blend(bg * 0.55, blend(bg, fg, 0.1), land * 0.6 + 0.2 * rough)
    else:
        body_c = blend(fg * 0.9, blend(fg, bg, 0.2), land * 0.6 + 0.2 * rough)
    sx, sy = float(ec.sun[0]), float(ec.sun[1])
    toward = (d.nx * sx + d.ny * sy) / max(1e-6, math.hypot(sx, sy))
    crescent = np.exp(-(1 - np.minimum(d.r, 1)) / 0.012) * smoothstep(0.2, 0.95, toward) * (d.r <= 1)
    hot = WHITE if stage.dark else blend(primary, WHITE, 0.2)
    sky = blend(primary, WHITE, 0.3)
    c = blend(body_c, hot, np.clip(crescent * 1.2, 0, 1))
    inner = np.exp(-(1 - np.minimum(d.r, 1)) / 0.04) * (d.r <= 1)
    c = blend(c, sky, inner * (0.25 + 0.4 * smoothstep(-0.2, 1, toward)))
    over = np.maximum(d.r - 1, 0)
    atmo = np.exp(-over / 0.018) * (d.r > 1) * (0.35 + 0.65 * smoothstep(-0.5, 1.0, toward))
    air = np.exp(-(((d.r - 1.035) / 0.006) ** 2)) * 0.5
    fade = smoothstep(1.1, 1.06, d.r)
    paint = Paint(w, h, stage.dark, margin=1.5, fine=True)
    rgb, a = paint.rgb.reshape(-1, 3), paint.a.reshape(-1)
    body = d.cover
    glow_a = np.clip((atmo + air) * fade, 0, 1) * (1 - body)
    glow_c = (sky * atmo[:, None] + blend(success, WHITE, 0.2) * air[:, None]) / np.maximum(atmo + air, 1e-4)[:, None]
    rgb[d.inside] = c * body[:, None] + glow_c * glow_a[:, None]
    a[d.inside] = np.clip(body + glow_a, 0, 1)
    return Sprite([paint.frame()], w, h, [1000], False, (spot.cols, spot.rows), offset=spot.offset(stage),
                  name="eclipse")


def corona_loop(stage: Stage, ec: Eclipse, frames: int = LOOP_FRAMES, max_px: int = LOOP_PX) -> Sprite:
    """The sun's corona round the planet, a loop the terminal plays: streamers that shimmer (noise turned
    in a circle, so the loop is seamless), brightest towards the sun, and the diamond where the sun
    peeks past the limb, breathing."""
    reach = ec.radius * 2.05
    spot, w, h, s, ox, oy = _eclipse_frame(stage, ec, reach, max_px)
    R = ec.radius * s
    paint0 = Paint(w, h, stage.dark)
    dx, dy = paint0.xs - ox, oy - paint0.ys
    r = np.sqrt(dx * dx + dy * dy) / R
    phi = np.arctan2(dy, dx)
    sx, sy = float(ec.sun[0]), float(ec.sun[1])
    sun_phi = math.atan2(sy, sx)
    toward = 0.5 + 0.5 * np.cos(phi - sun_phi)
    noise = Noise(stage.seed * 19 + 4)
    primary, secondary, accent = ink(stage, "primary"), ink(stage, "secondary"), ink(stage, "accent")
    inner = blend(blend(primary, WHITE, 0.55), accent, 0.12)
    outer = blend(primary, secondary, 0.6)
    edge = smoothstep(reach / ec.radius - 0.02, reach / ec.radius * 0.72, r)
    lim_x, lim_y = ox + R * math.cos(sun_phi) * 1.0, oy - R * math.sin(sun_phi) * 1.0
    out: list[bytes] = []
    for f in range(frames):
        ang = TAU * f / frames
        ca, sa = 0.35 * math.cos(ang), 0.35 * math.sin(ang)
        streak = noise.fbm(np.cos(phi) * 3.0 + ca, np.sin(phi) * 3.0 + sa, r * 0.7, octaves=4)
        rays = smoothstep(0.35, 0.8, streak) ** 1.5
        base = np.exp(-(r - 1) / (0.18 + 0.22 * toward)) * (r > 0.98)
        k = base * (0.25 + 0.75 * toward) * (0.55 + 0.9 * rays) * edge
        paint = Paint(w, h, stage.dark, margin=1.5)
        colour = blend(outer, inner, np.clip(np.exp(-(r - 1) / 0.25), 0, 1))
        if stage.dark:
            paint.add(colour * k[..., None], 1.0, cover=np.clip(k, 0, 1))
        else:
            paint.over(colour, np.clip(k, 0, 1) * 0.85)
        breathe = 0.85 + 0.15 * math.cos(ang)
        dia = star(paint.xs, paint.ys, lim_x, lim_y, 0.025 * R + 0.4, 0.5 * R * breathe, 0.85 * R, angle=0.35,
                   arms=6)
        paint.shine(WHITE if stage.dark else inner * 0.8, dia * 1.1 * breathe * edge)
        out.append(paint.frame())
    return Sprite(out, w, h, [LOOP_GAP], True, (spot.cols, spot.rows), offset=spot.offset(stage), name="corona")


#: The orbits' kinds, inside out.
ORBIT_KINDS = ("ticket", "review", "merged", "deploy", "release")


def orbit_lines(stage: Stage, ec: Eclipse, colours: dict[str, Array], days: int = 7,
                max_px: int = ORBITS_PX) -> Sprite:
    """The week's orbits, a still: one dashed ellipse per kind, in its colour, and a tick across them at
    each day's start, Monday at the top, going clockwise. Drawn at the terminal's own pixels, or, when
    that would be more than ``max_px`` (a big stage on a Retina screen), a little smaller and stretched
    over whole cells."""
    b_out = ec.orbits[-1] * ec.radius
    a_out = b_out * ec.ratio
    half_w = abs(a_out * math.cos(ec.tilt)) + abs(b_out * math.sin(ec.tilt)) + 6
    half_h = abs(a_out * math.sin(ec.tilt)) + abs(b_out * math.cos(ec.tilt)) + 6
    w, h = math.ceil(2 * half_w), math.ceil(2 * half_h)
    spot: Place | None = None
    sc = 1.0
    if w * h > max_px:
        spot = place(stage, ec.cx, ec.cy, half_w, half_h)
        w, h, sc = stretched_size(stage, spot.cols, spot.rows, max_px)
        cw, ch = stage.cell
        cx, cy = (ec.cx - spot.col * cw) * sc, (ec.cy - spot.row * ch) * sc
    else:
        cx, cy = w / 2, h / 2
    paint = Paint(w, h, stage.dark, margin=2.0)
    dx, dy = (paint.xs - cx) / sc, (paint.ys - cy) / sc  # in terminal pixels from the centre
    c, s = math.cos(ec.tilt), math.sin(ec.tilt)
    x, y = dx * c + dy * s, -dx * s + dy * c
    fg = ink(stage, "foreground")
    line = max(0.7, 0.055 * stage.cell[1], 0.75 / sc)
    for k, kind in enumerate(ORBIT_KINDS):
        b = ec.orbits[k] * ec.radius
        q = np.sqrt((x / (ec.ratio * b)) ** 2 + (y / b) ** 2)
        dist = np.abs(q - 1) * b
        ang = np.arctan2(y / b, x / (ec.ratio * b))
        dashes = smoothstep(0.25, 0.45, 0.5 + 0.5 * np.cos(ang * (60 + 12 * k)))
        stroke = np.clip((line - dist) * sc + 0.5, 0, 1) * (0.35 + 0.65 * dashes)  # half an image pixel of edge
        paint.shine(colours[kind], stroke * (0.42 if stage.dark else 0.55))
    # day ticks: across the orbits at each day's start
    for e in range(days):
        a = week_angle(e * 24.0)
        ux, uy = math.cos(a), math.sin(a)
        b0, b1 = ec.orbits[0] * ec.radius * 0.94, ec.orbits[-1] * ec.radius * 1.04
        qb = np.sqrt((x / ec.ratio) ** 2 + y * y)  # how far out, in orbit heights
        rel = (np.arctan2(y, x / ec.ratio) - math.atan2(uy, ux) + math.pi) % TAU - math.pi
        across = np.abs(rel) * qb * (1 + (ec.ratio - 1) * abs(uy))
        tick = np.clip((line * 0.8 - across) * sc + 0.5, 0, 1) * smoothstep(b0, b0 + 4, qb) * smoothstep(b1, b1 - 4, qb)
        paint.shine(fg, tick * (0.32 if e == 0 else 0.16) * (1 if stage.dark else 0.7))
    if spot is None:
        return Sprite([paint.frame()], w, h, [1000], False, name="orbits")
    return Sprite([paint.frame()], w, h, [1000], False, (spot.cols, spot.rows), offset=spot.offset(stage),
                  name="orbits")


def week_angle(hour: float) -> float:
    """Where in the week a moment is, as an angle round the orbits: Monday 00:00 at the top, clockwise."""
    return -math.pi / 2 + TAU * (hour % 168.0) / 168.0


__all__ = ["ORBIT_KINDS", "Eclipse", "Giant", "LOOP_FRAMES", "LOOP_GAP", "Place", "Terra", "berth_edges",
           "corona_loop", "day_edges", "eclipse_base", "giant_loop", "giant_ring", "orbit_lines", "place", "terra_base",
           "terra_clouds", "week_angle", "world_centre"]
