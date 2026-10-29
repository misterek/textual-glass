"""One-shot effects for the space scenes: the lights the week leaves, how they
come on, ships warping in and ships arriving. Pure numpy, no Textual imports.
Exploratory.

Every effect is a :class:`Sprite` that glass steps through once. The lights
hold their last frame, which is the light itself; the others end empty, so
nothing pops. One shape per kind of work, the same in every look, so a page
can carry one legend:

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


def light_on(stage: Stage, kind: str, colour: Array, style: str, seed: int = 0,
             squash: tuple[float, float] = (1.0, 0.0)) -> Sprite:
    """A light coming on: a white flash and a ring in the kind's colour, then the light settles. Its last
    frame is the light, which stays for the rest of the show and after it.

    ``style``: ``bead`` (a berth on the harbour's rail: the flash runs along the rail), ``city`` (lights
    spread round it like a town, and stay), ``star`` (a twinkle that shrinks to the star's own spikes)."""
    size = glyph_size(stage) * (1.25 if kind == "release" else 1.0) * (1.2 if style == "bead" else 1.0)
    reach = max(glyph_reach(kind, size), 3.0 * size) + 2.0
    side = math.ceil(2 * reach)
    w = h = side + (side % 2)
    cx = cy = w / 2
    rnd = np.random.default_rng(seed)
    n_town = {"merged": 5, "review": 4, "deploy": 5, "ticket": 3, "release": 7}[kind] if style == "city" else 0
    town_a = rnd.uniform(0, TAU, n_town)
    town_r = rnd.uniform(0.9, 2.0, n_town) * size
    town_k = rnd.uniform(0.25, 0.6, n_town)
    ratio, angle = squash
    times = [0.0, 0.06, 0.13, 0.22, 0.33, 0.47, 0.62, 0.8]
    out: list[bytes] = []
    for t in times:
        e = t / times[-1]
        paint = Paint(w, h, stage.dark, margin=2.0)
        flash = bump(e, 0.12, 0.6)
        ring_r = size * (0.6 + 2.1 * ease_out(e / 0.75))
        r = np.sqrt((paint.xs - cx) ** 2 + (paint.ys - cy) ** 2)
        ring = np.exp(-(((r - ring_r) / (0.22 * size + 0.6)) ** 2)) * smoothstep(reach, reach - 2.5, r)
        paint.shine(colour, ring * (1 - ramp(0.2, 0.75, e)) * 0.8)
        hot = WHITE if stage.dark else colour * 0.8
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
        out.append(paint.frame())
    gaps = [max(16, round((b - a) * 1000)) for a, b in zip(times, times[1:], strict=False)] + [1000]
    return Sprite(out, w, h, gaps, False, name=f"light-{kind}")


def final_light(sprite: Sprite) -> Sprite:
    return Sprite([sprite.frames[-1]], sprite.width, sprite.height, [1000], False, name=sprite.name)


# =============================================================== warp in ==


def warp_in(stage: Stage, heading: float, colour: Array, style: str, big: bool = False,
            max_px: int = 6_000) -> Sprite:
    """A ship arriving out of nowhere at the centre of the picture, heading along ``heading``. The ship
    itself appears at about 120 ms, in the flash. It is soft light, so it is drawn small (at most
    ``max_px`` pixels a frame) and stretched over whole cells: place it, and start the ship, on
    :func:`warp_spot`. Its last frame is (nearly) empty.

    ``style``: ``gate`` (a ring opens across its path and closes behind it), ``streak`` (it drops out of a
    streak of light), ``fold`` (space twinkles and folds open round it)."""
    cols, rows = warp_cells(stage, style, big)
    w, h, sc = stretched_size(stage, cols, rows, max_px)
    L = standard_length(stage) * (1.8 if big else 1.0) * sc
    reach = min(w, h) / 2 - 2
    cx, cy = w / 2, h / 2
    c, s = math.cos(heading), math.sin(heading)
    hot = WHITE if stage.dark else colour * 0.75
    bright = blend(colour, WHITE, 0.4)
    times = [0.0, 0.05, 0.1, 0.16, 0.23, 0.31, 0.4, 0.5, 0.6]
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
    if style == "dock":
        times = [0.0, 0.07, 0.14, 0.22, 0.31, 0.4, 0.5]
        light_at = 0.3
    elif style == "land":
        times = [0.0, 0.1, 0.2, 0.3, 0.4, 0.49, 0.58, 0.7, 0.84, 1.0]
        light_at = 0.6
    else:
        times = [0.0, 0.08, 0.17, 0.27, 0.38, 0.5, 0.62, 0.76, 0.9]
        light_at = 0.62
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


__all__ = ["WARP_LEAD", "arrive", "final_light", "folded_ship", "glyph", "glyph_reach", "glyph_size", "light_on",
           "warp_cells", "warp_in", "warp_spot"]
