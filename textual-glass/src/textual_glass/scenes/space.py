"""The week in review: a planet, and a ship for each piece of work done this
week. Pure numpy, no Textual imports. Exploratory art for one scene, in three
looks to choose from.

Each ship warps in, in the order the work happened, flies to its place and
lands or docks there, and leaves a light behind. When the show ends the page
holds its last frame, the summary: one light per piece of work, its shape
and colour telling the kind (``KINDS``), its place telling when it happened.
Only the world's own loop moves after that (the terminal plays it), so glass
sends nothing (rule 9).

* ``harbour``: a banded giant whose bands flow under a still ring. Haulers
  dock on the docking rail round the ring, cut into the seven days, Monday at
  the left. A release becomes a new moon.
* ``nightfall``: a world at night. Landers come down through the atmosphere
  and a town lights up where each lands: the day is its longitude (Monday at
  the left limb, the weekend at the dawn line), the kind its latitude. A
  release is a capital in the north, with a beam of light.
* ``halo``: an eclipsed planet ringed by one orbit per kind. Probes park on
  their kind's orbit at the time of the week the work happened (Monday at the
  top, going clockwise) and unfold into lights. A release flares the corona.

``make(stage, week, look)`` returns the :class:`~textual_glass.scenes.Scene`;
an app plays it with ``lambda stage: make(stage, week, "halo")``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from ..flourishes._draw import bump, ease_out, ramp
from ..render import Array, smoothstep
from . import Actor, Cue, Route, Scene, Stage
from ._effects import WARP_LEAD, arrive, final_light, folded_ship, glyph, glyph_size, light_on, warp_in, warp_spot
from ._paint import WHITE, Noise, Paint, Sprite, blend, disc, ink, star, still_sprite, stretched_size
from ._ships import ship_sprite, standard_length
from ._worlds import (
    ORBIT_KINDS,
    Eclipse,
    Giant,
    Terra,
    corona_loop,
    day_edges,
    eclipse_base,
    giant_loop,
    giant_ring,
    orbit_lines,
    place,
    terra_base,
    terra_clouds,
    week_angle,
)

#: The kinds of work, and the palette role each one's light is drawn in.
KINDS = ("merged", "review", "deploy", "ticket", "release")
ROLE = {"merged": "secondary", "review": "success", "deploy": "primary", "ticket": "foreground", "release": "accent"}
LOOKS = ("harbour", "nightfall", "halo")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

Point = tuple[float, float]


@dataclass(frozen=True)
class Work:
    """One piece of work: its kind, and when it happened, in hours since Monday 00:00 (0 to 168)."""

    kind: str
    hour: float

    @property
    def day(self) -> int:
        return min(6, max(0, int(self.hour // 24)))


def sample_week(count: int = 25, seed: int = 1, releases: int = 1) -> list[Work]:
    """A made-up week: mostly weekdays and office hours, a release late in the week, in order of time."""
    rnd = np.random.default_rng(seed)
    n = max(0, count - releases)
    days = rnd.choice(7, size=n, p=[0.2, 0.22, 0.21, 0.19, 0.15, 0.02, 0.01])
    hours = days * 24 + np.clip(rnd.normal(13.5, 2.8, n), 8, 21.5)
    kinds = rnd.choice(["merged", "review", "deploy", "ticket"], size=n, p=[0.3, 0.26, 0.2, 0.24])
    week = [Work(str(k), float(h)) for k, h in zip(kinds, hours, strict=True)]
    for r in range(releases):
        week.append(Work("release", float((3 + r % 2) * 24 + rnd.uniform(14, 17.5))))
    return sorted(week, key=lambda w: w.hour)


def mini_week(seed: int = 2) -> list[Work]:
    """Twelve pieces of work with one release: a short show for trying a look."""
    return sample_week(12, seed=seed, releases=1)


# ================================================================ slots ==


@dataclass(frozen=True)
class Slot:
    """Where one piece of work ends up: its light, where its ship stops and where it warps in (stage
    pixels), and how its light is foreshortened (ratio and angle; a town on a sphere's slope)."""

    light: Point
    dock: Point
    start: Point
    squash: tuple[float, float] = (1.0, 0.0)


def _launch(stage: Stage, target: Point, away: float, rnd: np.random.Generator, spread: float = 0.5,
            reach: float = 0.42, avoid: Callable[[Point], bool] | None = None) -> Point:
    """Where a ship bound for ``target`` warps in: about ``away`` (radians) from it, as far as the stage
    allows (within ``reach`` of its width), never on the stage's edge, and not where ``avoid`` says."""
    margin = 1.4 * standard_length(stage)
    best, best_d = (target[0] - margin, target[1]), -1.0
    for k in range(12):
        a = away + (rnd.normal(0, spread) if k else 0.0)
        c, s = math.cos(a), math.sin(a)
        lim = []
        if c > 1e-6:
            lim.append((stage.width - margin - target[0]) / c)
        if c < -1e-6:
            lim.append((margin - target[0]) / c)
        if s > 1e-6:
            lim.append((stage.height - margin - target[1]) / s)
        if s < -1e-6:
            lim.append((margin - target[1]) / s)
        far = max(0.0, min(lim + [reach * stage.width]))
        d = far * float(rnd.uniform(0.8, 1.0))
        p = (target[0] + c * d, target[1] + s * d)
        if avoid is not None and avoid(p):
            continue
        if d > best_d * 1.15 or (best_d < 0.2 * stage.width and d > best_d):
            best, best_d = p, d
    return best


def harbour_slots(stage: Stage, giant: Giant, week: Sequence[Work], seed: int = 0) -> list[Slot]:
    """Berths on the docking rail: each day's work in its day's stretch of the rail, in order, left to
    right; a second lane outside when a day is busy. A release goes to a moon's place above the ring."""
    rnd = np.random.default_rng(seed * 101 + 7)
    per_day: dict[int, list[int]] = {}
    for i, w in enumerate(week):
        if w.kind != "release":
            per_day.setdefault(w.day, []).append(i)
    cap = 6
    out: dict[int, Slot] = {}
    for d, items in per_day.items():
        lanes = 1 if len(items) <= cap else 2
        per_lane = math.ceil(len(items) / lanes)
        step = 1 / 7 / (cap + 0.6)
        for j, i in enumerate(items):
            lane, k = (j % lanes, j // lanes) if lanes > 1 else (0, j)
            phi = giant.rail_angle((d + 0.5) / 7 + (k - (per_lane - 1) / 2) * step)
            r = giant.rail + 0.13 * lane
            x, y, _ = giant.ring_point(r, phi)
            away = math.atan2(y - giant.cy, x - giant.cx)
            out[i] = Slot((x, y), (x, y), _launch(stage, (x, y), away, rnd, 0.55))
    moons = [(-1.78, -0.98), (1.86, -1.02), (-2.0, -0.42), (2.05, -0.5)]
    m = 0
    for i, w in enumerate(week):
        if w.kind == "release":
            mx, my = moons[m % len(moons)]
            p = (giant.cx + mx * giant.radius, giant.cy + my * giant.radius)
            out[i] = Slot(p, p, _launch(stage, p, math.atan2(my, mx), rnd, 0.35))
            m += 1
    return [out[i] for i in range(len(week))]


NIGHT_LATITUDES = {"release": 54.0, "merged": 34.0, "review": 13.0, "deploy": -8.0, "ticket": -29.0}
_CLUSTER = ((0.0, 0.0), (0.55, 0.5), (-0.55, -0.45), (0.5, -0.55), (-0.5, 0.55), (0.0, 0.95), (0.0, -0.95),
            (0.95, 0.0), (-0.95, 0.0))


def nightfall_slots(stage: Stage, terra: Terra, week: Sequence[Work], seed: int = 0) -> list[Slot]:
    """Towns on the night side: the day is the column (Monday at the left limb), the kind the row; a
    busy day's towns of one kind gather round each other. Landers come in from the night side."""
    rnd = np.random.default_rng(seed * 103 + 5)
    lo, hi = -0.88, 0.12  # the columns, in sin(longitude): even on screen
    col_w = (hi - lo) / 7
    count: dict[tuple[int, str], int] = {}
    out = []
    for w in week:
        k = count.get((w.day, w.kind), 0)
        count[(w.day, w.kind)] = k + 1
        ox, oy = _CLUSTER[k % len(_CLUSTER)]
        lat = NIGHT_LATITUDES[w.kind] + oy * 7.5 + float(rnd.uniform(-1.5, 1.5))
        s = lo + col_w * (w.day + 0.5 + ox * 0.3) + float(rnd.uniform(-0.01, 0.01))
        lon = math.degrees(math.asin(max(-0.99, min(0.99, s))))
        x, y, n = terra.point(lat, lon)
        squash = (max(0.3, float(n[2])), math.atan2(-float(n[1]), float(n[0])))

        def on_day_side(p: Point) -> bool:
            return p[0] > terra.cx + 0.2 * terra.radius and abs(p[1] - terra.cy) < 1.05 * terra.radius

        away = math.pi + float(rnd.uniform(-0.75, 0.75)) + (0.35 if lat < 0 else -0.35)
        out.append(Slot((x, y), (x, y), _launch(stage, (x, y), away, rnd, 0.4, avoid=on_day_side), squash))
    return out


def halo_slots(stage: Stage, ec: Eclipse, week: Sequence[Work], seed: int = 0) -> list[Slot]:
    """Places on the orbits: the kind's orbit, at the time of the week (Monday at the top, clockwise),
    nudged apart where two would touch. Probes come in from outside, a little behind their place."""
    rnd = np.random.default_rng(seed * 107 + 3)
    angles = [week_angle(w.hour) for w in week]
    size = glyph_size(stage)
    for k, kind in enumerate(ORBIT_KINDS):
        idx = sorted((i for i, w in enumerate(week) if w.kind == kind), key=lambda i: angles[i])
        if len(idx) < 2:
            continue
        b = ec.orbits[k] * ec.radius
        gap = 2.7 * size / (b * (1 + ec.ratio) / 2)
        for _ in range(40):  # push neighbours apart until no two touch
            moved = False
            for a, c in zip(idx, idx[1:], strict=False):
                d = angles[c] - angles[a]
                if d < gap:
                    push = (gap - d) / 2
                    angles[a] -= push
                    angles[c] += push
                    moved = True
            if not moved:
                break
    out = []
    for i, w in enumerate(week):
        k = ORBIT_KINDS.index(w.kind)
        x, y = ec.orbit_point(k, angles[i])
        away = math.atan2(y - ec.cy, x - ec.cx) - 0.55
        out.append(Slot((x, y), (x, y), _launch(stage, (x, y), away, rnd, 0.3)))
    return out


# ======================================================= release moments ==


def moon_sprite(stage: Stage, giant: Giant, colour: Array, seed: int) -> Sprite:
    """A new moon for a release: a white flash, a ring, and a small golden moon lit by the giant's sun
    fades out of it. Its last frame is the moon, which stays."""
    radius = 0.62 * stage.cell[1] * (1 + 0.15 * (seed % 3))
    reach = radius * 3.4
    side = math.ceil(2 * reach + 4)
    w = h = side + side % 2
    cx = cy = w / 2
    d = disc(w, h, cx, cy, radius, pad=0.0)
    n = np.stack([d.nx, d.ny, d.nz])
    noise = Noise(seed * 13 + 2, n=16)
    craters = noise.fbm(n[0] * 3 + seed, n[1] * 3, n[2] * 3, octaves=4)
    body_c = blend(colour, WHITE, 0.3) * (0.82 + 0.4 * (craters[:, None] - 0.5))
    mu = giant.sun @ n
    lit = smoothstep(-0.15, 0.35, mu) * (0.5 + 0.5 * np.clip(mu, 0, 1))
    shade = (0.28 if stage.dark else 0.5) + 0.8 * lit
    times = [0.0, 0.05, 0.1, 0.16, 0.23, 0.31, 0.4, 0.5, 0.62, 0.76, 0.92]
    out = []
    for t in times:
        e = t / times[-1]
        paint = Paint(w, h, stage.dark, margin=2.0)
        r = np.sqrt((paint.xs - cx) ** 2 + (paint.ys - cy) ** 2)
        flash = bump(e, 0.12, 0.55)
        hot = WHITE if stage.dark else colour * 0.8
        paint.shine(colour, soft(r, reach - 2) * 0.35 * (0.4 + 0.6 * flash))
        paint.shine(hot, star(paint.xs, paint.ys, cx, cy, 0.3 * radius, 2.6 * radius * flash + 1, reach - 1)
                    * 1.5 * flash)
        ring_r = radius * (1.1 + 1.9 * ease_out(e))
        ring = np.exp(-(((r - ring_r) / (0.12 * radius + 0.6)) ** 2)) * (1 - ramp(0.3, 1.0, e))
        paint.shine(colour, ring * 0.9 * smoothstep(reach, reach - 3, r))
        show = ramp(0.25, 0.7, e)
        if show > 0:
            rgb, a = paint.rgb.reshape(-1, 3), paint.a.reshape(-1)
            cov = d.cover * show
            c = body_c * shade[:, None] + blend(colour, WHITE, 0.6) * (flash * 0.6)
            rgb[d.inside] = c * cov[:, None] + rgb[d.inside] * (1 - cov[:, None])
            a[d.inside] = cov + a[d.inside] * (1 - cov)
        paint.shine(colour, np.exp(-np.abs(r - radius * 1.05) / (0.08 * radius + 0.5)) * 0.7 * show
                    * smoothstep(radius * 1.6, radius, r))
        paint.shine(colour, soft(r, 2.6 * radius) * 0.4 * show * (r > radius * 0.98))
        out.append(paint.frame())
    gaps = [max(16, round((b - a) * 1000)) for a, b in zip(times, times[1:], strict=False)] + [1000]
    return Sprite(out, w, h, gaps, False, name="moon")


def soft(r: Array, radius: float) -> Array:
    q = r / max(1e-6, radius)
    return np.exp(-4.0 * q * q) * smoothstep(1.0, 0.6, q)


def rail_wave(stage: Stage, giant: Giant, colour: Array, frames: int = 16, max_px: int = 60_000) -> Sprite:
    """For a release: a pulse of light runs the docking rail from Monday to Sunday, the whole week
    lighting up behind it, and fades. Soft, so it is drawn small and stretched. Ends empty."""
    phis = np.linspace(0, math.pi, 48)
    pts = [giant.ring_point(giant.rail + dr, a) for dr in (-0.1, 0.25) for a in phis]
    xs_, ys_ = [x for x, _, _ in pts], [y for _, y, _ in pts]
    cw, ch = stage.cell
    col0, row0 = max(0, math.floor((min(xs_) - 8) / cw)), max(0, math.floor((min(ys_) - 8) / ch))
    col1, row1 = min(stage.cols, math.ceil((max(xs_) + 8) / cw)), min(stage.rows, math.ceil((max(ys_) + 8) / ch))
    cols, rows = col1 - col0, row1 - row0
    w, h, s = stretched_size(stage, cols, rows, max_px)
    paint0 = Paint(w, h, stage.dark)
    X = (paint0.xs / s + col0 * cw - giant.cx) / giant.radius
    Y = (giant.cy - (paint0.ys / s + row0 * ch)) / giant.radius
    nrm = giant.normal
    Z = -(nrm[0] * X + nrm[1] * Y) / nrm[2]
    X, Y = np.broadcast_to(X, Z.shape), np.broadcast_to(Y, Z.shape)
    r = np.sqrt(X * X + Y * Y + Z * Z)
    u, wv = giant.axis[:, 0], giant.axis[:, 2]
    phi = np.arctan2(X * wv[0] + Y * wv[1] + Z * wv[2], X * u[0] + Y * u[1] + Z * u[2])
    front = Z > 0
    band = np.exp(-(((r - giant.rail) / 0.05) ** 2)) * front
    lo, hi = giant.ring
    sheet = smoothstep(lo, lo + 0.1, r) * smoothstep(giant.rail + 0.06, hi, r) * front
    edges = giant.berth_edges(7)
    out = []
    for f in range(frames):
        e = f / (frames - 1)
        head = edges[0] + (edges[-1] - edges[0]) * ease_out(min(1.0, e / 0.7))
        behind = smoothstep(head - 0.05, head + 0.05, phi) * (phi < edges[0] + 0.05)
        pulse = np.exp(-(((phi - head) / 0.09) ** 2))
        paint = Paint(w, h, stage.dark, margin=1.0)
        paint.shine(blend(colour, WHITE, 0.3), band * pulse * 2.6 * (1 - ramp(0.75, 1.0, e)))
        paint.shine(colour, band * behind * 0.9 * (1 - ramp(0.45, 1.0, e)))
        wide = np.exp(-(((phi - head) / 0.22) ** 2))  # the ring itself catches the light as it passes
        paint.shine(colour, sheet * wide * 0.55 * (1 - ramp(0.7, 1.0, e)))
        out.append(paint.frame())
    return Sprite(out, w, h, [70], False, (cols, rows),
                  offset=(giant.cx - (col0 + cols / 2) * cw, giant.cy - (row0 + rows / 2) * ch), name="rail-wave")


def beam(stage: Stage, x: float, y: float, colour: Array, frames: int = 16, max_px: int = 30_000) -> Sprite:
    """For a release on the night side: a pillar of light shoots from the town up out of the
    atmosphere, holds and fades from the ground up. Ends empty."""
    cw, ch = stage.cell
    col0, col1 = max(0, math.floor(x / cw) - 3), min(stage.cols, math.floor(x / cw) + 4)
    row1 = min(stage.rows, math.ceil(y / ch) + 2)
    cols, rows = col1 - col0, row1
    w, h, s = stretched_size(stage, cols, rows, max_px)
    paint0 = Paint(w, h, stage.dark)
    X, Y = paint0.xs / s + col0 * cw, paint0.ys / s
    out = []
    for f in range(frames):
        e = f / (frames - 1)
        top = y - (y + ch) * ease_out(min(1.0, e / 0.35))
        floor = y - (y + ch) * ramp(0.55, 1.0, e)
        width = 0.11 * ch * (1 + 2.5 * bump(e, 0.15, 0.6))
        across = np.abs(X - x)
        body = np.exp(-((across / width) ** 2)) * smoothstep(top - 2, top + ch, Y) * smoothstep(y + 2, y - 2, Y)
        body = body * smoothstep(floor + 2 * ch, floor - ch, Y) if e > 0.55 else body
        glow = np.exp(-across / (0.9 * ch)) * smoothstep(top, top + 3 * ch, Y) * smoothstep(y + 2, y - ch, Y) * 0.25
        base = np.exp(-(((X - x) ** 2 + ((Y - y) * 2.2) ** 2) / (1.4 * ch) ** 2)) * bump(e, 0.08, 0.6)
        paint = Paint(w, h, stage.dark, margin=1.0)
        paint.shine(colour, (glow + base * 0.8) * (1 - ramp(0.7, 1.0, e)))
        paint.shine(blend(colour, WHITE, 0.6), body * 1.3 * (1 - ramp(0.8, 1.0, e)))
        out.append(paint.frame())
    return Sprite(out, w, h, [70], False, (cols, rows), offset=(x - (col0 + cols / 2) * cw, y - rows / 2 * ch),
                  name="beam")


def sky_flash(stage: Stage, cx: float, cy: float, radius: float, colour: Array, frames: int = 14,
              max_px: int = 60_000) -> Sprite:
    """For a release: the whole atmosphere (or the corona) blazes in the release's colour and settles. A
    ring of light runs out from the limb. Ends empty."""
    reach = radius * 1.75
    spot = place(stage, cx, cy, reach, reach)
    w, h, s = stretched_size(stage, spot.cols, spot.rows, max_px)
    cw, ch = stage.cell
    ox, oy = (cx - spot.col * cw) * s, (cy - spot.row * ch) * s
    paint0 = Paint(w, h, stage.dark)
    r = np.sqrt((paint0.xs - ox) ** 2 + (paint0.ys - oy) ** 2) / (radius * s)
    edge = smoothstep(reach / radius - 0.02, reach / radius - 0.25, r)
    out = []
    for f in range(frames):
        e = f / (frames - 1)
        blaze = bump(e, 0.15, 1.0)
        rim = np.exp(-np.abs(r - 1.0) / 0.03) * (r > 0.97)
        wave_r = 1.0 + 0.7 * ease_out(e)
        wave = np.exp(-(((r - wave_r) / 0.04) ** 2)) * (1 - ramp(0.4, 1.0, e))
        paint = Paint(w, h, stage.dark, margin=1.0)
        paint.shine(colour, (rim * 1.4 * blaze + np.exp(-(r - 1) / 0.15) * (r > 1) * 0.5 * blaze) * edge)
        paint.shine(blend(colour, WHITE, 0.4), wave * 0.9 * edge)
        out.append(paint.frame())
    return Sprite(out, w, h, [80], False, (spot.cols, spot.rows), offset=spot.offset(stage), name="sky-flash")


def diamond_flare(stage: Stage, ec: Eclipse, colour: Array, frames: int = 18, max_px: int = 60_000) -> Sprite:
    """For a release on the halo: the diamond where the sun peeks past the limb blazes into a long-spiked
    star, and a ring of light the shape of the orbits sweeps out across them. Ends empty."""
    b_out = ec.orbits[-1] * ec.radius * 1.08
    reach_x, reach_y = b_out * ec.ratio + 10, b_out + 10
    spot = place(stage, ec.cx, ec.cy, reach_x, reach_y)
    w, h, s = stretched_size(stage, spot.cols, spot.rows, max_px)
    cw, ch = stage.cell
    ox, oy = (ec.cx - spot.col * cw) * s, (ec.cy - spot.row * ch) * s
    paint0 = Paint(w, h, stage.dark)
    dx, dy = paint0.xs - ox, paint0.ys - oy
    c, sn = math.cos(ec.tilt), math.sin(ec.tilt)
    x, y = dx * c + dy * sn, -dx * sn + dy * c
    q = np.sqrt((x / ec.ratio) ** 2 + y * y) / (ec.radius * s)  # in orbit heights, planet radii
    sun_phi = math.atan2(float(ec.sun[1]), float(ec.sun[0]))
    lx, ly = ox + ec.radius * s * math.cos(sun_phi), oy - ec.radius * s * math.sin(sun_phi)
    R = ec.radius * s
    edge = smoothstep(ec.orbits[-1] * 1.08, ec.orbits[-1] * 0.95, q)
    hot = WHITE if stage.dark else colour * 0.8
    out = []
    for f in range(frames):
        e = f / (frames - 1)
        blaze = bump(e, 0.12, 0.8)
        paint = Paint(w, h, stage.dark, margin=1.0)
        spikes = star(paint.xs, paint.ys, lx, ly, 0.06 * R, 2.2 * R * blaze + 1, 3.4 * R, angle=0.35, arms=6)
        paint.shine(hot, spikes * 1.6 * blaze * edge)
        paint.shine(colour, soft(np.sqrt((paint.xs - lx) ** 2 + (paint.ys - ly) ** 2), 1.4 * R) * 0.8 * blaze)
        wave_q = 1.0 + (ec.orbits[-1] - 0.95) * ease_out(min(1.0, e / 0.85))
        wave = np.exp(-(((q - wave_q) / 0.05) ** 2)) * (1 - ramp(0.55, 1.0, e)) * (q > 1.0)
        paint.shine(blend(colour, WHITE, 0.3), wave * 0.8 * edge)
        out.append(paint.frame())
    return Sprite(out, w, h, [70], False, (spot.cols, spot.rows), offset=spot.offset(stage), name="diamond-flare")


# ================================================================= looks ==


@dataclass(frozen=True)
class Style:
    """How a look's ships fly and land: their family, the warp-in, the arrival and the light it leaves,
    and the sun that lights them (on screen: x right, y down, z out)."""

    family: str
    warp: str
    arrive: str
    light: str
    sun: tuple[float, float, float]
    folded: bool = False


STYLES = {
    "harbour": Style("harbour", "gate", "dock", "bead", (-0.5, -0.55, 0.67)),
    "nightfall": Style("nightfall", "streak", "land", "city", (0.75, -0.2, 0.62)),
    "halo": Style("halo", "fold", "unfold", "star", (0.35, -0.3, 0.88), folded=True),
}


def colours(stage: Stage) -> dict[str, Array]:
    return {k: ink(stage, ROLE[k]) for k in KINDS}


def _duration(sprite: Sprite) -> float:
    """How long a one-shot plays, to its last frame's end: when it can leave."""
    gaps = sprite.gaps if len(sprite.gaps) == len(sprite.frames) else sprite.gaps * len(sprite.frames)
    return sum(gaps) / 1000


def _glide(u: float) -> float:
    """A ship's progress along its flight: out of the warp fast, a long glide, a gentle stop."""
    return 1 - (1 - u) ** 2.6


@dataclass
class World:
    """A look's world on a stage: its actors, the slots for the week and what a release does."""

    actors: list[Actor]
    slots: list[Slot]
    release: Callable[[int, Slot, float, Array], list[Actor]]
    light_z: int = 20


def _harbour(stage: Stage, week: Sequence[Work], frames: int) -> World:
    giant = Giant.on(stage)
    at = Route.still(giant.cx, giant.cy)
    loop = giant_loop(stage, giant, frames=frames)
    back, front = giant_ring(stage, giant, front=False), giant_ring(stage, giant, front=True)
    if stage.still:
        loop = still_sprite(loop)
    actors = [back.actor(at, z=2), loop.actor(at, z=4), front.actor(at, z=6)]
    count = [0]

    def release(i: int, slot: Slot, t: float, colour: Array) -> list[Actor]:
        count[0] += 1
        moon = moon_sprite(stage, giant, colour, seed=stage.seed + count[0])
        if stage.still:
            return [final_light(moon).actor(Route.still(*slot.light), z=21)]
        wave = rail_wave(stage, giant, colour)
        return [moon.actor(Route.still(*slot.light), enter=t, z=21),
                wave.actor(Route.still(giant.cx, giant.cy), enter=t + 0.25, leave=t + 0.25 + _duration(wave), z=22)]

    return World(actors, harbour_slots(stage, giant, week, stage.seed), release)


def _nightfall(stage: Stage, week: Sequence[Work], frames: int) -> World:
    terra = Terra.on(stage)
    at = Route.still(terra.cx, terra.cy)
    clouds = terra_clouds(stage, terra, frames=frames)
    if stage.still:
        clouds = still_sprite(clouds)
    actors = [terra_base(stage, terra).actor(at, z=2), clouds.actor(at, z=4)]

    def release(i: int, slot: Slot, t: float, colour: Array) -> list[Actor]:
        town = light_on(stage, "release", colour, "city", seed=stage.seed * 31 + i, squash=slot.squash)
        if stage.still:
            return [final_light(town).actor(Route.still(*slot.light), z=20)]
        up = beam(stage, slot.light[0], slot.light[1], colour)
        flash = sky_flash(stage, terra.cx, terra.cy, terra.radius, colour)
        return [town.actor(Route.still(*slot.light), enter=t, z=20),
                up.actor(Route.still(*slot.light), enter=t, leave=t + _duration(up), z=24),
                flash.actor(Route.still(terra.cx, terra.cy), enter=t + 0.1, leave=t + 0.1 + _duration(flash), z=22)]

    return World(actors, nightfall_slots(stage, terra, week, stage.seed), release)


def _halo(stage: Stage, week: Sequence[Work], frames: int) -> World:
    ec = Eclipse.on(stage)
    at = Route.still(ec.cx, ec.cy)
    corona = corona_loop(stage, ec, frames=frames)
    if stage.still:
        corona = still_sprite(corona)
    actors = [corona.actor(at, z=2), eclipse_base(stage, ec).actor(at, z=4),
              orbit_lines(stage, ec, colours(stage)).actor(at, z=6)]

    def release(i: int, slot: Slot, t: float, colour: Array) -> list[Actor]:
        light = light_on(stage, "release", colour, "star", seed=stage.seed * 31 + i)
        if stage.still:
            return [final_light(light).actor(Route.still(*slot.light), z=20)]
        flare = diamond_flare(stage, ec, colour)
        return [light.actor(Route.still(*slot.light), enter=t, z=20),
                flare.actor(Route.still(ec.cx, ec.cy), enter=t - 0.15, leave=t - 0.15 + _duration(flare), z=22)]

    return World(actors, halo_slots(stage, ec, week, stage.seed), release)


WORLDS: dict[str, Callable[[Stage, Sequence[Work], int], World]] = {
    "harbour": _harbour, "nightfall": _nightfall, "halo": _halo}


def make(stage: Stage, week: Sequence[Work], look: str = "halo", length: float = 20.0,
         loop_frames: int = 32) -> Scene:
    """The week in review on ``stage``, in ``look`` (``LOOKS``): about ``length`` seconds of ships, then
    the summary holds. Under ``minimal`` motion (``stage.still``) only the summary is drawn, one frame
    per actor."""
    style = STYLES[look]
    world = WORLDS[look](stage, week, loop_frames)
    inks = colours(stage)
    actors = list(world.actors)
    cues: list[Cue] = []
    n = max(1, len(week))
    spacing = min(1.4, max(0.5, length / n))
    t = 0.9
    end = 1.0
    for i, (work, slot) in enumerate(zip(week, world.slots, strict=True)):
        colour = inks[work.kind]
        big = work.kind == "release"
        if stage.still:
            if big:
                actors += world.release(i, slot, 0.0, colour)
            else:
                light = light_on(stage, work.kind, colour, style.light, seed=stage.seed * 31 + i,
                                 squash=slot.squash)
                actors.append(final_light(light).actor(Route.still(*slot.light), z=world.light_z))
            continue
        if big:
            t += 0.8
        (dx, dy) = slot.dock
        sx, sy = warp_spot(stage, *slot.start, style.warp, big)
        heading = math.atan2(dy - sy, dx - sx)
        dist = math.hypot(dx - sx, dy - sy)
        flight = 1.35 + 1.1 * dist / stage.width + (0.6 if big else 0.0)
        warp = warp_in(stage, heading, colour, style.warp, big=big)
        actors.append(warp.actor(Route.still(sx, sy), enter=t, leave=t + _duration(warp), z=50))
        make_ship = folded_ship if style.folded else ship_sprite
        ship = make_ship(stage, style.family, work.kind, heading, colour, style.sun)
        go, land = t + WARP_LEAD, t + WARP_LEAD + flight
        actors.append(ship.actor(Route.line((sx, sy), (dx, dy), go, land, ease=_glide), enter=go, leave=land,
                                 z=41 if big else 40))
        last, light_at = arrive(stage, style.family, work.kind, heading, colour, style.arrive, style.sun,
                                squash=slot.squash)
        actors.append(last.actor(Route.still(dx, dy), enter=land, leave=land + _duration(last), z=30))
        lit = land + light_at
        if big:
            actors += world.release(i, slot, lit, colour)
        else:
            light = light_on(stage, work.kind, colour, style.light, seed=stage.seed * 31 + i, squash=slot.squash)
            actors.append(light.actor(Route.still(*slot.light), enter=lit, z=world.light_z))
        cues.append(Cue(lit, ("landed", i, work.kind)))
        end = max(end, lit + (2.0 if big else 1.0))
        t += spacing + (0.8 if big else 0.0)
    return Scene(actors, (end + 0.6) if not stage.still else 0.1, cues)


def summary_glyph(paint: Paint, kind: str, x: float, y: float, stage: Stage) -> None:
    """A kind's light at (x, y) on any canvas: for a legend next to the scene."""
    glyph(paint, kind, x, y, glyph_size(stage), colours(stage)[kind])


__all__ = ["DAYS", "KINDS", "LOOKS", "ROLE", "STYLES", "Slot", "Style", "Work", "World", "colours",
           "day_edges", "make", "mini_week", "sample_week", "summary_glyph"]
