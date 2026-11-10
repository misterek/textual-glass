"""The bridge's room, drawn in light: where it goes and how it falls off. Pure numpy, no Textual.

The crew are sprites; the room around them is light at glass's own resolution, in glass's visual language,
and nothing in it is opaque, so the nebula shows through everywhere. The bridge is implied, not painted:

- the **viewscreen** is a glass pane: a luminous rounded frame around the screen sprite's cells, hanging
  from the panel's top edge, with a faint spill of light under it;
- the **rail** in front of the stations is a smooth glowing arc on the stations' ellipse, with a few points
  of light along it;
- the **command well** inside it is two faint dashed rings about the same centre, around the chair: they
  echo the week in review's orbits and give the bridge its shape;
- a **pool of light** behind each console, in its lamp's colour, and one under the chair: the room encodes
  state;
- a faint dotted **path of light** along the floor from each station that is waiting to the chair: who needs
  the captain, drawn into the well.

``shape(layout)`` says where each of them goes, in cells from the deck's top left corner. The light
functions take layer pixel centres (``xs``, ``ys``) and shapes in layer pixels, and return how much light
falls on each pixel (0 to 1). ``bridge.widgets`` wraps them in glass items (``Arc``, ``Pool``, ``Pane``),
each in a box its light ends inside (glass rule 16).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

RGB = tuple[int, int, int]
Box = tuple[int, int, int, int]                   # x, y, w, h in cells, from the deck's top left corner
Ellipse = tuple[float, float, float, float]       # centre x, y and radii x, y
Span = tuple[float, float]                        # parametric degrees along an ellipse (0 right, 90 up)
Glow = tuple[float, float, float, float, float]   # a pool: centre x, y, half width, reach up, reach down
Point = tuple[float, float]
Curve = tuple[Point, Point, Point, Point]         # a cubic Bezier curve's four control points
F32 = np.float32

#: Rows of clear floor between the rail and the nearest station (its sprite and labels).
RAIL_CLEAR = 0.6
#: The well's rings, as fractions of the rail's radius.
RINGS = (0.8, 0.6)
#: Where an arc's lit part ends: this many rows above the deck's bottom edge, and cols inside its sides.
FLOOR_EDGE, SIDE_EDGE = 1.3, 1.0
#: Degrees over which an arc fades out at each end.
ARC_FADE = 14.0
#: Cols of clear floor between a light path and its own station's box (and labels).
PATH_CLEAR = 1.2


@dataclass(frozen=True)
class Layout:
    """Where everything is on the deck, in cells from its top left corner."""

    size: tuple[int, int]                    # the deck, cols x rows
    viewscreen: Box
    stations: tuple[Box, ...]                # each: the sprite at the top (centred), then the label rows
    art: tuple[int, int]                     # a station's sprite, cols x rows
    label_rows: int
    chair: Box                               # the chair's sprite at the top (centred), its label row last
    chair_art: tuple[int, int]
    arc: tuple[float, float, float, float]   # the stations' ellipse: centre x, y and radii, in cells


@dataclass(frozen=True)
class Shape:
    """Where the room's light goes, in cells from the deck's top left corner."""

    rail: Ellipse
    rail_span: Span
    rings: tuple[tuple[Ellipse, Span], ...]  # the well's rings, outermost first (none if there's no room)
    pools: tuple[Glow, ...]                  # one behind each station's console
    chair: Glow                              # the chair's own pool
    pane: Box                                # the viewscreen's cells
    paths: tuple[Curve, ...] = ()            # one from each station's console to the chair (lit while it waits)


# ---------------------------------------------------------------- where --


@lru_cache(maxsize=16)
def shape(layout: Layout) -> Shape:
    """Where the rail, the well's rings, the pools and the pane go for ``layout``."""
    cols, rows = layout.size
    cx, cy, rx, ry = layout.arc
    if rx <= 0 or ry <= 0:  # no arc (a hand-made layout): a circle about the bottom middle
        cx, cy, rx, ry = cols / 2, rows * 1.05, max(1.0, cols / 2), max(1.0, rows * 0.8)
    # the rail: RAIL_CLEAR rows inside every station and its labels. Where the layout fits, that is well
    # outside the chair; in a cramped one it passes behind the chair's sprite rather than through a label.
    nearest = min((_nearest(s, cx, cy, rx, ry) for s in layout.stations), default=1.0)
    rail = max(0.15, nearest - RAIL_CLEAR / ry)
    rail_e = (cx, cy, rx * rail, ry * rail)
    rings = []
    for k in RINGS:
        e = (cx, cy, rx * rail * k, ry * rail * k)
        span = _span(e, cols, rows)
        if span[1] - span[0] >= 50:
            rings.append((e, span))
    pools = tuple(_pool(x + w / 2, y, layout.art, cols) for x, y, w, _h in layout.stations)
    x, y, w, _h = layout.chair
    paths = tuple(_path(s, layout.art, layout.chair, layout.chair_art, rx) for s in layout.stations)
    return Shape(rail_e, _span(rail_e, cols, rows), tuple(rings), pools,
                 _pool(x + w / 2, y, layout.chair_art, cols), layout.viewscreen, paths)


def _pool(mid: float, top: float, art: tuple[int, int], deck_cols: int) -> Glow:
    """The pool of a sprite ``art`` (cols, rows) big, centred at ``mid``, whose top edge is at ``top``: about
    the console's top surface, rising behind the console around the crew member, out past its sides (but not
    past the deck's), and only a little below its foot, where the labels are. It grows more slowly than the
    station does, so on a big window it stays a pool and doesn't become an aura."""
    cols, rows = art
    foot = top + rows
    half = min(1.6 + 0.5 * cols, mid - SIDE_EDGE / 2, deck_cols - SIDE_EDGE / 2 - mid)
    return (mid, foot - 0.22 * rows, max(1.0, half), 0.6 + 0.3 * rows, 0.2 + 0.2 * rows)


def _path(station: Box, art: tuple[int, int], chair: Box, chair_art: tuple[int, int], rx: float) -> Curve:
    """The floor path from a station to the chair. It starts beside the console, at its foot, ``PATH_CLEAR``
    cols past the station's box on the chair's side (the labels below are as wide as the box), and never past
    the chair's middle; it runs on toward the middle, then eases down into the well and into the chair from
    above. The stations at the top come down the gap between them; the ones at the sides cross the rail."""
    x, y, w, _h = station
    cols, rows = art
    mid, foot = x + w / 2, y + rows
    cx = chair[0] + chair[2] / 2
    ccols, crows = chair_art
    side = -1.0 if mid < cx else 1.0  # the station is left (-1) or right (1) of the chair

    def short(px: float) -> float:  # never past the chair's middle
        return min(px, cx - 0.25) if side < 0 else max(px, cx + 0.25)

    inner = x + w if side < 0 else x  # the box's edge on the chair's side
    p0 = (short(inner - side * PATH_CLEAR), foot - 0.3)
    k = min(1.0, abs(p0[0] - cx) / max(1.0, rx))  # 0: straight above the chair, 1: out at its side
    p3 = (cx + side * 0.3 * k * ccols, chair[1] + (0.35 + 0.25 * k) * crows)
    p1 = (short(p0[0] - side * max(1.5, 0.5 * abs(p3[0] - p0[0]))), p0[1])
    p2 = (p3[0], p3[1] - 0.55 * max(0.0, p3[1] - p0[1]))
    return (p0, p1, p2, p3)


def _nearest(box: Box, cx: float, cy: float, rx: float, ry: float) -> float:
    """The smallest radius (in units of the arc's radii) over a box."""
    x, y, w, h = box
    gx, gy = np.meshgrid(np.linspace(x, x + w, 24), np.linspace(y, y + h, 24))
    return float(np.sqrt(((gx - cx) / rx) ** 2 + ((gy - cy) / ry) ** 2).min())


def _farthest(box: Box, cx: float, cy: float, rx: float, ry: float) -> float:
    """The biggest radius over a box (at one of its corners)."""
    x, y, w, h = box
    return max(math.hypot((px - cx) / rx, (py - cy) / ry) for px in (x, x + w) for py in (y, y + h))


def _span(e: Ellipse, cols: int, rows: int) -> Span:
    """The part of the upper half of ``e`` that stays ``FLOOR_EDGE`` rows above the deck's bottom and
    ``SIDE_EDGE`` cols inside its sides, in parametric degrees (empty: (90, 90))."""
    cx, cy, rx, ry = e
    lo = 0.0
    if ry > 0:
        low = (cy - (rows - FLOOR_EDGE)) / ry  # sin of the angle where the ellipse crosses that line
        if low >= 1:
            return (90.0, 90.0)
        lo = max(lo, math.degrees(math.asin(max(-1.0, low))))
    if rx > 0:
        wide = min(cx - SIDE_EDGE, cols - SIDE_EDGE - cx) / rx  # cos of the angle where it crosses a side
        if wide <= -1:
            return (90.0, 90.0)
        if wide < 1:
            lo = max(lo, math.degrees(math.acos(max(-1.0, wide))))
    lo = min(lo, 90.0)
    return (lo, 180.0 - lo)


def arc_points(e: Ellipse, span: Span, n: int = 96) -> tuple[np.ndarray, np.ndarray]:
    """Points along ``e`` over ``span`` (the units of ``e``)."""
    cx, cy, rx, ry = e
    t = np.radians(np.linspace(span[0], span[1], n))
    return cx + rx * np.cos(t), cy - ry * np.sin(t)


# ---------------------------------------------------------------- light --


def smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    """0 at ``e0``, 1 at ``e1`` (either way round), smooth in between (glass's ``render.smoothstep``)."""
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def ellipse_field(xs: np.ndarray, ys: np.ndarray, e: Ellipse) -> tuple[np.ndarray, np.ndarray]:
    """The signed distance to the ellipse ``e`` (negative inside), first order, and each pixel's parametric
    angle in degrees (0 right, 90 up). In the units of ``e``: layer pixels for the items."""
    cx, cy, rx, ry = e
    u, v = (xs - cx) / rx, (ys - cy) / ry
    rho = np.maximum(np.sqrt(u * u + v * v), 1e-6)
    grad = np.sqrt((u / rx) ** 2 + (v / ry) ** 2) / rho
    d = (rho - 1) / np.maximum(grad, 1e-9)
    theta = np.degrees(np.arctan2(-v, u))
    return d.astype(F32), theta.astype(F32)


def along(theta: np.ndarray, span: Span, fade: float = ARC_FADE) -> np.ndarray:
    """1 inside ``span``, easing to 0 at both its ends over ``fade`` degrees."""
    t0, t1 = span
    fade = max(1e-3, min(fade, (t1 - t0) / 2))
    return (smoothstep(t0, t0 + fade, theta) * smoothstep(t1, t1 - fade, theta)).astype(F32)


def line(d: np.ndarray, width: float) -> np.ndarray:
    """An anti-aliased line ``width`` pixels wide where ``d`` is 0."""
    return np.clip(width / 2 + 0.5 - np.abs(d), 0, 1).astype(F32)


def bloom(d: np.ndarray, soft: float, reach: float) -> np.ndarray:
    """Light falling off from a line where ``d`` is 0: ``exp(-|d| / soft)``, eased to exactly 0 at
    ``reach`` pixels (rule 16: it ends where the item says)."""
    ad = np.abs(d)
    return (np.exp(-ad / max(soft, 1e-3)) * smoothstep(reach, reach * 0.3, ad)).astype(F32)


def inner(d: np.ndarray, depth: float) -> np.ndarray:
    """A glow just inside an arc (``d`` < 0), strongest at the line and gone ``depth`` pixels in."""
    return (smoothstep(-depth, 0.0, d) * (d < 0)).astype(F32)


def dashes(theta: np.ndarray, e: Ellipse, span: Span, period: float, duty: float = 0.5) -> np.ndarray:
    """Coverage of evenly spaced dashes along ``e`` about ``period`` pixels apart (a whole number of them
    over ``span``), anti-aliased at their ends."""
    rx, ry = e[2], e[3]
    radius = (rx + ry) / 2  # near enough: the arcs are about circular on screen
    length = math.radians(span[1] - span[0]) * radius
    if length <= 0 or period <= 0:
        return np.ones_like(theta)
    count = max(1, round(length / period))
    p = length / count
    s = (np.radians(theta) - math.radians(span[0])) * radius
    pos = np.mod(s, p)
    on = duty * p

    def edge(q: np.ndarray) -> np.ndarray:
        return np.clip(np.minimum(q, on - q) + 0.5, 0, 1)

    return np.maximum(edge(pos), edge(pos - p)).astype(F32)


def bead(xs: np.ndarray, ys: np.ndarray, x: float, y: float, radius: float,
         reach: float) -> tuple[np.ndarray, np.ndarray]:
    """A small point of light at (``x``, ``y``): a core ``radius`` pixels round, and a glow that ends at
    ``reach`` pixels."""
    d = np.sqrt((xs - x) ** 2 + (ys - y) ** 2)
    core = np.clip(radius + 0.5 - d, 0, 1)
    glow = np.exp(-d / max(1e-3, reach * 0.3)) * smoothstep(reach, reach * 0.35, d)
    return core.astype(F32), glow.astype(F32)


def moved(curve: Curve, dx: float, dy: float, sx: float = 1.0, sy: float = 1.0) -> Curve:
    """``curve`` moved by (``dx``, ``dy``), then scaled by (``sx``, ``sy``)."""
    a, b, c, d = (((x + dx) * sx, (y + dy) * sy) for x, y in curve)
    return (a, b, c, d)


def curve_points(curve: Curve, n: int = 160) -> np.ndarray:
    """``n`` points along a cubic Bezier curve, as an (n, 2) array (the curve's units)."""
    t = np.linspace(0.0, 1.0, n)[:, None]
    p0, p1, p2, p3 = (np.asarray(p, np.float64) for p in curve)
    return ((1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t ** 2 * p2 + t ** 3 * p3)


def dots(curve: Curve, spacing: float) -> list[tuple[float, float, float]]:
    """Points of light every ``spacing`` along ``curve`` (its units), each with a brightness that rises from
    0 at the start and falls back to 0 at the end, so the path comes out of a pool and fades into another."""
    pts = curve_points(curve)
    seg = np.hypot(*np.diff(pts, axis=0).T)
    along = np.concatenate([[0.0], np.cumsum(seg)])
    length = float(along[-1])
    if length <= spacing or spacing <= 0:
        return []
    count = int(length // spacing)
    at = (np.arange(count) + 0.5) * length / count
    xs, ys = np.interp(at, along, pts[:, 0]), np.interp(at, along, pts[:, 1])
    u = at / length
    weight = smoothstep(0.0, 0.22, u) * smoothstep(1.0, 0.7, u)
    return [(float(x), float(y), float(w)) for x, y, w in zip(xs, ys, weight, strict=True) if w > 0.01]


def pool(xs: np.ndarray, ys: np.ndarray, glow: Glow) -> np.ndarray:
    """A soft pool of light, brightest in the middle and exactly 0 at its edge: half an ellipse above its
    centre and a flatter half below it."""
    cx, cy, rx, up, down = glow
    ry = np.where(ys < cy, up, down)
    r2 = ((xs - cx) / rx) ** 2 + ((ys - cy) / ry) ** 2
    return (np.clip(1 - r2, 0, 1) ** 2).astype(F32)


def sd_rrect(xs: np.ndarray, ys: np.ndarray, cx: float, cy: float, hw: float, hh: float, r: float) -> np.ndarray:
    """Signed distance to a rounded rectangle, negative inside (glass's ``render.sd_rrect``)."""
    qx = np.abs(xs - cx) - (hw - r)
    qy = np.abs(ys - cy) - (hh - r)
    return (np.sqrt(np.maximum(qx, 0) ** 2 + np.maximum(qy, 0) ** 2) + np.minimum(np.maximum(qx, qy), 0) - r)


@dataclass(frozen=True)
class PaneGeometry:
    """The pane's frame around a sprite's box, in layer pixels."""

    left: float
    right: float
    top: float      # where the sprite starts: the frame fades in below it
    bottom: float   # the frame's bottom line
    radius: float
    width: float    # the line
    reach: float    # the halo outside it
    spill: float    # the light under it


def pane(xs: np.ndarray, ys: np.ndarray, p: PaneGeometry) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The viewscreen's glass frame: its line, the halo outside it, and the spill of light under it. The
    frame is open at the top: its sides fade in from the sprite's top edge, so it hangs from the panel's
    border, and it is never drawn over the title there."""
    hw = (p.right - p.left) / 2
    top = p.top - 3 * p.radius  # the frame's top corners lie above the deck, where nothing is drawn
    hh = (p.bottom - top) / 2
    d = sd_rrect(xs, ys, p.left + hw, top + hh, hw, hh, p.radius)
    hang = smoothstep(p.top, p.top + 3.2 * p.radius, ys)
    edge = line(d, p.width) * hang
    out = np.maximum(d, 0)
    halo = (np.exp(-out / max(1e-3, p.reach * 0.3)) * smoothstep(p.reach, p.reach * 0.3, out) * (d > 0) * hang)
    below = (ys - p.bottom) / max(1e-3, p.spill)
    u = np.clip((xs - p.left) / max(1.0, p.right - p.left), 0, 1)
    spill = (np.clip(1 - below, 0, 1) ** 2 * smoothstep(0.0, 0.12, below)
             * np.clip(np.sin(u * math.pi), 0, 1) ** 1.5)
    return edge.astype(F32), halo.astype(F32), spill.astype(F32)
