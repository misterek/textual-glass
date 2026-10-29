"""One-shot effects for the space scenes: the lights the week leaves, how they
come on, ships warping in and ships arriving. Pure numpy, no Textual imports.
Exploratory.

Every effect is a :class:`Sprite` that glass steps through once and that ends
empty or hands over to what stays, so nothing pops. A light that stays is a
still of its own (:func:`held_light`), the same picture for every light of its
kind, so the terminal holds one image per kind however busy the week; the
light coming on (:func:`light_on`, or the cheaper :func:`light_flash` when the
week is busy) plays over it and goes. One shape per kind of work, the same in
every look, so a page can carry one legend:

* merged: a round light;  review: a ring round a point;  deploy: a diamond;
* ticket: a small plain point;  release: a four-pointed star.
"""

from __future__ import annotations

import math

import numpy as np

from ..flourishes._draw import bump, ease_in_out, ease_out, ramp
from ..render import Array, smoothstep
from . import Stage
from ._paint import TAU, WHITE, Paint, Sprite, blend, soft_glow, star, stretched_size
from ._ships import FAMILIES, hull_inks, paint_ship, ship_canvas, standard_length


def glyph_size(stage: Stage) -> float:
    """A light's radius in terminal pixels: about a third of a row."""
    return 0.36 * stage.cell[1]


# ================================================================ glyphs ==


def glyph(paint: Paint, kind: str, x: float, y: float, size: float, colour: Array, level: float = 1.0,
          squash: tuple[float, float] = (1.0, 0.0)) -> None:
    """The light a kind of work leaves, centred at (x, y), ``size`` pixels in radius. ``squash`` is
    (ratio, angle): a light on a sphere, seen at a slant, is narrower along the slope."""
    if level <= 0.003:
        return
    ratio, angle = squash
    dx, dy = paint.xs - x, paint.ys - y
    if ratio < 0.999:  # measure across the slope at full length, along it stretched
        c, s = math.cos(angle), math.sin(angle)
        along, across = dx * c + dy * s, -dx * s + dy * c
        dx, dy = along / max(0.35, ratio), across
    r = np.sqrt(dx * dx + dy * dy)
    hot = WHITE if paint.stage.dark else blend(colour, WHITE, 0.15) * 0.8
    bright = blend(colour, WHITE, 0.45)
    halo = 2.3 * size
    if kind == "merged":
        body = np.clip(0.62 * size + 0.5 - r, 0, 1)
        paint.shine(colour, soft_glow(r, halo) * 0.55 * level)
        paint.shine(bright, body * 0.95 * level)
        paint.shine(hot, soft_glow(r, 0.45 * size + 0.8) * 0.9 * level)
    elif kind == "review":
        ring = np.clip(0.2 * size + 0.55 - np.abs(r - 0.8 * size), 0, 1)
        paint.shine(colour, soft_glow(r, halo) * 0.4 * level)
        paint.shine(bright, ring * 1.0 * level)
        paint.shine(hot, soft_glow(r, 0.32 * size + 0.8) * 1.0 * level)
    elif kind == "deploy":
        dia = (np.abs(dx) + np.abs(dy)) / math.sqrt(2)
        body = np.clip(0.6 * size + 0.5 - dia, 0, 1)
        paint.shine(colour, soft_glow(dia, halo) * 0.5 * level)
        paint.shine(bright, body * 0.95 * level)
        paint.shine(hot, soft_glow(r, 0.4 * size + 0.8) * 0.85 * level)
    elif kind == "ticket":
        body = np.clip(0.36 * size + 0.5 - r, 0, 1)
        paint.shine(colour, soft_glow(r, 1.5 * size) * 0.35 * level)
        paint.shine(bright, body * 0.9 * level)
        paint.shine(hot, soft_glow(r, 0.25 * size + 0.7) * 0.7 * level)
    else:  # release
        paint.shine(colour, soft_glow(r, 3.2 * size) * 0.55 * level)
        paint.shine(colour, star(paint.xs, paint.ys, x, y, 0.5 * size, 3.4 * size, 3.6 * size) * 0.9 * level)
        paint.shine(hot, star(paint.xs, paint.ys, x, y, 0.35 * size, 2.2 * size, 2.6 * size) * 0.9 * level)
        paint.shine(hot, soft_glow(r, 0.6 * size + 1.0) * 1.0 * level)


def glyph_reach(kind: str, size: float) -> float:
    return (3.7 if kind == "release" else 2.4) * size


#: When a light coming on (:func:`light_on`) settles, in seconds: the held light takes over then.
LIGHT_SETTLES = 0.8
_LIGHT_TIMES = (0.0, 0.06, 0.13, 0.22, 0.33, 0.47, 0.62, LIGHT_SETTLES)
#: Town lights round a city, per kind (``city`` style), at full size.
_TOWNS = {"merged": 5, "review": 4, "deploy": 5, "ticket": 3, "release": 7}


def _light_size(stage: Stage, kind: str, style: str, scale: float) -> tuple[float, float, int]:
    """A light's radius, how far its light reaches and its square canvas's side, in terminal pixels."""
    size = glyph_size(stage) * scale * (1.25 if kind == "release" else 1.0) * (1.2 if style == "bead" else 1.0)
    reach = max(glyph_reach(kind, size), 3.0 * size) + 2.0
    side = math.ceil(2 * reach)
    return size, reach, side + (side % 2)


def _light_frame(stage: Stage, kind: str, colour: Array, style: str, seed: int, squash: tuple[float, float],
                 scale: float, e: float) -> bytes:
    """A light ``e`` of the way (0 to 1) through coming on: a white flash and a ring in the kind's colour,
    the light swelling a little past its level and settling; at 1, the light that stays."""
    size, reach, w = _light_size(stage, kind, style, scale)
    h = w
    cx = cy = w / 2
    rnd = np.random.default_rng(seed)
    n_town = round(_TOWNS[kind] * min(1.0, scale) ** 2) if style == "city" else 0
    town_a = rnd.uniform(0, TAU, n_town)
    town_r = rnd.uniform(0.9, 2.0, n_town) * size
    town_k = rnd.uniform(0.25, 0.6, n_town)
    ratio, angle = squash
    paint = Paint(w, h, stage.dark, margin=2.0)
    flash = bump(e, 0.12, 0.6)
    hot = WHITE if stage.dark else colour * 0.8
    if e < 1.0:
        ring_r = size * (0.6 + 2.1 * ease_out(e / 0.75))
        r = np.sqrt((paint.xs - cx) ** 2 + (paint.ys - cy) ** 2)
        ring = np.exp(-(((r - ring_r) / (0.22 * size + 0.6)) ** 2)) * smoothstep(reach, reach - 2.5, r)
        paint.shine(colour, ring * (1 - ramp(0.2, 0.75, e)) * 0.8)
        paint.shine(hot, soft_glow(r, size * (0.8 + 1.4 * flash)) * 1.6 * flash)
        if style == "bead":  # the flash runs both ways along the rail (horizontal enough at this size)
            across = np.abs(paint.ys - cy)
            streak = np.exp(-across / (0.12 * size + 0.4)) * np.exp(-np.abs(paint.xs - cx) / (1.3 * size))
            paint.shine(blend(colour, WHITE, 0.5), streak * 1.1 * flash * smoothstep(reach, reach - 3, r))
        if style == "star":
            spikes = star(paint.xs, paint.ys, cx, cy, 0.4 * size, size * (1.2 + 2.0 * flash),
                          reach - 1.0, angle=math.pi / 4)
            paint.shine(hot, spikes * 1.2 * flash)
    level = ease_in_out(ramp(0.1, 0.55, e)) * (1 + 0.35 * bump(e - 0.3, 0.15, 0.6))
    glyph(paint, kind, cx, cy, size, colour, level, squash)
    for k in range(n_town):
        grow = ramp(0.25 + 0.08 * k, 0.6 + 0.08 * k, e)
        tx = cx + town_r[k] * math.cos(town_a[k]) * (ratio if abs(math.cos(town_a[k] - angle)) > 0.7 else 1)
        ty = cy + town_r[k] * math.sin(town_a[k])
        d = np.sqrt((paint.xs - tx) ** 2 + (paint.ys - ty) ** 2)
        paint.shine(blend(colour, WHITE, 0.3), soft_glow(d, 0.55 * size + 0.8) * town_k[k] * grow)
    return paint.frame()


def light_on(stage: Stage, kind: str, colour: Array, style: str, seed: int = 0,
             squash: tuple[float, float] = (1.0, 0.0), scale: float = 1.0) -> Sprite:
    """A light coming on: a white flash and a ring in the kind's colour, the light swelling and settling.
    It plays for ``LIGHT_SETTLES`` seconds and goes: put the :func:`held_light` of the same arguments,
    which is exactly where it settles, on stage then.

    ``style``: ``bead`` (a berth on the harbour's rail: the flash runs along the rail), ``city`` (lights
    spread round it like a town, and stay), ``star`` (a twinkle that shrinks to the star's own spikes).
    ``scale`` makes it smaller (a busy week)."""
    _, _, w = _light_size(stage, kind, style, scale)
    times = _LIGHT_TIMES
    out = [_light_frame(stage, kind, colour, style, seed, squash, scale, t / times[-1]) for t in times[:-1]]
    gaps = [max(16, round((b - a) * 1000)) for a, b in zip(times, times[1:], strict=False)]
    return Sprite(out, w, w, gaps, False, name=f"light-{kind}")


def held_light(stage: Stage, kind: str, colour: Array, style: str, seed: int = 0,
               squash: tuple[float, float] = (1.0, 0.0), scale: float = 1.0) -> Sprite:
    """The light that stays, a still: what :func:`light_on` settles into. Make one per kind (and look)
    and put every light of that kind on stage from it, so they share one image."""
    _, _, w = _light_size(stage, kind, style, scale)
    return Sprite([_light_frame(stage, kind, colour, style, seed, squash, scale, 1.0)], w, w, [1000], False,
                  name=f"held-{kind}")


#: When a :func:`light_flash` is at its brightest, in seconds: the held light comes on under it then.
FLASH_PEAK = 0.07


def light_flash(stage: Stage, x: float, y: float, colour: Array, scale: float = 1.0, big: bool = False,
                max_px: int = 900) -> Sprite:
    """A light coming on the cheap way, for a busy week: a white flash and a ring of the kind's colour
    centred at (x, y) on the stage, soft, so drawn small and stretched over the cells round it. It ends
    empty: the :func:`held_light` comes on under it at ``FLASH_PEAK``."""
    from ._worlds import place  # the worlds draw with the effects: imported here, not at the top

    size = glyph_size(stage) * scale * (1.6 if big else 1.0)
    reach = 3.3 * size + 2.0
    spot = place(stage, x, y, reach, reach)
    w, h, s = stretched_size(stage, spot.cols, spot.rows, max_px)
    cw, ch = stage.cell
    ox, oy = (x - spot.col * cw) * s, (y - spot.row * ch) * s
    paint0 = Paint(w, h, stage.dark)
    r = np.sqrt((paint0.xs - ox) ** 2 + (paint0.ys - oy) ** 2) / s
    fence = smoothstep(reach, reach - 3.0, r)
    hot = WHITE if stage.dark else colour * 0.8
    times = (0.0, 0.04, FLASH_PEAK, 0.12, 0.19, 0.28, 0.4)
    out = []
    for t in times:
        e = t / times[-1]
        flash = bump(e, 0.16, 0.7)
        ring_r = size * (0.5 + 2.4 * ease_out(e))
        ring = np.exp(-(((r - ring_r) / (0.25 * size + 0.8)) ** 2)) * (1 - ramp(0.3, 1.0, e)) * ramp(0.0, 0.15, e)
        paint = Paint(w, h, stage.dark, margin=1.0)
        paint.shine(colour, ring * 0.85 * fence)
        paint.shine(hot, soft_glow(r, size * (0.7 + 1.3 * flash)) * 1.5 * flash * fence)
        paint.shine(colour, soft_glow(r, 2.4 * size) * 0.45 * flash * fence)
        out.append(paint.frame())
    gaps = [max(16, round((b - a) * 1000)) for a, b in zip(times, times[1:], strict=False)] + [40]
    return Sprite(out, w, h, gaps, False, (spot.cols, spot.rows), offset=spot.offset(stage), name="flash")


# =============================================================== warp in ==


def warp_in(stage: Stage, heading: float, colour: Array, style: str, big: bool = False,
            max_px: int = 6_000, brief: bool = False) -> Sprite:
    """A ship arriving out of nowhere at the centre of the picture, heading along ``heading``. The ship
    itself appears at about 120 ms, in the flash. It is soft light, so it is drawn small (at most
    ``max_px`` pixels a frame) and stretched over whole cells: place it, and start the ship, on
    :func:`warp_spot`. Its last frame is (nearly) empty.

    ``style``: ``gate`` (a ring opens across its path and closes behind it), ``streak`` (it drops out of a
    streak of light), ``fold`` (space twinkles and folds open round it). ``brief``: five frames, not
    nine, for a busy week."""
    cols, rows = warp_cells(stage, style, big)
    w, h, sc = stretched_size(stage, cols, rows, max_px)
    L = standard_length(stage) * (1.8 if big else 1.0) * sc
    reach = min(w, h) / 2 - 2
    cx, cy = w / 2, h / 2
    c, s = math.cos(heading), math.sin(heading)
    hot = WHITE if stage.dark else colour * 0.75
    bright = blend(colour, WHITE, 0.4)
    times = [0.0, 0.1, 0.23, 0.4, 0.6] if brief else [0.0, 0.05, 0.1, 0.16, 0.23, 0.31, 0.4, 0.5, 0.6]
    out: list[bytes] = []
    for t in times:
        e = t / times[-1]
        paint = Paint(w, h, stage.dark, margin=1.5)
        dx, dy = paint.xs - cx, paint.ys - cy
        along, across = dx * c + dy * s, -dx * s + dy * c
        r = np.sqrt(dx * dx + dy * dy)
        fence = smoothstep(reach + 1, reach - 2, r)
        flash = bump(e, 0.2, 0.55)
        thin = max(0.6, 0.035 * L)
        if style == "gate":
            # an ellipse across the path: wide across, thin along, opening then closing
            open_ = bump(e, 0.3, 1.0)
            ra, rb = 0.62 * L * (0.25 + 0.75 * ease_out(min(1, e / 0.3))), 0.16 * L
            q = np.sqrt((across / max(1, ra)) ** 2 + (along / rb) ** 2)
            rim = np.exp(-(((q - 1) * min(ra, rb) / (0.05 * L + 0.5)) ** 2))
            paint.shine(colour, rim * 1.3 * open_ * fence)
            paint.shine(bright, rim * np.exp(-np.abs(across) / (0.4 * L)) * 0.6 * open_ * fence)
            paint.shine(colour, smoothstep(1.0, 0.4, q) * 0.35 * open_ * fence)
        elif style == "streak":
            # a streak along the path behind it, rushing in and gone
            tail = 0.1 * L + 1.9 * L * (1 - ease_out(min(1, e / 0.35)))
            on = (along < 0) * np.exp(-np.maximum(-along, 0) / max(1.0, tail))
            line = np.exp(-(across / thin) ** 2) * on * (1 - ramp(0.3, 0.6, e))
            paint.shine(bright, line * 1.4 * fence)
            paint.shine(hot, line * np.exp(along / (0.2 * L)) * 1.2 * fence)
        else:  # fold
            twinkle = star(paint.xs, paint.ys, cx, cy, 0.06 * L, 0.55 * L * flash + 1, reach - 1, angle=heading)
            paint.shine(hot, twinkle * 1.3 * flash * fence)
            fold_r = 0.9 * L * (1 - ease_out(min(1, e / 0.5)))
            fold = np.exp(-(((r - fold_r) / thin) ** 2)) * (1 - ramp(0.35, 0.55, e))
            paint.shine(colour, fold * 1.0 * fence)
        # the flash, and a ring thrown out after it
        paint.shine(hot, soft_glow(r, 0.28 * L * (0.4 + flash)) * 1.7 * flash)
        paint.shine(colour, soft_glow(r, 0.55 * L) * 0.6 * flash)
        ring_r = 0.15 * L + 0.7 * L * ease_out(ramp(0.25, 1.0, e))
        ring = np.exp(-(((r - ring_r) / thin) ** 2)) * (1 - ramp(0.4, 1.0, e)) * ramp(0.2, 0.3, e)
        paint.shine(colour, ring * 0.9 * fence)
        out.append(paint.frame())
    gaps = [max(16, round((b - a) * 1000)) for a, b in zip(times, times[1:], strict=False)] + [60]
    return Sprite(out, w, h, gaps, False, (cols, rows), name=f"warp-{style}")


def warp_cells(stage: Stage, style: str, big: bool = False) -> tuple[int, int]:
    """The cells a warp-in covers (it is stretched over them)."""
    L = standard_length(stage) * (1.8 if big else 1.0)
    side = 2 * L * (1.2 if style == "streak" else 0.95) + 6
    cw, ch = stage.cell
    return max(1, math.ceil(side / cw)), max(1, math.ceil(side / ch))


def warp_spot(stage: Stage, x: float, y: float, style: str, big: bool = False) -> tuple[float, float]:
    """The point nearest (x, y) where a warp-in's cells line up with the stage's, so it is drawn exactly
    where its ship starts."""
    cols, rows = warp_cells(stage, style, big)
    cw, ch = stage.cell
    return (round(x / cw - cols / 2) + cols / 2) * cw, (round(y / ch - rows / 2) + rows / 2) * ch


#: When the ship appears in a warp-in, seconds after it starts.
WARP_LEAD = 0.12


# ================================================================ arrive ==


def arrive_cost(stage: Stage, family: str, kind: str, heading: float, style: str) -> int:
    """How many pixels :func:`arrive` draws (every frame), without drawing it."""
    design = FAMILIES[family][kind]
    L = standard_length(stage) * design.size
    w, h, _, _ = ship_canvas(design, L, heading)
    side = math.ceil(max(w, h, 1.5 * L) + 8)
    side += side % 2
    return side * side * len(_ARRIVE_TIMES[style])


_ARRIVE_TIMES = {"dock": (0.0, 0.07, 0.14, 0.22, 0.31, 0.4, 0.5),
                 "land": (0.0, 0.1, 0.2, 0.3, 0.4, 0.49, 0.58, 0.7, 0.84, 1.0),
                 "unfold": (0.0, 0.08, 0.17, 0.27, 0.38, 0.5, 0.62, 0.76, 0.9)}
_LIGHT_AT = {"dock": 0.3, "land": 0.6, "unfold": 0.62}


def light_cost(stage: Stage, kind: str, style: str, scale: float = 1.0) -> int:
    """How many pixels :func:`light_on` draws (every frame), without drawing it."""
    side = _light_size(stage, kind, style, scale)[2]
    return side * side * (len(_LIGHT_TIMES) - 1)


def arrive(stage: Stage, family: str, kind: str, heading: float, colour: Array, style: str,
           sun: tuple[float, float, float], squash: tuple[float, float] = (1.0, 0.0)) -> tuple[Sprite, float]:
    """A ship's last moments, centred where it stops: it starts as the flying ship (without its flame)
    and ends empty. Returns the sprite and when its light should come on (seconds after it starts).

    ``style``: ``dock`` (it slides into its berth, shrinking, and clamps flash either side), ``land`` (it
    drops away towards the surface in a sheath of re-entry light, and touches down with a flash and a
    shock ring flat on the ground: ``squash``), ``unfold`` (it brakes with a flash at its nose and opens
    its wings and panels, then turns into its light)."""
    design = FAMILIES[family][kind]
    L = standard_length(stage) * design.size
    w, h, _, _ = ship_canvas(design, L, heading)
    side = math.ceil(max(w, h, 1.5 * L) + 8)
    w = h = side + (side % 2)
    cx = cy = w / 2
    inks = hull_inks(stage)
    c, s = math.cos(heading), math.sin(heading)
    hot = WHITE if stage.dark else colour * 0.75
    times, light_at = list(_ARRIVE_TIMES[style]), _LIGHT_AT[style]
    end = times[-1]
    out: list[bytes] = []
    for t in times:
        e = t / end
        paint = Paint(w, h, stage.dark, margin=2.5)
        r = np.sqrt((paint.xs - cx) ** 2 + (paint.ys - cy) ** 2)
        if style == "dock":
            k = ease_in_out(min(1.0, e / 0.75))
            push = 0.18 * L * k  # into the berth
            paint_ship(paint, design, colour, cx + c * push, cy + s * push, L, heading, sun, inks,
                       flame=0.35 * (1 - k), fade=1 - ramp(0.45, 0.8, e), scale=1 - 0.5 * k)
            clamp = bump(e - 0.45, 0.12, 0.5)
            for side_ in (1, -1):
                x, y = cx - s * side_ * 0.32 * L, cy + c * side_ * 0.32 * L
                paint.shine(hot, star(paint.xs, paint.ys, x, y, 0.05 * L + 0.4, 0.22 * L, 0.3 * L) * 1.4 * clamp)
            paint.shine(colour, soft_glow(r, 0.4 * L) * 0.5 * clamp)
        elif style == "land":
            k = ease_in_out(min(1.0, e / 0.55))
            heat = bump(e, 0.3, 0.6)
            paint_ship(paint, design, colour, cx, cy, L, heading, sun, inks, flame=0.4 * (1 - k),
                       fade=1 - ramp(0.4, 0.58, e), scale=1 - 0.85 * k, heat=heat * 1.2)
            touch = bump(e - 0.5, 0.06, 0.45)
            ratio, angle = squash
            ca, sa = math.cos(angle), math.sin(angle)
            dx, dy = paint.xs - cx, paint.ys - cy
            q = np.sqrt(((dx * ca + dy * sa) / max(0.3, ratio)) ** 2 + (-dx * sa + dy * ca) ** 2)
            shock_r = 0.08 * L + 0.55 * L * ease_out(ramp(0.5, 1.0, e))
            shock = np.exp(-(((q - shock_r) / (0.03 * L + 0.6)) ** 2)) * ramp(0.5, 0.56, e) * (1 - ramp(0.6, 1.0, e))
            paint.shine(colour, shock * 1.0 * smoothstep(0.5 * w - 1, 0.5 * w - 4, r))
            paint.shine(hot, soft_glow(q, 0.2 * L) * 1.6 * touch)
        else:  # unfold
            unfold = 0.35 + 0.65 * ease_out(ramp(0.1, 0.55, e))
            brake = bump(e, 0.08, 0.35)
            paint_ship(paint, design, colour, cx, cy, L, heading, sun, inks, flame=0.0,
                       fade=1 - ramp(0.55, 0.85, e), unfold=unfold)
            nx, ny = cx + c * 0.5 * L, cy + s * 0.5 * L
            dn = np.sqrt((paint.xs - nx) ** 2 + (paint.ys - ny) ** 2)
            paint.shine(hot, soft_glow(dn, 0.22 * L) * 1.3 * brake)
            paint.shine(colour, soft_glow(dn, 0.4 * L) * 0.6 * brake)
            paint.shine(colour, soft_glow(r, 0.45 * L) * 0.6 * bump(e - 0.45, 0.15, 0.5))
        out.append(paint.frame())
    gaps = [max(16, round((b - a) * 1000)) for a, b in zip(times, times[1:], strict=False)] + [60]
    return Sprite(out, w, h, gaps, False, name=f"arrive-{style}"), light_at


def folded_ship(stage: Stage, family: str, kind: str, heading: float, colour: Array,
                sun: tuple[float, float, float], frames: int = 3, gap: int = 70) -> Sprite:
    """A probe in flight with its wings and panels folded (they open where it parks)."""
    design = FAMILIES[family][kind]
    L = standard_length(stage) * design.size
    w, h, cx, cy = ship_canvas(design, L, heading)
    inks = hull_inks(stage)
    out = []
    for f in range(frames):
        paint = Paint(w, h, stage.dark, margin=2.0)
        paint_ship(paint, design, colour, cx, cy, L, heading, sun, inks, flame=0.85 + 0.25 * ((f * 7) % 3) / 2,
                   unfold=0.35)
        out.append(paint.frame())
    return Sprite(out, w, h, [gap], True, offset=(cx - w / 2, cy - h / 2), name=f"{family}-{kind}-folded")


def warp_cost(stage: Stage, style: str, big: bool = False, max_px: int = 6_000, brief: bool = False) -> int:
    """How many pixels :func:`warp_in` draws (every frame), without drawing it."""
    w, h, _ = stretched_size(stage, *warp_cells(stage, style, big), max_px)
    return w * h * (5 if brief else 9)


__all__ = ["FLASH_PEAK", "LIGHT_SETTLES", "WARP_LEAD", "arrive", "arrive_cost", "folded_ship", "glyph", "glyph_reach",
           "glyph_size", "held_light", "light_cost", "light_flash", "light_on", "warp_cells", "warp_cost", "warp_in",
           "warp_spot"]
