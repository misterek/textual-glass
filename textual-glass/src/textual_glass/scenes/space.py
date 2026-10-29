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
an app plays it with ``lambda stage: make(stage, week, "halo")``. Each light
that comes on is a cue, ``("landed", i, kind)``, and the last one is ``END``.

Space is always night: on a light theme the show paints its own sky behind
everything and draws in the theme's hues lifted to glow (``_sky``). A busy
week takes about as long as a quiet one: it comes in waves, a day at a time,
its lights smaller, its ships and lights sharing pictures, and most of its
ships arriving the cheaper way (``TIERS``), so it fits the contract's limits.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from ..flourishes._draw import bump, ease_out, ramp
from ..render import Array, smoothstep
from . import MAX_ACTORS, MAX_PIXELS, Actor, Cue, Route, Scene, Stage
from ._effects import (
    _LIGHT_AT,
    FLASH_PEAK,
    LIGHT_SETTLES,
    WARP_LEAD,
    arrive,
    arrive_cost,
    folded_ship,
    glyph,
    glyph_size,
    held_light,
    light_cost,
    light_flash,
    light_on,
    warp_cost,
    warp_in,
    warp_spot,
)
from ._paint import WHITE, Noise, Paint, Sprite, blend, disc, ink, star, still_sprite, stretched_size
from ._ships import FAMILIES, ship_canvas, ship_sprite, standard_length
from ._sky import night, night_sky
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
#: A light that stays, as it is drawn: its kind, its town's seed and its slope (ratio, angle).
Light = tuple[str, int, tuple[float, float]]


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


#: Ships fly in this many headings, so the ships of a kind that go the same way share a picture.
HEADINGS = 24
#: The lights shrink to fit a busy week, down to this share of their size, and no further.
MIN_SCALE = 0.5
_GOLDEN = math.pi * (3 - math.sqrt(5))


@dataclass(frozen=True)
class Slot:
    """Where one piece of work ends up: its light, where its ship stops and where it warps in (stage
    pixels), and how its light is foreshortened (ratio and angle; a town on a sphere's slope)."""

    light: Point
    dock: Point
    start: Point
    squash: tuple[float, float] = (1.0, 0.0)


def heading_of(a: float) -> float:
    """``a`` (radians) to the nearest of the ``HEADINGS`` directions ships fly in."""
    step = 2 * math.pi / HEADINGS
    return round(a / step) % HEADINGS * step


def _launch(stage: Stage, target: Point, away: float, rnd: np.random.Generator, spread: float = 0.5,
            reach: float = 0.42, avoid: Callable[[Point], bool] | None = None) -> Point:
    """Where a ship bound for ``target`` warps in: about ``away`` (radians) from it, as far as the stage
    allows (within ``reach`` of its width), never on the stage's edge, and not where ``avoid`` says. The
    direction is one of the ``HEADINGS``, so the ship's picture can be shared."""
    margin = 1.4 * standard_length(stage)
    best, best_d = (target[0] - margin, target[1]), -1.0
    for k in range(12):
        a = heading_of(away + (rnd.normal(0, spread) if k else 0.0))
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


#: The harbour's lanes round the rail, in order of use: the rail itself, then outside, inside...
_LANES = (0, 1, -1, 2, -2, 3, -3)


def _harbour_room(giant: Giant, size: float) -> tuple[int, float]:
    """How many lights fit along one day of the rail in one lane, and the lanes' spacing in planet radii
    (the ring is seen at a slant: enough apart where it is most foreshortened, at the front)."""
    return max(1, int(giant.rail_length / 7 / (2.7 * size))), 2.3 * size / (0.45 * giant.radius)


def harbour_slots(stage: Stage, giant: Giant, week: Sequence[Work], seed: int = 0, size: float = 0.0) -> list[Slot]:
    """Berths on the docking rail: each day's work in its day's stretch of the rail, in order, left to
    right; more lanes either side of it when a day is busy, each lane half a berth along from the last.
    A release goes to a moon's place above the ring. ``size`` is a light's radius."""
    rnd = np.random.default_rng(seed * 101 + 7)
    size = size or glyph_size(stage) * 1.2
    per_day: dict[int, list[int]] = {}
    for i, w in enumerate(week):
        if w.kind != "release":
            per_day.setdefault(w.day, []).append(i)
    cap, lane_dr = _harbour_room(giant, size)
    step = 2.7 * size / giant.rail_length
    out: dict[int, Slot] = {}
    for d, items in per_day.items():
        lanes = min(len(_LANES), math.ceil(len(items) / cap))
        for j, i in enumerate(items):
            lane, k = j % lanes, j // lanes
            in_lane = len(range(lane, len(items), lanes))
            along = (d + 0.5) / 7 + (k - (in_lane - 1) / 2 + (0.5 if lane % 2 else 0.0)) * step
            phi = giant.rail_angle(min(1.0, max(0.0, along)))
            x, y, _ = giant.ring_point(giant.rail + _LANES[lane] * lane_dr, phi)
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
#: The night side's columns, one per day, in sin(longitude): even on screen.
_NIGHT_COLUMNS = (-0.88, 0.12)


def _town_room(terra: Terra, kind: str) -> float:
    """Half the width of a day's column on screen at a kind's latitude: how far a cluster may spread."""
    lo, hi = _NIGHT_COLUMNS
    return 0.5 * (hi - lo) / 7 * terra.radius * math.cos(math.radians(NIGHT_LATITUDES[kind]))


def nightfall_slots(stage: Stage, terra: Terra, week: Sequence[Work], seed: int = 0, size: float = 0.0) -> list[Slot]:
    """Towns on the night side: the day is the column (Monday at the left limb), the kind the row; a
    day's towns of one kind gather round each other in a sunflower, as tight as their size allows.
    Landers come in from the night side. ``size`` is a light's radius."""
    rnd = np.random.default_rng(seed * 103 + 5)
    size = size or glyph_size(stage)
    lo, hi = _NIGHT_COLUMNS
    col_w = (hi - lo) / 7
    groups: dict[tuple[int, str], list[int]] = {}
    for i, w in enumerate(week):
        groups.setdefault((w.day, w.kind), []).append(i)
    spacing = 2.9 * size
    out: dict[int, Slot] = {}

    def on_day_side(p: Point) -> bool:
        return p[0] > terra.cx + 0.2 * terra.radius and abs(p[1] - terra.cy) < 1.05 * terra.radius

    for (day, kind), items in groups.items():
        lat = NIGHT_LATITUDES[kind]
        lon = math.degrees(math.asin(lo + col_w * (day + 0.5)))
        x0, y0, _ = terra.point(lat, lon)
        for k, i in enumerate(items):
            r = 0.0 if len(items) == 1 else 0.55 * spacing * math.sqrt(k + 0.5)
            x = x0 + r * math.cos(k * _GOLDEN) + float(rnd.uniform(-0.06, 0.06)) * spacing
            y = y0 + 1.3 * r * math.sin(k * _GOLDEN) + float(rnd.uniform(-0.06, 0.06)) * spacing
            nx, ny = (x - terra.cx) / terra.radius, (terra.cy - y) / terra.radius
            nz = math.sqrt(max(0.0, 1 - nx * nx - ny * ny))
            squash = (max(0.3, nz), math.atan2(-ny, nx))
            away = math.pi + float(rnd.uniform(-0.75, 0.75)) + (0.35 if lat < 0 else -0.35)
            out[i] = Slot((x, y), (x, y), _launch(stage, (x, y), away, rnd, 0.4, avoid=on_day_side), squash)
    return [out[i] for i in range(len(week))]


def halo_slots(stage: Stage, ec: Eclipse, week: Sequence[Work], seed: int = 0, size: float = 0.0) -> list[Slot]:
    """Places on the orbits: the kind's orbit, at the time of the week (Monday at the top, clockwise),
    nudged apart where two would touch. Probes come in from outside, a little behind their place.
    ``size`` is a light's radius."""
    rnd = np.random.default_rng(seed * 107 + 3)
    angles = [week_angle(w.hour) for w in week]
    size = size or glyph_size(stage)
    for k, kind in enumerate(ORBIT_KINDS):
        idx = sorted((i for i, w in enumerate(week) if w.kind == kind), key=lambda i: angles[i])
        if len(idx) < 2:
            continue
        b = ec.orbits[k] * ec.radius
        gap = min(2.7 * size / (b * (1 + ec.ratio) / 2), 2 * math.pi / len(idx))
        for _ in range(40 + 2 * len(idx)):  # push neighbours apart until no two touch
            moved = False
            for a, c in zip(idx, idx[1:], strict=False):
                d = angles[c] - angles[a]
                if d < gap - 1e-9:
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


def light_scale(stage: Stage, week: Sequence[Work], look: str) -> float:
    """How big the lights can be for this week (1 is full size): as big as fits the busiest day (and, at
    night, the busiest town), down to ``MIN_SCALE``. Below that, lights crowd rather than shrink."""
    base = glyph_size(stage)
    per: dict[tuple[int, str], int] = {}
    for w in week:
        per[(w.day, w.kind)] = per.get((w.day, w.kind), 0) + 1
    fit = base
    if look == "harbour":
        giant = Giant.on(stage)
        days: dict[int, int] = {}
        for (day, kind), m in per.items():
            if kind != "release":
                days[day] = days.get(day, 0) + m
        busiest = max(days.values(), default=0)
        if busiest:  # three lanes at most
            fit = giant.rail_length / 7 / (2.7 * math.ceil(busiest / 3)) / 1.2
    elif look == "nightfall":
        terra = Terra.on(stage)
        for (_, kind), m in per.items():
            if m > 1:
                fit = min(fit, 0.9 * _town_room(terra, kind) / (0.55 * math.sqrt(m - 0.5)) / 2.9)
    else:
        ec = Eclipse.on(stage)
        for (_, kind), m in per.items():  # a day's office hours, about a twelfth of the orbit
            b = ec.orbits[ORBIT_KINDS.index(kind)] * ec.radius
            arc = 2 * math.pi * b * (1 + ec.ratio) / 2 / 12
            fit = min(fit, arc / (2.7 * m))
    return min(1.0, max(MIN_SCALE, fit / base))


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
    """Each kind's colour as the show draws it: the theme's, lifted to glow on the night on a light
    theme (``_sky.night``)."""
    stage = night(stage)
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
    """A look's world on a stage: its actors, the slots for the week and what a release does besides its
    light (``release_lit``: what it does is its light, as the harbour's new moon is)."""

    actors: list[Actor]
    slots: list[Slot]
    release: Callable[[int, Slot, float, Array], list[Actor]]
    light_z: int = 20
    release_lit: bool = False


def _moment_px(week: Sequence[Work]) -> int:
    """Pixels a frame for a release's moment (a flare, a wave of light): soft light, drawn small, and
    smaller when the week has several releases."""
    releases = sum(w.kind == "release" for w in week)
    return max(16_000, round(60_000 * min(1.0, 1.25 / max(1, releases))))


def _harbour(stage: Stage, week: Sequence[Work], frames: int, size: float) -> World:
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
            return [still_sprite(moon).actor(Route.still(*slot.light), z=21)]
        wave = rail_wave(stage, giant, colour, max_px=_moment_px(week))
        return [moon.actor(Route.still(*slot.light), enter=t, z=21),
                wave.actor(Route.still(giant.cx, giant.cy), enter=t + 0.25, leave=t + 0.25 + _duration(wave), z=22)]

    return World(actors, harbour_slots(stage, giant, week, stage.seed, size), release, release_lit=True)


def _nightfall(stage: Stage, week: Sequence[Work], frames: int, size: float) -> World:
    terra = Terra.on(stage)
    at = Route.still(terra.cx, terra.cy)
    clouds = terra_clouds(stage, terra, frames=frames)
    if stage.still:
        clouds = still_sprite(clouds)
    actors = [terra_base(stage, terra).actor(at, z=2), clouds.actor(at, z=4)]

    def release(i: int, slot: Slot, t: float, colour: Array) -> list[Actor]:
        if stage.still:
            return []
        up = beam(stage, slot.light[0], slot.light[1], colour, max_px=_moment_px(week) // 2)
        flash = sky_flash(stage, terra.cx, terra.cy, terra.radius, colour, max_px=_moment_px(week))
        return [up.actor(Route.still(*slot.light), enter=t, leave=t + _duration(up), z=24),
                flash.actor(Route.still(terra.cx, terra.cy), enter=t + 0.1, leave=t + 0.1 + _duration(flash), z=22)]

    return World(actors, nightfall_slots(stage, terra, week, stage.seed, size), release)


def _halo(stage: Stage, week: Sequence[Work], frames: int, size: float) -> World:
    ec = Eclipse.on(stage)
    at = Route.still(ec.cx, ec.cy)
    corona = corona_loop(stage, ec, frames=frames)
    if stage.still:
        corona = still_sprite(corona)
    actors = [corona.actor(at, z=2), eclipse_base(stage, ec).actor(at, z=4),
              orbit_lines(stage, ec, colours(stage)).actor(at, z=6)]

    def release(i: int, slot: Slot, t: float, colour: Array) -> list[Actor]:
        if stage.still:
            return []
        flare = diamond_flare(stage, ec, colour, max_px=_moment_px(week))
        return [flare.actor(Route.still(ec.cx, ec.cy), enter=t - 0.15, leave=t - 0.15 + _duration(flare), z=22)]

    return World(actors, halo_slots(stage, ec, week, stage.seed, size), release)


WORLDS: dict[str, Callable[[Stage, Sequence[Work], int, float], World]] = {
    "harbour": _harbour, "nightfall": _nightfall, "halo": _halo}


# ================================================================ the show ==


#: Of the contract's limits (``scenes.MAX_PIXELS``, ``scenes.MAX_ACTORS``), what a show plans to use,
#: however busy the week: the rest is headroom.
PIXEL_SHARE, ACTOR_SHARE = 0.66, 0.85
#: How a piece of work arrives, from the most to the least the terminal holds for it:
#:
#: * ``full``: it warps in, flies, lands or docks (its own arrival) and its light comes on;
#: * ``busy``: it warps in briefly, flies and stops, and its light flashes on where it stops;
#: * ``spark``: its light flashes on, no ship (only when the week is too big for ships);
#: * ``still``: its light just comes on.
#:
#: A release is always ``full``. Every one leaves its light, one shared picture per kind.
TIERS = ("full", "busy", "spark", "still")
_TIER_ACTORS = {"full": 5, "busy": 4, "spark": 2, "still": 1}  # warp, ship, arrival, light on, held light
#: The show's last cue, at its end: what is on stage now holds.
END = ("end",)
#: At most this many pieces of work are shown (the latest): a light each, and room for the world.
MAX_WORK = int(ACTOR_SHARE * MAX_ACTORS) - 80
#: Launches before the first one, and the time a show needs after its last one.
FIRST, TAIL = 0.9, 4.3
#: Room round a release, before and after (seconds).
RELEASE_ROOM = 0.8


def schedule(week: Sequence[Work], length: float = 20.0) -> list[float]:
    """When each piece of work's ship sets off, in order, so the show lasts about ``length`` seconds: no
    more than 1.4 s apart however quiet the week, with room round each release, and in waves, a day at
    a time with a pause between the days, once the week is busy."""
    n = len(week)
    if not n:
        return []
    releases = sum(w.kind == "release" for w in week)
    busy = min(1.0, max(0.0, (n - 25) / 50))
    days = len({w.day for w in week})
    pause = busy * 0.3 * (n - 1) / max(1, days - 1)  # in launches: the days' pauses, about 30% of a busy show
    units = [0.0]
    for a, b in zip(week, week[1:], strict=False):
        units.append(units[-1] + 1.0 + (pause if b.day != a.day else 0.0))
    span = max(0.5, length - FIRST - TAIL - 2 * RELEASE_ROOM * releases)
    unit = min(1.4, span / units[-1]) if units[-1] else 0.0
    out, extra = [], 0.0
    for w, u in zip(week, units, strict=True):
        if w.kind == "release":
            extra += RELEASE_ROOM
        out.append(FIRST + u * unit + extra)
        if w.kind == "release":
            extra += RELEASE_ROOM
    return out


def _spread(count: int) -> list[int]:
    """0 to ``count - 1`` in an order whose every start is spread evenly through them (golden ratio)."""
    return sorted(range(count), key=lambda k: ((k + 0.5) * 0.6180339887) % 1.0)


@dataclass
class _Item:
    """One piece of work on its way: where it starts and stops, how it flies and how it arrives."""

    index: int
    work: Work
    slot: Slot
    launch: float
    start: Point
    heading: float
    flight: float
    tier: str = "busy"


def _pixels(actors: Sequence[Actor]) -> int:
    """The pixels the terminal holds for ``actors``: an image shared by stills and loops counts once."""
    seen: dict[int, Actor] = {}
    for a in actors:
        seen[id(a.frames) if len(a.frames) == 1 or a.loop else id(a)] = a
    return sum(a.width * a.height * len(a.frames) for a in seen.values())


#: A ship's plume flickers over this many frames; two in a busy week (more than ``BUSY`` pieces of work).
PLUME_FRAMES, BUSY = 3, 60


def _ship_px(stage: Stage, style: Style, kind: str, heading: float, frames: int) -> int:
    design = FAMILIES[style.family][kind]
    w, h, _, _ = ship_canvas(design, standard_length(stage) * design.size, heading)
    return w * h * frames


def _tiers(stage: Stage, style: Style, items: list[_Item], fixed_px: int, fixed_actors: int, scale: float) -> None:
    """Choose how each piece of work arrives (``TIERS``) so the show fits ``PIXEL_SHARE`` of the pixel
    limit and ``ACTOR_SHARE`` of the actor limit: every one ``busy`` to start with (fewer if that is
    already too much), then as many ``full`` as fit, spread through the week."""
    budget, room = PIXEL_SHARE * MAX_PIXELS, int(ACTOR_SHARE * MAX_ACTORS) - fixed_actors
    for it in items:
        it.tier = "full" if it.work.kind == "release" else "busy"
    plain = [it for it in items if it.work.kind != "release"]
    flash = 900 * 7

    def busy_px(it: _Item) -> int:
        return warp_cost(stage, style.warp, brief=True, max_px=BRIEF_WARP_PX) + flash

    def full_px(it: _Item) -> int:
        kind, h = it.work.kind, it.heading
        big = kind == "release"
        return (warp_cost(stage, style.warp, big) + arrive_cost(stage, style.family, kind, h, style.arrive)
                + light_cost(stage, kind, style.light, scale))

    plume = PLUME_FRAMES if len(items) <= BUSY else 2

    def total() -> tuple[int, int]:
        ships = {(it.work.kind, it.heading): _ship_px(stage, style, it.work.kind, it.heading, plume)
                 for it in items if it.tier in ("full", "busy")}
        px = fixed_px + sum(ships.values())
        px += sum(full_px(it) if it.tier == "full" else busy_px(it) if it.tier == "busy"
                  else flash if it.tier == "spark" else 0 for it in items)
        return px, sum(_TIER_ACTORS[it.tier] for it in items)

    order = [plain[k] for k in _spread(len(plain))]
    px, actors = total()
    for lower in ("spark", "still"):  # a week too big for ships, then for flashes
        for it in reversed(order):
            if px <= budget and actors <= room:
                break
            if _TIER_ACTORS[lower] < _TIER_ACTORS[it.tier]:
                it.tier = lower
                px, actors = total()
    if any(it.tier != "busy" for it in plain):
        return
    for it in order:  # then as many full arrivals as fit
        cost = full_px(it) - busy_px(it)
        if px + cost > budget or actors + 1 > room:
            break
        it.tier = "full"
        px, actors = px + cost, actors + 1


#: A brief warp-in's pixels a frame (``busy``): soft light, drawn small.
BRIEF_WARP_PX = 1_500


def make(stage: Stage, week: Sequence[Work], look: str = "halo", length: float = 20.0,
         loop_frames: int = 32) -> Scene:
    """The week in review on ``stage``, in ``look`` (``LOOKS``): about ``length`` seconds of ships, however
    busy the week, then the summary holds. A busy week comes in waves, a day at a time, its lights
    smaller (``light_scale``) and most of its ships arriving the cheaper way (``TIERS``), so the show
    fits the contract's limits. On a light theme the show brings its own night (``_sky``). Under
    ``minimal`` motion (``stage.still``) only the summary is drawn, one frame per actor.

    Each light that comes on is a cue, ``("landed", i, kind)``, ``i`` the work's index in ``week``, and the
    last one, ``END``, comes as the show is done, just before the summary holds. A week
    of more than ``MAX_WORK`` pieces of work shows its latest ``MAX_WORK``: one light each is all the
    terminal holds."""
    picked = list(range(len(week)))[-MAX_WORK:]
    if len(picked) < len(week):
        week = [week[k] for k in picked]
    sky = night_sky(stage)  # on a light theme, a window into space behind everything
    stage = night(stage)
    style = STYLES[look]
    scale = light_scale(stage, week, look)
    world = WORLDS[look](stage, week, loop_frames, glyph_size(stage) * scale * (1.2 if style.light == "bead" else 1))
    inks = colours(stage)
    actors = sky + list(world.actors)
    cues: list[Cue] = []
    held: dict[Light, Sprite] = {}

    def held_for(kind: str, i: int, squash: tuple[float, float]) -> tuple[Light, Sprite]:
        """The light that stays for a kind: one picture per kind (and, at night, per slope and town),
        and what it is drawn from (kind, town seed, slope)."""
        if style.light == "city":
            ratio, angle = squash
            ratio = round(ratio * 10) / 10
            angle = 0.0 if ratio > 0.95 else round(angle / (math.pi / 12)) * (math.pi / 12)
            key: Light = (kind, stage.seed * 31 + i % 3, (ratio, angle))
        else:
            key = (kind, stage.seed * 31, (1.0, 0.0))
        if key not in held:
            held[key] = held_light(stage, kind, inks[kind], style.light, key[1], key[2], scale)
        return key, held[key]

    if stage.still:
        for i, (work, slot) in enumerate(zip(week, world.slots, strict=True)):
            if work.kind == "release" and world.release_lit:
                actors += world.release(i, slot, 0.0, inks[work.kind])
                continue
            actors.append(held_for(work.kind, i, slot.squash)[1].actor(Route.still(*slot.light), z=world.light_z))
            actors += world.release(i, slot, 0.0, inks[work.kind]) if work.kind == "release" else []
        return Scene(actors, 0.1, [Cue(0.1, END)])

    items = []
    for i, (work, slot, launch) in enumerate(zip(week, world.slots, schedule(week, length), strict=True)):
        big = work.kind == "release"
        dx, dy = slot.dock
        sx, sy = warp_spot(stage, *slot.start, style.warp, big)
        flight = 1.35 + 1.1 * math.hypot(dx - sx, dy - sy) / stage.width + (0.6 if big else 0.0)
        items.append(_Item(i, work, slot, launch, (sx, sy), heading_of(math.atan2(dy - sy, dx - sx)), flight))
    extras = []  # what the releases do besides their lights: drawn now, so the budget knows them
    for it in items:
        if it.work.kind == "release":
            land = it.launch + WARP_LEAD + it.flight
            extras += world.release(it.index, it.slot, land + _LIGHT_AT[style.arrive], inks["release"])
    fixed = _pixels(actors + extras) + sum(h.width * h.height for h in
                                           (held_for(w.kind, i, sl.squash)[1] for i, (w, sl) in
                                            enumerate(zip(week, world.slots, strict=True))))
    _tiers(stage, style, items, fixed, len(actors) + len(extras), scale)
    actors += extras

    ships: dict[tuple[str, float], Sprite] = {}
    drawn: dict[tuple[object, ...], Sprite] = {}  # one-shots drawn once (each still has an image of its own)
    arrivals: dict[tuple[object, ...], tuple[Sprite, float]] = {}
    end = 1.0
    for it in items:
        work, slot, i = it.work, it.slot, it.index
        kind, colour, big = work.kind, inks[work.kind], work.kind == "release"
        (sx, sy), (dx, dy) = it.start, slot.dock
        t, heading = it.launch, it.heading
        go, land = t + WARP_LEAD, t + WARP_LEAD + it.flight
        if it.tier in ("full", "busy"):
            brief = it.tier == "busy"
            key = ("warp", kind, heading, big, brief)
            if key not in drawn:
                drawn[key] = warp_in(stage, heading, colour, style.warp, big=big, brief=brief,
                                     max_px=BRIEF_WARP_PX if brief else 6_000)
            warp = drawn[key]
            actors.append(warp.actor(Route.still(sx, sy), enter=t, leave=t + _duration(warp), z=50))
            if (kind, heading) not in ships:
                make_ship = folded_ship if style.folded else ship_sprite
                ships[(kind, heading)] = make_ship(stage, style.family, kind, heading, colour, style.sun,
                                                   frames=PLUME_FRAMES if len(items) <= BUSY else 2)
            ship = ships[(kind, heading)]
            actors.append(ship.actor(Route.line((sx, sy), (dx, dy), go, land, ease=_glide), enter=go, leave=land,
                                     z=41 if big else 40))
        if it.tier == "full":
            squash_key = (round(slot.squash[0], 1), round(slot.squash[1], 1))
            akey = (kind, heading, squash_key)
            if akey not in arrivals:
                arrivals[akey] = arrive(stage, style.family, kind, heading, colour, style.arrive, style.sun,
                                        squash=slot.squash)
            last, light_at = arrivals[akey]
            actors.append(last.actor(Route.still(dx, dy), enter=land, leave=land + _duration(last), z=30))
            lit = land + light_at
        else:
            lit = land if it.tier == "busy" else t + WARP_LEAD
        if not (big and world.release_lit):
            hkey, keep = held_for(kind, i, slot.squash)
            if it.tier == "full":
                okey = ("on", *hkey)
                if okey not in drawn:  # the same light as the one that stays, coming on
                    drawn[okey] = light_on(stage, kind, colour, style.light, hkey[1], hkey[2], scale)
                on = drawn[okey]
                actors.append(on.actor(Route.still(*slot.light), enter=lit, leave=lit + LIGHT_SETTLES,
                                       z=world.light_z + 1))
                stays = lit + LIGHT_SETTLES
            elif it.tier in ("busy", "spark"):
                flash = light_flash(stage, *slot.light, colour, scale, big=big)
                actors.append(flash.actor(Route.still(*slot.light), enter=lit, leave=lit + _duration(flash),
                                          z=world.light_z + 1))
                stays = lit + FLASH_PEAK
            else:
                stays = lit
            actors.append(keep.actor(Route.still(*slot.light), enter=stays, z=world.light_z))
        cues.append(Cue(lit, ("landed", picked[i], kind)))
        end = max(end, lit + (2.0 if big else 1.0))
    cues.append(Cue(end + 0.6, END))
    return Scene(actors, end + 0.6, cues)


def summary_glyph(paint: Paint, kind: str, x: float, y: float, stage: Stage) -> None:
    """A kind's light at (x, y) on any canvas: for a legend next to the scene."""
    glyph(paint, kind, x, y, glyph_size(stage), colours(stage)[kind])


__all__ = ["DAYS", "END", "KINDS", "LOOKS", "ROLE", "STYLES", "Slot", "Style", "Work", "World", "colours",
           "day_edges", "make", "mini_week", "sample_week", "summary_glyph"]
