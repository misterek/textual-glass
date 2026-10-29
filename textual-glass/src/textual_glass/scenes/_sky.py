"""The night the space scenes bring with them on a light theme. Pure numpy, no
Textual imports.

Space is always night. On a dark theme the dashboard's own space shows through
and nothing here is used. On a light one a scene paints a window into space
behind everything (:func:`night_sky`): a rounded viewport over the whole stage
in the theme's deep-space colours (``Stage.space``), with faint nebulae
(``Stage.nebula``) and a few stars, its edge fading to transparent (rule 17).
Everything else is drawn for that night (:func:`night`): as on a dark theme,
in the theme's hues lifted so they glow (:func:`lift`).
"""

from __future__ import annotations

import colorsys
import dataclasses

import numpy as np

from ..palette import RGB
from ..render import F32, smoothstep
from . import Actor, Route, Stage
from ._paint import Noise, Paint, Sprite, sd_box, stretched_size

#: Pixels of the viewport's picture (it is smooth, so the terminal stretches it over the stage).
SKY_PX = 160_000
#: Lightness and saturation a hue is lifted to, at least, to glow on the night (HLS, 0 to 1).
GLOW_LIGHTNESS, GLOW_SATURATION = 0.64, 0.9
#: The night's foreground: light, a trace of the theme's own hue.
NIGHT_LIGHTNESS, NIGHT_SATURATION = 0.86, 0.32


def lift(c: RGB, lightness: float = GLOW_LIGHTNESS, saturation: float = GLOW_SATURATION) -> RGB:
    """A colour that glows on the night: its hue kept, its lightness and saturation raised to at least
    ``lightness`` and ``saturation`` (greys stay grey, only lighter)."""
    h, li, s = colorsys.rgb_to_hls(*(v / 255 for v in c))
    li = min(0.82, max(li, lightness))
    if s > 0.08:
        s = max(s, saturation)
    r, g, b = colorsys.hls_to_rgb(h, li, s)
    return round(r * 255), round(g * 255), round(b * 255)


def _foreground(c: RGB) -> RGB:
    """The theme's (dark) foreground as light on the night, a trace of its hue left."""
    h, _, s = colorsys.rgb_to_hls(*(v / 255 for v in c))
    r, g, b = colorsys.hls_to_rgb(h, NIGHT_LIGHTNESS, min(s, NIGHT_SATURATION))
    return round(r * 255), round(g * 255), round(b * 255)


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    return round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t), round(a[2] + (b[2] - a[2]) * t)


def night(stage: Stage) -> Stage:
    """The stage as the art draws on it: itself on a dark theme; on a light one, a dark stage whose
    colours are the theme's hues lifted to glow (:func:`lift`), a light foreground and the theme's
    deep space as the background."""
    if stage.dark:
        return stage
    colors = {role: lift(c) for role, c in stage.colors.items()}
    colors["foreground"] = _foreground(stage.colors["foreground"])
    colors["background"] = _mix(*stage.space, 0.55)
    return dataclasses.replace(stage, dark=True, colors=colors)


def _corner(stage: Stage) -> tuple[float, float]:
    """The viewport's corner radius and the width of its fading edge, in terminal pixels."""
    cw, ch = stage.cell
    return min(1.2 * ch, 0.25 * min(stage.width, stage.height)), max(3.0, min(0.6 * cw, 0.12 * stage.height))


def viewport(stage: Stage, max_px: int = SKY_PX) -> Sprite:
    """The window into space: the theme's deep space, top to bottom, two faint nebulae and a third
    fainter still, a vignette and the faintest stars, in a rounded viewport over the whole stage whose
    edge fades to nothing. A still, drawn small: the terminal stretches it over the stage's cells."""
    w, h, s = stretched_size(stage, stage.cols, stage.rows, max_px)
    paint = Paint(w, h, True, margin=1.0, fine=True)
    xs, ys = paint.xs / w, paint.ys / h
    top, bottom = (np.asarray(c, F32) / 255 for c in stage.space)
    neb = [np.asarray(c, F32) / 255 for c in stage.nebula]
    t = (ys ** 0.8)[..., None]
    sky = top * (1 - t) + bottom * t
    sky = np.broadcast_to(sky, (h, w, 3)).astype(F32)
    noise = Noise(stage.seed * 23 + 9, n=16)
    aspect = w / max(1, h)
    u, v = xs * 3.0 * aspect, ys * 3.0
    n1 = noise.fbm(u, v, np.full_like(u * v, 0.3), octaves=4)
    n2 = noise.fbm(u + 7.1, v + 3.3, np.full_like(u * v, 1.7), octaves=4)
    cloud1 = smoothstep(0.42, 0.78, n1) * np.exp(-(((xs - 0.82) / 0.5) ** 2 + ((ys - 0.15) / 0.6) ** 2))
    cloud2 = smoothstep(0.45, 0.8, n2) * np.exp(-(((xs - 0.12) / 0.45) ** 2 + ((ys - 0.9) / 0.55) ** 2))
    cloud3 = smoothstep(0.5, 0.9, n1 * n2 * 1.6) * 0.6
    sky = sky + neb[0] * (cloud1 * 0.32)[..., None] + neb[1] * (cloud2 * 0.28)[..., None]
    sky = sky + neb[2] * (cloud3 * 0.14)[..., None]
    sky = sky * np.clip(1 - 0.4 * ((xs - 0.5) ** 2 + (ys - 0.45) ** 2) * 2.0, 0.55, 1)[..., None]
    rnd = np.random.default_rng(stage.seed * 29 + 1)
    count = w * h // 700
    if count:  # the faintest stars, soft at this size; the bright ones are actors of their own
        sx, sy = rnd.uniform(0, w, count), rnd.uniform(0, h, count)
        glow = (0.04 + 0.14 * rnd.random(count) ** 4).astype(F32)
        jx, jy = np.clip(sx.astype(int), 0, w - 1), np.clip(sy.astype(int), 0, h - 1)
        np.add.at(sky, (jy, jx), np.array([0.85, 0.9, 1.0], F32) * glow[:, None])
    radius, fade = (k * s for k in _corner(stage))
    d = sd_box(paint.xs, paint.ys, w / 2, h / 2, w / 2 - 1.0, h / 2 - 1.0, radius)  # 0 a pixel inside the edge
    alpha = smoothstep(0.0, -max(2.5, fade), d)
    paint.over(sky, alpha)
    return Sprite([paint.frame()], w, h, [1000], False, (stage.cols, stage.rows), name="night")


def _star(size: int, glow: float, spikes: bool) -> bytes:
    """A star at its own pixels: a hot point in a soft glow, and four faint spikes on the bright ones."""
    paint = Paint(size, size, True, margin=1.0)
    c = size / 2
    r = np.sqrt((paint.xs - c) ** 2 + (paint.ys - c) ** 2)
    reach = c - 1.0
    fade = smoothstep(reach, reach * 0.55, r)
    tint = np.array([0.86, 0.92, 1.0], F32)
    paint.add(tint, np.exp(-((r / 0.85) ** 2)) * glow)
    paint.add(tint, np.exp(-r / (0.12 * size + 0.4)) * 0.35 * glow * fade)
    if spikes:
        dx, dy = np.abs(paint.xs - c), np.abs(paint.ys - c)
        arm = (np.exp(-dx / 0.45) + np.exp(-dy / 0.45)) * np.exp(-r / (0.22 * size)) * 0.5 * glow
        paint.add(tint, arm * fade)
    return paint.frame()


def stars(stage: Stage) -> list[Actor]:
    """A few crisp stars over the viewport, at the terminal's own pixels: three pictures, each one image
    the terminal shows at every star of its kind (their frames are the same object). Kept off the
    viewport's fading edge."""
    side = max(5, round(0.32 * stage.cell[1]) | 1)
    looks = [([_star(side, 0.55, False)], side), ([_star(side, 0.85, False)], side),
             ([_star(side + 4, 1.0, True)], side + 4)]
    cells = stage.cols * stage.rows
    count = max(6, min(28, cells // 180))
    rnd = np.random.default_rng(stage.seed * 31 + 7)
    radius, fade = _corner(stage)
    inset = fade + side
    out = []
    for k in range(count):
        kind = 2 if k % 9 == 0 else 1 if k % 3 == 0 else 0
        frames, size = looks[kind]
        x = float(rnd.uniform(inset, max(inset + 1, stage.width - inset)))
        y = float(rnd.uniform(inset, max(inset + 1, stage.height - inset)))
        out.append(Actor(frames, size, size, Route.still(x, y), z=1))
    return out


def night_sky(stage: Stage) -> list[Actor]:
    """The viewport and its stars, to go behind everything else (z 0 and 1): nothing on a dark theme."""
    if stage.dark:
        return []
    return [viewport(stage).actor(Route.still(stage.width / 2, stage.height / 2), z=0), *stars(stage)]


__all__ = ["GLOW_LIGHTNESS", "GLOW_SATURATION", "SKY_PX", "lift", "night", "night_sky", "stars", "viewport"]
