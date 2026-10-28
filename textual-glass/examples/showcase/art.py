"""Pixel art made in code: a little rocket taking off, with a flickering flame,
puffs of smoke and twinkling sparkles. No image files.

``rocket_frames()`` returns RGBA frames (``(size, size, 4)`` uint8 arrays) and
the time per frame, ready for ``GlassImage(first, size, size, frames=rest,
gap_ms=gap)``. Nothing here depends on Textual or textual-glass.
"""

from __future__ import annotations

import math

import numpy as np

SIZE = 48
FRAMES = 6
GAP_MS = 110


def _hex(code: str) -> tuple[int, int, int]:
    code = code.lstrip("#")
    return int(code[0:2], 16), int(code[2:4], 16), int(code[4:6], 16)


OUTLINE = _hex("#1d1630")
HULL_LIGHT = _hex("#ffffff")
HULL = _hex("#e3e8f2")
HULL_SHADE = _hex("#aab4c8")
RED_LIGHT = _hex("#ff7a7f")
RED = _hex("#e5484d")
RED_SHADE = _hex("#a8323d")
RIM = _hex("#6b7590")
GLASS = _hex("#5ad1ff")
GLASS_DEEP = _hex("#2a7fd4")
GLINT = _hex("#e8fbff")
NOZZLE = _hex("#59607a")
FLAME_CORE = _hex("#fff6c8")
FLAME_MID = _hex("#ffc23d")
FLAME_OUT = _hex("#ff6a2b")
SMOKE = _hex("#c6cbe0")
SPARKLE = _hex("#ffd75e")
SPARKLE_TIP = _hex("#fff4c2")

# The rocket flies up and to the right: its axis runs from the tail (TAIL) to the nose.
TAIL = (18.5, 29.5)
LENGTH = 27.0
WIDTH = 6.0  # half the hull's width
SPARKLES = ((7.0, 8.0, 0.0), (21.0, 6.0, 0.55), (9.0, 22.0, 0.3), (41.0, 33.0, 0.15), (30.0, 42.0, 0.7),
            (44.0, 21.0, 0.45))  # x, y, phase
PUFFS = ((-14.5, 1.2, 3.2), (-19.0, -1.4, 2.6), (-22.5, 0.6, 1.9))


def rocket_frames(size: int = SIZE, frames: int = FRAMES) -> tuple[list[np.ndarray], int]:
    """The animation's frames (straight RGBA) and the gap between them in ms."""
    return [_frame(size, k / frames) for k in range(frames)], GAP_MS


def _frame(size: int, t: float) -> np.ndarray:
    scale = size / SIZE
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float32)
    x, y = (xs + 0.5) / scale, (ys + 0.5) / scale  # design units
    # u: along the axis from the tail towards the nose; v: across it (negative is the lit side).
    ax, ay = math.cos(-math.pi / 4), math.sin(-math.pi / 4)
    dx, dy = x - TAIL[0], y - TAIL[1]
    u = dx * ax + dy * ay
    v = -dx * ay + dy * ax
    out = np.zeros((size, size, 4), np.uint8)

    def paint(mask: np.ndarray, colour: tuple[int, int, int]) -> None:
        out[mask] = (*colour, 255)

    # sparkles: four-pointed stars that grow and shrink out of step with each other
    for sx, sy, phase in SPARKLES:
        level = 0.5 + 0.5 * math.cos(2 * math.pi * (t + phase))
        reach = 1.0 + 2.0 * level
        ox, oy = np.abs(x - sx), np.abs(y - sy)
        arm = ((ox < 0.55) & (oy < reach)) | ((oy < 0.55) & (ox < reach))
        paint(arm, SPARKLE)
        paint((ox < 0.55) & (oy < 0.55) & (level > 0.4), SPARKLE_TIP)

    # smoke puffs drift back from the tail and fade
    for pu, pv, radius in PUFFS:
        drift = t * 3.0
        r = radius * (1.0 - 0.25 * t)
        puff = (u - (pu - drift)) ** 2 + (v - pv) ** 2 <= r * r
        paint(puff, SMOKE)
        rim = puff & ((u - (pu - drift)) ** 2 + (v - pv + 0.9) ** 2 > r * r)
        paint(rim, HULL_SHADE)

    # the flame flickers: its length and width wobble from frame to frame
    flicker = 0.5 + 0.5 * math.sin(2 * math.pi * t * 2)
    length = 10.0 + 4.0 * flicker
    along = np.clip((-u - 1.0) / length, 0.0, 1.0)
    half = (3.4 + 0.8 * flicker) * np.sqrt(np.maximum(0.0, 1.0 - along)) * (1.0 - 0.15 * along)
    flame = (u < -1.0) & (u > -1.0 - length) & (np.abs(v) <= half)
    paint(flame, FLAME_OUT)
    paint(flame & (np.abs(v) <= half * 0.66) & (along < 0.8), FLAME_MID)
    paint(flame & (np.abs(v) <= half * 0.3) & (along < 0.45), FLAME_CORE)

    # hull: a body with an ogive nose
    nose_start = LENGTH - 10.0
    width = np.where(u < nose_start, WIDTH, WIDTH * np.sqrt(np.clip((LENGTH - u) / 10.0, 0.0, 1.0)))
    hull = (u >= 0.0) & (u <= LENGTH) & (np.abs(v) <= width)
    # fins: swept back from the hull at the tail
    fin_reach = WIDTH + 4.8 * np.clip(1.0 - (u + 1.0) / 9.0, 0.0, 1.0)
    fins = (u >= -1.5) & (u <= 8.0) & (np.abs(v) > WIDTH - 0.5) & (np.abs(v) <= fin_reach)
    nozzle = (u >= -1.5) & (u < 0.0) & (np.abs(v) <= 3.4)
    body = hull | fins | nozzle

    # outline: one pixel around the body
    grown = body.copy()
    grown[1:, :] |= body[:-1, :]
    grown[:-1, :] |= body[1:, :]
    grown[:, 1:] |= body[:, :-1]
    grown[:, :-1] |= body[:, 1:]
    paint(grown & ~body, OUTLINE)

    paint(nozzle, NOZZLE)
    paint(hull, HULL)
    paint(hull & (v < -WIDTH * 0.35), HULL_LIGHT)
    paint(hull & (v > WIDTH * 0.45), HULL_SHADE)
    nose = hull & (u > nose_start + 1.5)
    paint(nose, RED)
    paint(nose & (v < -1.0), RED_LIGHT)
    paint(nose & (v > 1.6), RED_SHADE)
    band = hull & (u > 3.0) & (u < 4.6)
    paint(band, RED_SHADE)
    paint(fins, RED)
    paint(fins & (v < 0), RED_LIGHT)
    paint(fins & (v > 0) & (np.abs(v) > fin_reach - 1.4), RED_SHADE)

    # porthole: a rim, blue glass and a glint
    wu, wr = LENGTH - 14.5, 3.3
    d2 = (u - wu) ** 2 + v ** 2
    paint(d2 <= (wr + 1.0) ** 2, RIM)
    paint(d2 <= wr ** 2, GLASS_DEEP)
    paint((u - wu - 0.6) ** 2 + (v + 0.6) ** 2 <= (wr - 0.8) ** 2, GLASS)
    paint((u - wu - 0.9) ** 2 + (v + 1.3) ** 2 <= 0.8, GLINT)
    return out
