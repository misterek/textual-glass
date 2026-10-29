"""Ships for the space scenes, drawn from signed-distance parts. Pure numpy, no
Textual imports. Exploratory.

A ship is a list of :class:`Part`\\ s in ship units: ``u`` runs from the tail
(-0.5) to the nose (+0.5), ``v`` across. The painter turns it to a heading,
lights its hull from the scene's sun (a bevel worked out from the hull's
distance field), outlines it so it reads over a bright planet, and adds its
lamps and the flickering plume of its engines in the colour of its kind.
A ship's heading is fixed for its flight, so it is drawn once per flight.

Three families, one per look: haulers (``harbour``), landers (``nightfall``)
and probes (``halo``), each with a design per kind of work.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from ..render import F32, Array, smoothstep
from . import Stage
from ._paint import (
    WHITE,
    Paint,
    Sprite,
    blend,
    cover,
    ink,
    sd_box,
    sd_capsule,
    sd_circle,
    sd_ellipse,
    sd_poly,
    soft_glow,
)

Shape = Callable[[Array, Array], Array]


@dataclass(frozen=True)
class Part:
    """One piece of a ship: its distance field (in ship units) and what it is made of: ``hull``,
    ``panel`` (darker plating), ``trim`` (painted in the kind's colour), ``glass``, ``glow`` (a light in
    the kind's colour that is part of the body, like a lit container) or ``cut`` (a dark slot)."""

    shape: Shape
    material: str = "hull"


@dataclass(frozen=True)
class Design:
    parts: tuple[Part, ...]
    #: Engines: (u, v, radius) of each nozzle, at the tail.
    engines: tuple[tuple[float, float, float], ...]
    #: Lamps: (u, v, size) of small lights in the kind's colour.
    lamps: tuple[tuple[float, float, float], ...] = ()
    #: Length, as a share of the family's standard ship.
    size: float = 1.0
    #: How long the plume reaches behind the nozzles (ship lengths).
    plume: float = 0.42
    #: The hull's extent across (ship units), for the canvas.
    span: float = 0.3
    name: str = ""
    extra: dict[str, float] = field(default_factory=dict)


def _box(cu: float, cv: float, hu: float, hv: float, r: float = 0.0) -> Shape:
    return lambda u, v: sd_box(u, v, cu, cv, hu, hv, r)


def _poly(*pts: tuple[float, float]) -> Shape:
    return lambda u, v: sd_poly(u, v, pts)


def _mirror(*pts: tuple[float, float]) -> Shape:
    """A polygon symmetric about the ship's axis, from its points on one side (nose to tail)."""
    full = list(pts) + [(u, -v) for u, v in reversed(pts)]
    return lambda u, v: sd_poly(u, v, full)


def _pair(*pts: tuple[float, float]) -> Shape:
    """A polygon on one side of the axis and its mirror image on the other."""
    a, b = list(pts), [(u, -v) for u, v in pts]
    return lambda u, v: np.minimum(sd_poly(u, v, a), sd_poly(u, v, b))


def _ellipse(cu: float, cv: float, ru: float, rv: float) -> Shape:
    return lambda u, v: sd_ellipse(u, v, cu, cv, ru, rv)


def _capsule(u0: float, v0: float, u1: float, v1: float, r: float) -> Shape:
    return lambda u, v: sd_capsule(u, v, u0, v0, u1, v1, r)


def _circle(cu: float, cv: float, r: float) -> Shape:
    return lambda u, v: sd_circle(u, v, cu, cv, r)


def _ring(cu: float, cv: float, ru: float, rv: float, width: float) -> Shape:
    return lambda u, v: np.abs(sd_ellipse(u, v, cu, cv, ru, rv)) - width


def _both(shape: Callable[[float], Shape], v: float) -> Shape:
    """A part and its mirror image across the axis (``shape(v)`` and ``shape(-v)``)."""
    a, b = shape(v), shape(-v)
    return lambda u, w: np.minimum(a(u, w), b(u, w))


def _pods(cu: float) -> Shape:
    """A pair of cargo pods either side of a barge's spine, at ``cu``."""
    return _both(lambda s: _box(cu, s, 0.075, 0.07, 0.022), 0.13)


def _dish(cu: float, ru: float, rv: float) -> Shape:
    """A dish seen side on: an ellipse with a bite out of its front."""
    return lambda u, v: np.maximum(sd_ellipse(u, v, cu, 0.0, ru, rv), -sd_ellipse(u, v, cu + 0.07, 0.0, ru - 0.01,
                                                                                  rv - 0.03))


# ============================================================== families ==


def _haulers() -> dict[str, Design]:
    """Boxy working ships for the harbour: plated hulls, cargo in the kind's colour."""
    return {
        "merged": Design((
            Part(_mirror((0.5, 0.0), (0.36, 0.07), (-0.42, 0.07), (-0.47, 0.05))),
            Part(_box(-0.36, 0.0, 0.08, 0.11, 0.025), "panel"),
            *(Part(_pods(cu), "trim") for cu in (-0.17, 0.02, 0.2)),
            Part(_box(-0.33, 0.0, 0.025, 0.07), "glass"),
            Part(_box(0.04, 0.0, 0.33, 0.012), "cut"),
        ), engines=((-0.47, 0.045, 0.035), (-0.47, -0.045, 0.035)),
            lamps=((0.47, 0.0, 0.03), (-0.17, 0.2, 0.025), (0.2, -0.2, 0.025)), span=0.22, name="barge"),
        "review": Design((
            Part(_capsule(-0.12, 0.17, 0.27, 0.06, 0.022), "panel"),
            Part(_capsule(-0.12, -0.17, 0.27, -0.06, 0.022), "panel"),
            Part(_box(-0.14, 0.0, 0.22, 0.15, 0.07)),
            Part(_box(-0.06, 0.0, 0.07, 0.15, 0.03), "panel"),
            Part(_ring(0.3, 0.0, 0.06, 0.25, 0.028), "trim"),
            Part(_ellipse(0.02, 0.0, 0.06, 0.08), "glass"),
        ), engines=((-0.37, 0.0, 0.07),), lamps=((0.3, 0.25, 0.03), (0.3, -0.25, 0.03), (0.36, 0.0, 0.035)),
            size=0.86, span=0.3, name="tug"),
        "deploy": Design((
            Part(_box(0.0, 0.0, 0.34, 0.025), "panel"),
            Part(_mirror((-0.18, 0.06), (-0.26, 0.11), (-0.46, 0.11), (-0.48, 0.06))),
            Part(_box(-0.22, 0.0, 0.03, 0.09), "glass"),
            Part(_box(0.2, 0.0, 0.2, 0.17, 0.035), "glow"),
            Part(_box(0.2, 0.0, 0.205, 0.008), "cut"),
            Part(_box(0.2, 0.0, 0.008, 0.175), "cut"),
            Part(_both(lambda s: _box(0.2, s, 0.22, 0.018, 0.01), 0.17), "panel"),
        ), engines=((-0.48, 0.06, 0.035), (-0.48, -0.06, 0.035)), lamps=((-0.3, 0.12, 0.022), (-0.3, -0.12, 0.022)),
            span=0.2, name="hauler"),
        "ticket": Design((
            Part(_mirror((0.5, 0.0), (0.05, 0.1), (-0.3, 0.2), (-0.45, 0.17), (-0.4, 0.06))),
            Part(_mirror((0.16, 0.0), (0.02, 0.05), (-0.12, 0.05))),
            Part(_mirror((0.12, 0.0), (0.03, 0.03), (-0.08, 0.03)), "glass"),
            Part(_both(lambda s: _capsule(-0.05, s * 0.6, -0.36, s, 0.018), 0.16), "trim"),
        ), engines=((-0.43, 0.0, 0.05),), lamps=((-0.42, 0.18, 0.03), (-0.42, -0.18, 0.03)), size=0.7,
            span=0.21, name="skiff"),
        "release": Design((
            Part(_mirror((0.5, 0.0), (0.38, 0.05), (0.1, 0.09), (-0.12, 0.13), (-0.44, 0.13), (-0.48, 0.09))),
            Part(_mirror((0.25, 0.0), (0.12, 0.04), (-0.4, 0.04)), "panel"),
            Part(_box(-0.02, 0.0, 0.3, 0.008), "glow"),
            Part(_box(-0.3, 0.0, 0.07, 0.06, 0.02)),
            Part(_box(-0.29, 0.0, 0.02, 0.045), "glass"),
            Part(_both(lambda s: _box(-0.2, s, 0.2, 0.025, 0.01), 0.105), "trim"),
        ), engines=((-0.48, 0.08, 0.03), (-0.48, 0.0, 0.035), (-0.48, -0.08, 0.03)),
            lamps=tuple((u, s * 0.135, 0.016) for u in (-0.38, -0.28, -0.18, -0.08, 0.02, 0.12) for s in (1, -1)),
            size=1.9, span=0.15, plume=0.3, name="carrier"),
    }


def _landers() -> dict[str, Design]:
    """Sleek ships for the world at night: smooth hulls, heat shields, wings."""
    return {
        "merged": Design((
            Part(_mirror((0.42, 0.02), (0.0, 0.1), (-0.32, 0.36), (-0.44, 0.36), (-0.4, 0.08))),
            Part(_pair((-0.2, 0.22), (-0.31, 0.345), (-0.42, 0.345), (-0.37, 0.22)), "trim"),
            Part(_ellipse(0.02, 0.0, 0.48, 0.075)),
            Part(_ellipse(0.27, 0.0, 0.1, 0.035), "glass"),
        ), engines=((-0.44, 0.0, 0.05), (-0.4, 0.13, 0.025), (-0.4, -0.13, 0.025)),
            lamps=((-0.42, 0.36, 0.025), (-0.42, -0.36, 0.025)), span=0.38, name="shuttle"),
        "review": Design((
            Part(_mirror((0.35, 0.0), (0.34, 0.12), (0.29, 0.19), (-0.32, 0.08), (-0.37, 0.0))),
            Part(_ellipse(0.32, 0.0, 0.06, 0.2), "panel"),
            Part(_box(0.04, 0.0, 0.035, 0.15), "trim"),
            Part(_ellipse(0.18, 0.0, 0.04, 0.05), "glass"),
        ), engines=((-0.38, 0.0, 0.06),), lamps=((0.04, 0.17, 0.03), (0.04, -0.17, 0.03)), size=0.82, span=0.22,
            name="capsule"),
        "deploy": Design((
            Part(_both(lambda s: _capsule(-0.1, 0.1 * s, -0.42, 0.3 * s, 0.02), 1), "panel"),
            Part(_both(lambda s: _capsule(0.15, 0.1 * s, 0.32, 0.3 * s, 0.02), 1), "panel"),
            Part(_ellipse(-0.05, 0.0, 0.36, 0.14)),
            Part(_box(0.1, 0.0, 0.13, 0.1, 0.04), "glow"),
            Part(_ellipse(-0.24, 0.0, 0.05, 0.07), "glass"),
            Part(_both(lambda s: _circle(-0.42, 0.3 * s, 0.035), 1), "trim"),
            Part(_both(lambda s: _circle(0.32, 0.3 * s, 0.035), 1), "trim"),
        ), engines=((-0.42, 0.0, 0.055),), lamps=(), span=0.34, name="lander"),
        "ticket": Design((
            Part(_mirror((0.48, 0.0), (0.3, 0.08), (-0.2, 0.15), (-0.42, 0.12), (-0.44, 0.0))),
            Part(_ellipse(0.18, 0.0, 0.12, 0.045), "glass"),
            Part(_both(lambda s: _capsule(0.0, 0.11 * s, -0.36, 0.12 * s, 0.022), 1), "trim"),
        ), engines=((-0.44, 0.0, 0.05),), lamps=(), size=0.68, span=0.16, name="pod"),
        "release": Design((
            Part(_mirror((0.2, 0.05), (-0.18, 0.4), (-0.3, 0.4), (-0.26, 0.05))),
            Part(_both(lambda s: _capsule(-0.14, 0.36 * s, -0.3, 0.4 * s, 0.018), 1), "trim"),
            Part(_mirror((0.5, 0.0), (0.4, 0.05), (-0.46, 0.06), (-0.48, 0.0))),
            Part(_mirror((-0.25, 0.04), (-0.4, 0.16), (-0.47, 0.16), (-0.44, 0.04))),
            Part(_box(0.0, 0.0, 0.36, 0.01), "glow"),
            Part(_ellipse(0.39, 0.0, 0.06, 0.025), "glass"),
        ), engines=((-0.48, 0.0, 0.04), (-0.46, 0.12, 0.025), (-0.46, -0.12, 0.025)),
            lamps=((-0.28, 0.4, 0.02), (-0.28, -0.4, 0.02), (0.48, 0.0, 0.02)), size=1.8, span=0.4, plume=0.32,
            name="liner"),
    }


def _probes() -> dict[str, Design]:
    """Spindly ships for the eclipse: masts, dishes and solar wings; they unfold where they park."""
    return {
        "merged": Design((
            Part(_capsule(-0.4, 0.0, 0.3, 0.0, 0.02), "panel"),
            Part(_mirror((0.5, 0.0), (0.26, 0.09), (0.18, 0.09), (0.22, 0.0))),
            Part(_mirror((-0.2, 0.0), (-0.36, 0.16), (-0.44, 0.16), (-0.4, 0.0))),
            Part(_circle(0.33, 0.0, 0.035), "trim"),
            Part(_both(lambda s: _capsule(-0.37, 0.15 * s, -0.42, 0.15 * s, 0.025), 1), "trim"),
        ), engines=((-0.44, 0.0, 0.04),), lamps=((0.33, 0.0, 0.04),), span=0.17, name="dart"),
        "review": Design((
            Part(_capsule(-0.36, 0.0, 0.18, 0.0, 0.025), "panel"),
            Part(_dish(0.2, 0.14, 0.3)),
            Part(_capsule(0.2, 0.0, 0.42, 0.0, 0.015), "panel"),
            Part(_circle(0.42, 0.0, 0.03), "trim"),
            Part(_box(-0.25, 0.0, 0.1, 0.07, 0.02)),
            Part(_box(-0.25, 0.0, 0.02, 0.07), "trim"),
        ), engines=((-0.36, 0.0, 0.045),), lamps=((0.42, 0.0, 0.035),), size=0.95, span=0.31, name="dish"),
        "deploy": Design((
            Part(_both(lambda s: _box(-0.02, 0.27 * s, 0.11, 0.17, 0.01), 1), "trim"),
            Part(_both(lambda s: _box(-0.02, 0.27 * s, 0.112, 0.006), 1), "cut"),
            Part(_both(lambda s: _box(-0.02, 0.2 * s, 0.006, 0.1), 1), "cut"),
            Part(_both(lambda s: _box(-0.02, 0.33 * s, 0.006, 0.1), 1), "cut"),
            Part(_box(-0.02, 0.0, 0.2, 0.012), "panel"),
            Part(_box(0.0, 0.0, 0.16, 0.08, 0.02)),
            Part(_circle(0.2, 0.0, 0.04), "panel"),
        ), engines=((-0.18, 0.0, 0.04),), lamps=((0.21, 0.0, 0.03),), span=0.46, name="comsat"),
        "ticket": Design((
            Part(_both(lambda s: _box(0.0, 0.2 * s, 0.1, 0.09, 0.01), 1), "trim"),
            Part(_both(lambda s: _box(0.0, 0.2 * s, 0.006, 0.09), 1), "cut"),
            Part(_box(0.0, 0.0, 0.13, 0.13, 0.02)),
            Part(_box(0.0, 0.0, 0.07, 0.07, 0.01), "panel"),
        ), engines=((-0.14, 0.0, 0.04),), lamps=((0.12, 0.0, 0.025),), size=0.62, span=0.3, name="cubesat"),
        "release": Design((
            Part(_both(lambda s: _box(0.0, 0.3 * s, 0.07, 0.15, 0.01), 1), "trim"),
            Part(_both(lambda s: _box(0.3 * s, 0.0, 0.15, 0.07, 0.01), 1), "trim"),
            Part(_ring(0.0, 0.0, 0.17, 0.17, 0.02), "panel"),
            Part(_both(lambda s: _box(0.0, 0.3 * s, 0.072, 0.005), 1), "cut"),
            Part(_both(lambda s: _box(0.3 * s, 0.0, 0.005, 0.072), 1), "cut"),
            Part(_circle(0.0, 0.0, 0.1)),
            Part(_circle(0.0, 0.0, 0.05), "glow"),
        ), engines=((-0.47, 0.0, 0.035),), lamps=tuple((0.17 * math.cos(a), 0.17 * math.sin(a), 0.022)
                                                     for a in np.linspace(0, 2 * math.pi, 8, endpoint=False)),
            size=1.5, span=0.47, plume=0.25, name="station"),
    }


FAMILIES: dict[str, dict[str, Design]] = {"harbour": _haulers(), "nightfall": _landers(), "halo": _probes()}


# ================================================================ painter ==


@dataclass(frozen=True)
class Look:
    """How a family is lit and coloured on a stage: the sun's direction on screen (x right, y down, z out)
    and the kinds' colours."""

    sun: tuple[float, float, float]
    colours: dict[str, Array]


def hull_inks(stage: Stage) -> dict[str, Array]:
    bg, fg = ink(stage, "background"), ink(stage, "foreground")
    primary = ink(stage, "primary")
    if stage.dark:
        return {"hull": blend(fg, WHITE, 0.15) * 0.9, "panel": blend(bg, fg, 0.42), "glass": blend(bg, primary, 0.55),
                "cut": blend(bg, fg, 0.15), "outline": bg * 0.5}
    return {"hull": blend(bg, WHITE, 0.6), "panel": blend(bg, fg, 0.3), "glass": blend(primary, fg, 0.4),
            "cut": blend(bg, fg, 0.55), "outline": fg * 0.85}


def standard_length(stage: Stage) -> float:
    """A standard ship's length in terminal pixels: about one and three quarter rows."""
    return 1.75 * stage.cell[1]


def ship_canvas(design: Design, length: float, heading: float, glow: float = 0.0) -> tuple[int, int, float, float]:
    """A canvas just big enough for the ship and its plume at ``heading``: its size, and where the ship's
    centre is on it."""
    back = 0.5 + design.plume
    c, s = math.cos(heading), math.sin(heading)
    pts = [(u * c - v * s, u * s + v * c) for u in (0.5, -back) for v in (design.span, -design.span)]
    pad = 3.0 + glow
    x0, x1 = min(p[0] for p in pts) * length - pad, max(p[0] for p in pts) * length + pad
    y0, y1 = min(p[1] for p in pts) * length - pad, max(p[1] for p in pts) * length + pad
    w, h = math.ceil(x1 - x0), math.ceil(y1 - y0)
    return w, h, -x0, -y0


def paint_ship(paint: Paint, design: Design, colour: Array, cx: float, cy: float, length: float,
               heading: float, sun: tuple[float, float, float], inks: dict[str, Array], flame: float = 1.0,
               fade: float = 1.0, scale: float = 1.0, heat: float = 0.0, unfold: float = 1.0) -> None:
    """Draw ``design`` centred at (cx, cy), ``length`` pixels long, pointing along ``heading`` (radians, y
    down). ``flame`` is how bright its plume burns (0 for none), ``fade`` how opaque it is, ``scale`` how
    big (a ship landing far below shrinks), ``heat`` a glow of re-entry over its nose, and ``unfold``
    how far its trim (wings, panels) has opened, from 0 to 1."""
    L = length * scale
    if L < 1.0 or fade <= 0.003:
        return
    c, s = math.cos(heading), math.sin(heading)
    dx, dy = paint.xs - cx, paint.ys - cy
    u = (dx * c + dy * s) / L
    v = (-dx * s + dy * c) / L
    u, v = np.broadcast_arrays(u, v)
    px = 1.0 / L  # one pixel, in ship units
    dists: list[tuple[Array, str]] = []
    hull = np.full(u.shape, np.inf, F32)
    for part in design.parts:
        if part.material == "trim" and unfold < 1.0:
            d = part.shape(u, v / max(0.05, unfold)) * max(0.05, min(1.0, unfold))
        else:
            d = part.shape(u, v)
        dists.append((d, part.material))
        if part.material != "cut":
            hull = np.minimum(hull, d)
    # the bevel: a height that rises inwards from the hull's edge, lit from the sun
    bevel = smoothstep(0.0, 0.07, -hull)
    gy, gx = np.gradient(bevel)
    lx, ly, lz = sun
    k = 0.9 * L * 0.07
    shade = (-gx * k * lx - gy * k * ly + lz) / np.sqrt(1 + (gx * k) ** 2 + (gy * k) ** 2)
    shade = np.clip(0.45 + 0.75 * shade, 0.25, 1.25)[..., None]
    # a dark outline behind it all, so it reads over a bright planet or ring
    paint.over(inks["outline"], cover((hull - 1.1 * px) * L) * 0.75 * fade)
    for d, material in dists:
        cov = cover(d * L) * fade
        if material == "trim":
            paint.over(blend(colour, WHITE, 0.15) * shade * 0.95, cov)
        elif material == "glow":
            paint.over(blend(colour, WHITE, 0.35), cov)
        elif material == "cut":
            paint.over(inks["cut"], cov * 0.85)
        elif material == "glass":
            paint.over(inks["glass"] * shade, cov)
            paint.shine(WHITE, cov * smoothstep(0.2, 0.9, shade[..., 0] - 0.5) * 0.25)
        else:
            paint.over(inks[material] * shade, cov)
    # lights: lamps and glowing parts, in the kind's colour
    for lu, lv, size in design.lamps:
        lv = lv * (unfold if abs(lv) > 0.12 else 1.0)
        r = np.sqrt((u - lu) ** 2 + (v - lv) ** 2) * L
        rad = max(1.6, size * L * 2.2)
        paint.shine(colour, soft_glow(r, rad) * 0.9 * fade)
        paint.shine(blend(colour, WHITE, 0.7), soft_glow(r, max(1.0, size * L * 0.9)) * 0.8 * fade)
    for d, material in dists:
        if material == "glow":
            paint.shine(colour, soft_glow(np.maximum(d, 0) * L, 0.12 * L + 2.0) * 0.45 * fade)
    # the plume: white at the nozzle, the kind's colour along it, flickering
    if flame > 0:
        for eu, ev, er in design.engines:
            along = (eu - u) / design.plume
            across = np.abs(v - ev) / (er * (1.0 + 0.6 * along))
            plume = np.exp(-across * across) * np.clip(1 - along, 0, 1) ** 1.6 * smoothstep(-0.12, 0.05, along)
            core = np.exp(-(across / 0.5) ** 2) * np.clip(1 - along * 2.5, 0, 1) ** 2 * smoothstep(-0.1, 0.03, along)
            paint.shine(colour, plume * 1.2 * flame * fade)
            paint.shine(WHITE if paint.stage.dark else blend(colour, WHITE, 0.3), core * 1.1 * flame * fade)
    if heat > 0:  # re-entry: a hot sheath over the nose, streaming back
        along = 0.5 - u
        sheath = np.exp(-np.maximum(hull, 0) * L / (0.08 * L + 1.5)) * np.exp(-along * 2.2) * (hull > -0.02)
        paint.shine(blend(colour, WHITE, 0.5), sheath * heat * 1.3 * fade)
        tail = np.exp(-(v / (0.25 + 0.4 * np.clip(-u, 0, 2))) ** 2) * smoothstep(0.5, -0.2, u) * np.exp(
            -np.clip(-u - 0.3, 0, None) * 2.5)
        paint.shine(colour, tail * heat * 0.5 * fade)


def ship_sprite(stage: Stage, family: str, kind: str, heading: float, colour: Array,
                sun: tuple[float, float, float], frames: int = 3, gap: int = 70) -> Sprite:
    """A ship in flight at ``heading``: a few frames of its plume flickering, which the terminal loops.
    ``offset`` is where the ship's centre is from the picture's centre."""
    design = FAMILIES[family][kind]
    L = standard_length(stage) * design.size
    w, h, cx, cy = ship_canvas(design, L, heading)
    inks = hull_inks(stage)
    rnd = np.random.default_rng(zlib.crc32(f"{family}-{kind}".encode()) + stage.seed)
    flames = 0.82 + 0.28 * rnd.random(frames)
    out = []
    for f in range(frames):
        paint = Paint(w, h, stage.dark, margin=2.0)
        paint_ship(paint, design, colour, cx, cy, L, heading, sun, inks, flame=float(flames[f]))
        out.append(paint.frame())
    return Sprite(out, w, h, [gap], True, offset=(cx - w / 2, cy - h / 2), name=f"{family}-{kind}")


__all__ = ["FAMILIES", "Design", "Look", "Part", "hull_inks", "paint_ship", "ship_canvas", "ship_sprite",
           "standard_length"]
