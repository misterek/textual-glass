"""Backdrop painters: the opaque image under everything. No Textual imports.

The theme variable ``glass-backdrop`` picks one (``Palette.backdrop``):

* ``gradient`` (the default): a soft diagonal gradient from the theme.
* ``nebula``: deep space with two soft nebulae and a sparse starfield.

Each returns float32 ``(height, width, 3)`` and is deterministic, and both
place everything in relative coordinates, so the same window rendered at two
resolutions (a quick render, then the full one) looks the same.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np

from .render import F32, col, smoothstep

if TYPE_CHECKING:
    from .palette import Palette


def _smooth_noise(rng: np.random.Generator, width: int, height: int, cells_x: int, cells_y: int) -> np.ndarray:
    grid = rng.random((cells_y + 1, cells_x + 1), dtype=F32)

    def weights(n: int, cells: int) -> np.ndarray:
        g = (np.arange(n, dtype=F32) + 0.5) * cells / n
        i = np.floor(g).astype(int)
        f = g - i
        f = f * f * (3 - 2 * f)
        m = np.zeros((n, cells + 1), F32)
        m[np.arange(n), np.minimum(i, cells)] += 1 - f
        m[np.arange(n), np.minimum(i + 1, cells)] += f
        return m

    return weights(height, cells_y) @ grid @ weights(width, cells_x).T


def _fbm(rng: np.random.Generator, width: int, height: int, base_x: int, base_y: int, octaves: int) -> np.ndarray:
    acc = np.zeros((height, width), F32)
    amp, total = 1.0, 0.0
    for o in range(octaves):
        acc += amp * _smooth_noise(rng, width, height, base_x << o, max(1, base_y << o))
        total += amp
        amp *= 0.5
    return acc / total


def gradient(width: int, height: int, pal: Palette, seed: int = 7) -> np.ndarray:
    """A soft diagonal gradient from the theme (``bg_top`` to ``bg_bottom``), a
    cool light in the top-left corner, a gentle vignette and a faint cloudy
    texture that breaks up banding in the dark gradient (ordered dithering
    would triple the compressed size)."""
    rng = np.random.default_rng(seed)
    xs = (np.arange(width, dtype=F32)[None, :] + 0.5) / width
    ys = (np.arange(height, dtype=F32)[:, None] + 0.5) / height
    t = np.clip(0.3 * xs + 0.7 * ys, 0, 1)[..., None] ** 1.1
    img = col(pal.bg_top) * (1 - t) + col(pal.bg_bottom) * t
    light = np.exp(-(((xs - 0.12) / 0.5) ** 2) - ((ys + 0.05) / 0.45) ** 2)[..., None]
    img = img + col(pal.primary) * light * (0.045 if pal.dark else 0.025)
    img = img + col(pal.secondary) * np.exp(-(((xs - 0.95) / 0.4) ** 2) - ((ys - 1.0) / 0.5) ** 2)[..., None] * 0.02
    vignette = 1 - 0.2 * ((xs - 0.5) ** 2 * 1.4 + (ys - 0.42) ** 2) * (1.0 if pal.dark else 0.3)
    img = img * vignette[..., None]
    texture = ((_smooth_noise(rng, width, height, 9, 6) - 0.5) * 0.6
               + (_smooth_noise(rng, width, height, 31, 19) - 0.5) * 0.4)
    img = img * (1 + texture[..., None] * (0.07 if pal.dark else 0.02))
    return img.astype(F32)


def nebula(width: int, height: int, pal: Palette, seed: int = 7, stars: int | None = None) -> np.ndarray:
    """Deep space: a vertical gradient (``pal.space``), two soft nebulae and a
    faint third (``pal.nebula``, scaled by ``pal.nebula_strength``), a vignette
    and a sparse starfield. Kept dim on purpose: it is ambience behind content.
    ``stars`` is how many (by default, by area)."""
    rng = np.random.default_rng(seed)
    xs = (np.arange(width, dtype=F32)[None, :] + 0.5) / width
    ys = (np.arange(height, dtype=F32)[:, None] + 0.5) / height
    t = (ys ** 0.8)[..., None]
    img = col(pal.space[0]) * (1 - t) + col(pal.space[1]) * t
    img = np.broadcast_to(img, (height, width, 3)).astype(F32).copy()
    n1 = _fbm(rng, width, height, 5, 3, 4)
    n2 = _fbm(rng, width, height, 4, 3, 4)
    neb1 = smoothstep(0.42, 0.78, n1) * np.exp(-(((xs - 0.82) / 0.45) ** 2 + ((ys - 0.12) / 0.55) ** 2))
    neb2 = smoothstep(0.45, 0.8, n2) * np.exp(-(((xs - 0.1) / 0.4) ** 2 + ((ys - 0.92) / 0.5) ** 2))
    neb3 = smoothstep(0.5, 0.9, n1 * n2 * 1.6) * 0.6
    k = pal.nebula_strength
    img += col(pal.nebula[0]) * (neb1 * 0.5 * k)[..., None]
    img += col(pal.nebula[1]) * (neb2 * 0.45 * k)[..., None]
    img += col(pal.nebula[2]) * (neb3 * 0.22 * k)[..., None]
    vig = 1 - 0.45 * ((xs - 0.5) ** 2 + (ys - 0.45) ** 2) * 2.2
    img *= np.clip(vig, 0.4, 1)[..., None]
    n = width * height // 900 if stars is None else stars
    if n and width > 2 and height > 2:
        sxs = (1 + rng.random(n) * (width - 2)).astype(int)
        sys_ = (1 + rng.random(n) * (height - 2)).astype(int)
        b = (0.06 + 0.7 * rng.random(n) ** 7).astype(F32)
        tint = rng.random(n)
        cols = np.stack([0.75 + 0.25 * tint, 0.82 + 0.1 * tint, 1.0 - 0.2 * tint], 1).astype(F32)
        np.add.at(img, (sys_, sxs), cols * b[:, None])
    return img


PAINTERS: dict[str, Callable[[int, int, Palette], np.ndarray]] = {"gradient": gradient, "nebula": nebula}


def draw(width: int, height: int, pal: Palette) -> np.ndarray:
    """The backdrop the palette asks for (``pal.backdrop``)."""
    return PAINTERS.get(pal.backdrop, gradient)(width, height, pal)
