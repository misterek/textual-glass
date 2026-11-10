"""The smooth style's furniture and effects: the station's console and chair, the ensigns, the little effects
(a "!", sleepy z's, steam, sparks, smoke, motion lines, a sparkle) and the captain's chair. Pure numpy.

Station coordinates are design units in the 116 x 100 box (``_figure``), y down.
"""

from __future__ import annotations

import math

import numpy as np

from . import _figure as fig
from ._paint import (
    F32,
    WHITE,
    Array,
    Canvas,
    Light,
    Region,
    fill,
    mix,
    normals,
    sd_arc,
    sd_box,
    sd_capsule,
    sd_circle,
    sd_ellipse,
    sd_poly,
    smoothstep,
    soft_glow,
    star,
    union,
)

#: Where the ensigns stand: their feet, on the floor beside the console, then in front of its corner.
ENSIGN_SLOTS = ((107.0, 99.4), (9.0, 99.4), (26.0, 99.4))
ENSIGN_SIZE = 1.2
LIGHTS = ("light1", "light2", "light3", "light4")


def draw_chair_back(cv: Canvas, light: Light, colors: dict[str, Array]) -> None:
    """The crew member's chair, behind them: a wide padded back, most of it hidden behind the member, its
    wings showing either side of the head; on its own (an empty station), a chair with a headrest."""
    reg = cv.region(24, 24, 92, fig.DESK + 4)
    if reg is None:
        return
    x, y = reg.x, reg.y
    back = union(sd_box(x, y, fig.CX, 57.0, 29.0, 22.0, 12.0), sd_box(x, y, fig.CX, 31.0, 13.0, 5.5, 5.0),
                 sd_box(x, y, fig.CX, 36.0, 3.5, 4.0, 1.0))
    seam = np.minimum(np.abs(np.abs(reg.full(x - fig.CX)) - 10.0), 9.0)
    tone = colors["chair"] * 1.15 * (0.86 + 0.14 * smoothstep(0.3, 1.3, seam))[..., None]
    fill(cv, light, reg, back, tone, 7.0, wrap=0.3, gloss=0.15)


#: The console: its top edge (``fig.DESK``) to the floor, and its width at the top and at the floor.
CONSOLE_TOP_X = (17.0, 99.0)
CONSOLE_BASE_X = (21.0, 95.0)
CONSOLE_BASE = 99.4
#: The bottom of the console's top plate: below this line the console's front hides what sits behind it.
PLATE_BOTTOM = fig.DESK + 4.6
LAMP_Y = fig.DESK + 12.0


def plate_sd(x: Array, y: Array) -> Array:
    """The console's top plate (signed distance, design units): what hands rest on."""
    (l0, r0), top_y = CONSOLE_TOP_X, fig.DESK
    return sd_poly(x, y, ((l0 + 2.5, top_y + 0.8), (r0 - 2.5, top_y + 0.8), (r0 + 0.8, PLATE_BOTTOM),
                          (l0 - 0.8, PLATE_BOTTOM))) - 0.8


def front_sd(x: Array, y: Array) -> Array:
    """The console's front panel, from under the plate to the floor."""
    (l0, r0), (l1, r1) = CONSOLE_TOP_X, CONSOLE_BASE_X
    return sd_poly(x, y, ((l0 - 0.4, fig.DESK + 4.4), (r0 + 0.4, fig.DESK + 4.4), (r1, CONSOLE_BASE - 1.6),
                          (l1, CONSOLE_BASE - 1.6))) - 1.6


def plate_cover(cv: Canvas, reg: Region) -> Array:
    """How much of each pixel of ``reg`` the console's top plate covers (0 to 1)."""
    return cv.cover(reg.full(plate_sd(reg.x, reg.y)))


def front_cover(cv: Canvas, reg: Region) -> Array:
    """How much of each pixel of ``reg`` the console's front shows (0 to 1): the front panel where the plate
    doesn't cover it. A picture of a member is hidden there (``_picture``): it sits behind the console."""
    return cv.cover(reg.full(front_sd(reg.x, reg.y))) * (1 - plate_cover(cv, reg))


def ensign_step(frames: int, gap: int, ensign_gap: int) -> int:
    """How many station frames an ensign holds each of its bobs: about its own gap, but dividing the pose's
    loop and leaving at least two steps in it (so a two-frame pose still bobs)."""
    per = max(1, round(ensign_gap / max(1, gap)))
    while per > 1 and (frames % per or frames // per < 2):
        per -= 1
    return per


def draw_console(cv: Canvas, light: Light, colors: dict[str, Array], lamp: Array, rig: fig.Rig | None) -> None:
    """The console in front of the member, low and slim: a top plate with a screen set into it (lit in the
    lamp's colour: its light is what falls on the member), a panel with small lights, and the status lamp, a
    bar of light in exactly the lamp's colour with its glow round it (dark at an empty station)."""
    top_y, base = fig.DESK, CONSOLE_BASE
    l0, r0 = CONSOLE_TOP_X
    reg = cv.region(l0 - 4, top_y - 2, r0 + 4, 100)
    if reg is None:
        return
    x, y = reg.x, reg.y
    console = colors["console"]
    lit = rig is not None
    power = rig.lamp if rig is not None else 0.0
    plate = reg.full(plate_sd(x, y))
    front = reg.full(front_sd(x, y))
    # the front panel: lit from above, darker towards the floor
    nx, ny, nz = normals(reg, front, 2.2, cv.px)
    shade = 0.92 - 0.3 * smoothstep(top_y + 6, base, reg.full(y))
    fill(cv, light, reg, front, console * shade[..., None], n=(nx * 0.9, ny * 0.9 + 0.12, nz), wrap=0.2, rim=0.3)
    groove = np.maximum(np.abs(y - (base - 4.0)) - 0.4, front + 3.0)
    cv.darken(reg, cv.cover(reg.full(groove)) * 0.35)
    # the top plate: brighter, facing up, a lit front edge
    pn = normals(reg, plate, 1.2, cv.px)
    fill(cv, light, reg, plate, console * 1.35 + 0.04, n=(pn[0] * 0.6, pn[1] * 0.6 - 0.75, pn[2] * 0.6 + 0.3),
         wrap=0.3, gloss=0.25, shininess=12.0)
    edge = np.maximum(np.abs(y - (top_y + 1.2)) - 0.45, plate)
    cv.light(reg, WHITE, cv.cover(reg.full(edge)) * 0.18)
    screen = reg.full(sd_poly(x, y, ((36.0, top_y + 1.8), (80.0, top_y + 1.8), (82.0, top_y + 3.9),
                                     (34.0, top_y + 3.9))) - 0.3)
    fill(cv, None, reg, screen, mix(console * 0.6, lamp, 0.55 if lit else 0.12))
    if lit:
        cv.shine(reg, lamp, soft_glow(np.maximum(screen, 0), 3.0) * 0.25 * power, opacity=0.25)
    # the lamp
    bar = reg.full(sd_box(x, y, fig.CX, LAMP_Y, 11.5, 2.8, 2.8))
    cv.darken(reg, cv.cover(reg.full(sd_box(x, y, fig.CX, LAMP_Y, 13.4, 4.4, 4.4)), 2.0) * 0.45)
    if lit:
        fill(cv, None, reg, bar, lamp)
        cv.light(reg, lamp, soft_glow(np.maximum(bar, 0), 12.0) * (bar > 0) * 0.55 * max(0.4, power))
    else:
        fill(cv, None, reg, bar, mix(lamp, console, 0.72) * 0.85)
    # small lights either side, blinking in a pattern per frame
    pattern = rig.lights if rig is not None else 0
    for k, lx in enumerate((27.0, 33.5, 82.5, 89.0)):
        on = lit and ((k * 5 + pattern * 3 + k * k) % 7) not in (0, 3)
        c = colors[LIGHTS[(k + pattern) % 4]] if on else console * 0.55
        fill(cv, None, reg, sd_circle(x, y, lx, LAMP_Y, 1.35), c)
        if on:
            cv.shine(reg, c, soft_glow(reg.full(np.sqrt((x - lx) ** 2 + (y - LAMP_Y) ** 2)), 5.0) * 0.35,
                     opacity=0.0)


def draw_ensign(cv: Canvas, light: Light, look: fig.Look, at: tuple[float, float], bob: float,
                size: float = ENSIGN_SIZE) -> None:
    """A little helper standing on the floor in front of the console: a running subagent, in its member's
    uniform, ``size`` times its drawn size (about 23 design units tall at 1)."""
    fx, floor = at
    fy = floor - bob
    k = size
    reg = cv.region(fx - 10 * k, fy - 24 * k, fx + 10 * k, floor + 1.5)
    if reg is None:
        return
    x, y = reg.full(reg.x - fx) / k, reg.full(reg.y - fy) / k  # the ensign's own units, feet at 0

    def paint(d: Array, color: Array, thick: float, wrap: float = 0.25, gloss: float = 0.0, warm: float = 0.0) -> None:
        fill(cv, light, reg, d * k, color, thick * k, wrap=wrap, gloss=gloss, warm=warm)

    cv.darken(reg, soft_glow(np.maximum(sd_ellipse(x, y + bob / k, 0.0, -0.5, 5.5, 1.3) * k, 0), 2.5 * k) * 0.4)
    paint(np.minimum(sd_ellipse(x, y, -2.2, -1.1, 2.0, 1.2), sd_ellipse(x, y, 2.2, -1.1, 2.0, 1.2)),
          look.uniform * 0.35, 1.0)
    paint(np.minimum(sd_capsule(x, y, -4.6, -10.0, -6.0, -5.0, 1.5), sd_capsule(x, y, 4.6, -10.0, 6.0, -5.0, 1.5)),
          look.uniform * 0.85, 1.4, wrap=0.3)
    paint(sd_poly(x, y, ((-3.3, -11.0), (3.3, -11.0), (4.0, -2.6), (-4.0, -2.6))) - 1.5, look.uniform, 2.6, wrap=0.3)
    fill(cv, None, reg, sd_arc(x, y, 0.0, -12.3, 2.2, 0.35, 2.79, 0.45) * k, look.trim, alpha=0.9)
    r = 5.4
    u, v = x / r, (y + 17.2) / r
    paint(sd_circle(x, y, 0.0, -17.2, r), look.skin, 4.0, wrap=0.4, warm=0.7)
    eyes = np.minimum(sd_ellipse(u, v, -0.36, 0.14, 0.13, 0.19), sd_ellipse(u, v, 0.36, 0.14, 0.13, 0.19))
    fill(cv, None, reg, eyes * r * k, look.eyes)
    shine = np.minimum(sd_circle(u, v, -0.4, 0.06, 0.06), sd_circle(u, v, 0.32, 0.06, 0.06))
    fill(cv, None, reg, shine * r * k, WHITE, alpha=0.9)
    fill(cv, None, reg, sd_arc(u, v, 0.0, 0.32, 0.13, 0.7, 2.44, 0.045) * r * k, look.mouth)
    _, front = fig.hair_shapes(look.hair_style, u * 1.04, v * 1.04 + 0.04, 0.0)
    paint(front * r, look.hair, 2.0, wrap=0.3, gloss=0.25)


# ================================================================ effects ==


def draw_fx(cv: Canvas, rig: fig.Rig) -> None:
    for kind, fx, fy, k in rig.fx:
        if kind == "bang":
            _bang(cv, fx, fy, 15.0, k)
        elif kind == "zee":
            _zee(cv, fx, fy, 8.0 * k, min(1.0, 1.4 - 0.5 * k))
        elif kind == "steam":
            _steam(cv, fx, fy, k)
        elif kind == "spark":
            _spark(cv, fx, fy, k)
        elif kind == "smoke":
            _smoke(cv, fx, fy, k)
        elif kind == "arcs":
            _arcs(cv, fx, fy, k)
        elif kind == "sparkle":
            _sparkle(cv, fx, fy, k)


def _bang(cv: Canvas, x: float, y: float, size: float, k: float) -> None:
    """A glowing "!"."""
    reg = cv.region(x - size * 0.6, y - size * 1.1, x + size * 0.6, y + size * 0.35)
    if reg is None or k <= 0:
        return
    col = np.array([1.0, 0.82, 0.4], F32)
    d = reg.full(np.minimum(sd_capsule(reg.x, reg.y, x, y - size * 0.92, x, y - size * 0.36, size * 0.13,
                                       size * 0.085), sd_circle(reg.x, reg.y, x, y - size * 0.08, size * 0.11)))
    cv.shine(reg, col, soft_glow(np.maximum(d, 0), size * 0.45) * 0.55 * k, opacity=0.6)
    fill(cv, None, reg, d, mix(col, WHITE, 0.25), alpha=k)


def _zee(cv: Canvas, x: float, y: float, size: float, alpha: float) -> None:
    s = size / 2
    reg = cv.region(x - s - 3, y - s - 3, x + s + 3, y + s + 3)
    if reg is None or alpha <= 0:
        return
    w = max(0.5, size * 0.11)
    d = reg.full(union(sd_capsule(reg.x, reg.y, x - s, y - s, x + s, y - s, w),
                       sd_capsule(reg.x, reg.y, x + s, y - s, x - s, y + s, w),
                       sd_capsule(reg.x, reg.y, x - s, y + s, x + s, y + s, w)))
    col = np.array([0.82, 0.88, 1.0], F32)
    cv.shine(reg, col, soft_glow(np.maximum(d, 0), 2.5) * 0.3 * alpha, opacity=0.5)
    fill(cv, None, reg, d, col, alpha=alpha)


def _steam(cv: Canvas, x: float, y: float, phase: float) -> None:
    """Two wisps rising from a mug, drifting right, a third of a wave further each frame."""
    reg = cv.region(x - 7, y - 17, x + 11, y + 1)
    if reg is None:
        return
    total = np.zeros(reg.shape, F32)
    for j, ox in enumerate((-1.8, 1.8)):
        t = np.clip((y - reg.y) / 15.0, 0, 1)
        wave = ox + 4.0 * t * t + 1.5 * np.sin(t * 7.0 - (phase + j * 0.4) * 2 * math.pi) * (0.3 + t)
        d = reg.full(np.abs(reg.x - x - wave) - (0.55 + 0.5 * t))
        a = np.clip(0.5 - d / 0.9, 0, 1) * reg.full(smoothstep(0.0, 0.15, t) * smoothstep(1.0, 0.45, t))
        total = np.maximum(total, a)
    cv.shine(reg, np.array([0.95, 0.97, 1.0], F32), total * 0.55, opacity=0.9)


def _spark(cv: Canvas, x: float, y: float, k: float) -> None:
    reach = 12.0
    reg = cv.region(x - reach, y - reach, x + reach, y + reach)
    if reg is None or k <= 0:
        return
    hot = np.array([1.0, 0.98, 0.85], F32)
    orange = np.array([1.0, 0.6, 0.22], F32)
    r = reg.full(np.sqrt((reg.x - x) ** 2 + (reg.y - y) ** 2))
    cv.shine(reg, orange, soft_glow(r, reach) * 0.7 * k, opacity=0.7)
    cv.shine(reg, hot, reg.full(star(reg.x, reg.y, x, y, 1.4, 8.0 * k, reach, angle=0.4)) * 1.2 * k, opacity=1.0)
    for j, (dx, dy) in enumerate(((6.0, -4.5), (-6.5, -2.5), (2.5, -8.0))):
        bit = reg.full(sd_circle(reg.x, reg.y, x + dx * k, y + dy * k, 0.7))
        cv.shine(reg, hot if j else orange, np.clip(0.5 - bit / 0.6, 0, 1) * k, opacity=1.0)


def _smoke(cv: Canvas, x: float, y: float, t: float) -> None:
    reg = cv.region(x - 9, y - 9, x + 9, y + 9)
    if reg is None:
        return
    col = np.array([0.72, 0.74, 0.82], F32)
    for j, (dx, dy, r) in enumerate(((0.0, 0.0, 3.0), (3.0, -2.6, 2.4), (-2.4, -3.6, 2.0))):
        d = reg.full(sd_circle(reg.x, reg.y, x + dx, y + dy - 2.0 * t, r * (1 + 0.4 * t)))
        cv.shine(reg, col, np.clip(0.5 - d / 2.4, 0, 1) * (0.28 - 0.05 * j) * (1 - 0.5 * t), opacity=0.7)


def _arcs(cv: Canvas, x: float, y: float, side: float) -> None:
    """Two short motion lines beside a waving hand at (x, y), on its ``side`` (1 right, -1 left)."""
    reg = cv.region(x - 12, y - 12, x + 12, y + 12)
    if reg is None:
        return
    col = np.array([1.0, 0.9, 0.62], F32)
    mid = 0.0 if side > 0 else math.pi
    for j, r in enumerate((6.5, 9.5)):
        d = reg.full(sd_arc(reg.x, reg.y, x, y, r, mid - 0.5, mid + 0.5, 0.42 - 0.08 * j))
        cv.shine(reg, col, cv.cover(d) * (0.75 - 0.25 * j), opacity=0.9)


def _sparkle(cv: Canvas, x: float, y: float, k: float) -> None:
    reg = cv.region(x - 6, y - 6, x + 6, y + 6)
    if reg is None or k <= 0:
        return
    cv.shine(reg, np.array([0.8, 0.95, 1.0], F32), reg.full(star(reg.x, reg.y, x, y, 0.6, 3.5 * k, 5.5)) * 0.9 * k,
             opacity=1.0)


# ================================================================ the captain's chair ==

CHAIR_DESIGN = (116.0, 100.0)


def captain_chair(colors: dict[str, Array], waiting: int, bright: bool, cv: Canvas) -> None:
    """The captain's chair (the user's, empty), seen from behind and a little above: a contoured padded back
    with lit piping and quilting, a headrest, armrests with small glowing panels, a glossy pedestal on a ring
    of light. Three waiting lights across the top of the back glow amber for each crew member waiting
    (``bright``: the pulse's high), and wash the back in their light; dark sockets otherwise."""
    count = min(waiting, 3)
    amber = colors["waiting"] if bright else colors["waiting_dim"]
    light = Light(amber, (58.0, 22.0, 16.0), lamp_power=0.28 * count * (1.0 if bright else 0.6), reach=40.0,
                  rim_power=0.7)
    reg = cv.everywhere()
    x, y = reg.x, reg.y
    captain, console, cyan = colors["captain"], colors["console"], colors["light1"]
    # the floor: a soft shadow and a ring of light round the pedestal
    floor = reg.full(sd_ellipse(x, y, 58.0, 94.0, 36.0, 4.8))
    cv.over(reg, np.zeros(3, F32), soft_glow(np.maximum(floor, 0), 5.0) * 0.5 * (floor > -0.5) +
            np.clip(-floor / 3.0, 0, 1) * 0.5)
    ring = np.abs(sd_ellipse(x, y, 58.0, 94.0, 32.0, 4.0)) - 0.55
    cv.shine(reg, mix(cyan, captain, 0.3), cv.cover(reg.full(ring), 2.0) * 0.5, opacity=0.6)
    pedestal = sd_poly(x, y, ((51.0, 76.0), (65.0, 76.0), (70.0, 93.0), (46.0, 93.0))) - 1.2
    fill(cv, light, reg, pedestal, console * 0.75, 2.5, gloss=0.5, shininess=18.0)
    # the seat past the back, and the armrests reaching forward, their tops lit, a panel on each
    fill(cv, light, reg, sd_box(x, y, 58.0, 73.0, 31.0, 6.0, 5.0), captain * 0.8, 3.5)
    for side in (-1.0, 1.0):
        ax = 58.0 + side * 36.0
        arm = sd_box(x, y, ax, 66.0, 10.0, 7.5, 4.0)
        fill(cv, light, reg, arm, console * 1.1, 3.5, gloss=0.35, shininess=14.0)
        top = sd_box(x, y, ax, 61.0, 9.0, 2.6, 2.6)
        fill(cv, light, reg, top, console * 1.5 + 0.03, 1.5, gloss=0.4)
        panel = reg.full(sd_box(x, y, ax + side * 1.0, 61.0, 6.0, 1.4, 1.4))
        fill(cv, None, reg, panel, console * 0.45)
        for bx, c in ((-3.4, cyan), (0.0, colors["light2"]), (3.4, amber if count else cyan)):
            button = reg.full(sd_circle(x, y, ax + side * (bx + 1.0), 61.0, 1.0))
            fill(cv, None, reg, button, mix(c, WHITE, 0.25))
            cv.shine(reg, c, soft_glow(np.maximum(button, 0), 3.2) * 0.45, opacity=0.35)
    # the back: wider at the shoulders, rounded, its edge piped; quilted inside; the headrest above
    back = sd_poly(x, y, ((36.0, 29.0), (80.0, 29.0), (83.0, 44.0), (77.0, 71.0), (39.0, 71.0), (33.0, 44.0))) - 8.0
    head = sd_box(x, y, 58.0, 14.5, 15.0, 6.5, 6.5)
    posts = np.minimum(sd_box(x, y, 51.0, 22.5, 2.0, 3.5, 1.0), sd_box(x, y, 65.0, 22.5, 2.0, 3.5, 1.0))
    cv.darken(reg, soft_glow(np.maximum(reg.full(back), 0), 4.0) * 0.3)
    fill(cv, light, reg, posts, console * 0.9, 1.5, gloss=0.5)
    bd = reg.full(back)
    n = normals(reg, bd, 9.0, cv.px)
    inner = bd + 4.5
    quilt = np.abs(np.sin((reg.full(x) - 58.0) * 0.32 + reg.full(y) * 0.32)) * np.abs(
        np.sin((reg.full(x) - 58.0) * 0.32 - reg.full(y) * 0.32))
    tone = captain * (0.95 - 0.12 * smoothstep(0.75, 0.95, 1 - quilt) * (inner < 0))[..., None]
    fill(cv, light, reg, bd, tone, n=n, wrap=0.3, gloss=0.2, shininess=10.0)
    piping = np.maximum(np.abs(bd + 1.6) - 0.65, -bd - 3.0)
    fill(cv, light, reg, piping, captain * 1.7 + 0.05, n=n, alpha=0.9, wrap=0.3, gloss=0.4)
    hd = reg.full(head)
    hn = normals(reg, hd, 5.0, cv.px)
    fill(cv, light, reg, hd, captain * 1.05, n=hn, wrap=0.3, gloss=0.25, shininess=10.0)
    fill(cv, light, reg, np.maximum(np.abs(hd + 1.4) - 0.6, -hd - 2.5), captain * 1.7 + 0.05, n=hn, alpha=0.9,
         gloss=0.4)
    # the waiting lights
    for k, lx in enumerate((45.0, 58.0, 71.0)):
        on = count >= k + 1
        socket = reg.full(sd_box(x, y, lx, 34.0, 5.6, 2.8, 2.8))
        fill(cv, light, reg, socket, captain * 0.4, 1.0)
        dot = reg.full(sd_box(x, y, lx, 34.0, 4.4, 1.7, 1.7))
        fill(cv, None, reg, dot, amber if on else colors["indicator_off"])
        if on:
            cv.light(reg, amber, soft_glow(np.maximum(dot, 0), 14.0) * (0.5 if bright else 0.25))
            cv.shine(reg, amber, soft_glow(np.maximum(dot, 0), 5.5) * (0.5 if bright else 0.28), opacity=0.45)
            fill(cv, None, reg, sd_capsule(x, y, lx - 2.8, 33.6, lx + 2.8, 33.6, 0.5), mix(amber, WHITE, 0.7),
                 alpha=0.8)
