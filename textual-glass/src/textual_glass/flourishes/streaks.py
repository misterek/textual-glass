"""Streaks: flourishes that travel. No Textual imports.

A streak is a head of light and a trail that glows where the head has been
and fades with age. Each one is drawn in its own small window of the stage
(or, for the comet, a band around its path worked out once), so the cost
follows the light, not the stage. On a dark theme the light adds up and its
cores burn white; on a light theme the same shapes are painted in colour.
"""

from __future__ import annotations

import math

import numpy as np

from ..render import F32, Array, Colour, col, smoothstep
from . import Flourish, Stage
from ._draw import border, centre, ease_out, rng, segment, unit

Frames = tuple[list[bytes], list[int]]
#: A window of the stage: its rows and columns, and their pixel centres.
Win = tuple[slice, slice, Array, Array]

WHITE = np.ones(3, F32)


# =============================================================== helpers ==


class _Canvas:
    """One frame being drawn, like ``_draw.Light``, but into windows of the
    stage: light adds up on a dark theme, paint covers on a light one."""

    def __init__(self, stage: Stage) -> None:
        self.stage = stage
        self.dark = stage.dark
        self.rgb: Array = np.zeros((stage.height, stage.width, 3), F32)
        self.a: Array = np.zeros((stage.height, stage.width), F32)

    def _apply(self, rgb: Array, a: Array, color: Array, k: Array) -> tuple[Array, Array]:
        k = np.clip(k, 0, None)
        if self.dark:
            return rgb + color * k[..., None], 1 - (1 - a) * (1 - np.minimum(k, 1))
        cover = np.minimum(k, 1)
        return color * cover[..., None] + rgb * (1 - cover)[..., None], cover + a * (1 - cover)

    def put(self, win: Win, color: Array, k: Array) -> None:
        """Light (or paint) ``color`` at ``k`` into a window."""
        sy, sx = win[0], win[1]
        self.rgb[sy, sx], self.a[sy, sx] = self._apply(self.rgb[sy, sx], self.a[sy, sx], color, k)

    def put_at(self, flat: Array, color: Array, k: Array) -> None:
        """Light (or paint) at the stage's pixels ``flat`` (unique flat indices)."""
        rgb, a = self.rgb.reshape(-1, 3), self.a.reshape(-1)
        rgb[flat], a[flat] = self._apply(rgb[flat], a[flat], color, k)

    def frame(self, mask: Array) -> bytes:
        """Straight-alpha RGBA bytes, faded out by ``mask``; on a dark theme light past full brightness turns
        white. Only the lit pixels are worked on."""
        a = (self.a * mask).reshape(-1)
        lit = np.flatnonzero(a > 2 / 255)  # no haze of nearly invisible light: smaller uploads
        out = np.zeros((a.size, 4), np.uint8)
        a = np.minimum(a[lit], 1)
        a = a * smoothstep(2 / 255, 6 / 255, a)
        s = self.rgb.reshape(-1, 3)[lit] / np.maximum(self.a.reshape(-1)[lit], 1e-4)[:, None]
        if self.dark:
            peak = s.max(axis=1)
            s = s / np.maximum(peak, 1.0)[:, None] + (np.clip(peak - 1.0, 0, None) * 0.6)[:, None]
        # faint light needs less colour precision (its error shows at its alpha): coarser steps there
        # never shift what shows by more than about one level, and compress much better
        step = np.exp2(np.clip(np.floor(-np.log2(np.maximum(a, 1e-3))), 0, 4))[:, None]
        out[lit, :3] = np.minimum(np.round(np.clip(s, 0, 1) * 255 / step) * step, 255)
        out[lit, 3] = a * 255 + 0.5
        return out.tobytes()


def _win(stage: Stage, x: float, y: float, rx: float, ry: float | None = None) -> Win | None:
    """The stage's pixels within ``rx`` (and ``ry``) of a point, or None off the stage."""
    ry = rx if ry is None else ry
    x0, x1 = max(0, int(x - rx)), min(stage.width, int(math.ceil(x + rx)) + 1)
    y0, y1 = max(0, int(y - ry)), min(stage.height, int(math.ceil(y + ry)) + 1)
    if x0 >= x1 or y0 >= y1:
        return None
    xs = np.arange(x0, x1, dtype=F32)[None, :] + F32(0.5)
    ys = np.arange(y0, y1, dtype=F32)[:, None] + F32(0.5)
    return slice(y0, y1), slice(x0, x1), xs, ys


def _ink(stage: Stage, color: Colour) -> Array:
    """A colour as drawn: as it is on a dark theme, a little deeper on a light one (it must show on white)."""
    c = col(color)
    return c if stage.dark else c * F32(0.82)


def _hot(stage: Stage, color: Colour) -> Array:
    """A core's colour: white on a dark theme, the deep colour itself on a light one."""
    return WHITE if stage.dark else col(color) * F32(0.7)


def _ramp(a: Array, b: Array, t: Array) -> Array:
    """Two colours mixed per pixel: ``t`` (an array) is the weight of ``b``."""
    return a * (1 - t)[..., None] + b * t[..., None]


def _timeline(total_ms: float, frames: int) -> tuple[list[float], list[int]]:
    """Frame start times (s) and even gaps (ms) for an effect ``total_ms`` long."""
    n = max(2, min(48, frames))
    gap = max(16, round(total_ms / n))
    return [k * gap / 1000 for k in range(n)], [gap] * n


def _sparkle(cv: _Canvas, x: float, y: float, k: float, color: Array, size: float, spike: float = 0.0) -> None:
    """A tiny star: a soft dot, with four thin spikes ``spike`` long (0 for none)."""
    reach = max(2.5 * size, spike) + 1.0
    win = _win(cv.stage, x, y, reach)
    if win is None or k <= 0.003:
        return
    xs, ys = win[2], win[3]
    dx, dy = np.abs(xs - x), np.abs(ys - y)
    r2 = dx * dx + dy * dy
    shape = np.exp(-r2 / (size * size))
    if spike > 0:
        thin = 0.45 * size + 0.15
        shape = shape + 0.7 * (np.exp(-dy / thin - dx / (0.35 * spike)) + np.exp(-dx / thin - dy / (0.35 * spike)))
    shape *= smoothstep(reach, reach * 0.55, np.sqrt(r2))
    cv.put(win, color, shape * k)


def _streak(cv: _Canvas, tail: tuple[float, float], head: tuple[float, float], width: float, k: float,
            color: Array, tint: Array, hot: Array, burn: float = 1.0, halo: float = 0.0) -> None:
    """A straight streak, faint and thin at its tail, bright at its round head; ``tint`` at the tail turns to
    ``color`` at the head, around a ``hot`` core (``burn`` is how hot), with a soft ``halo`` round the head."""
    if k <= 0.003:
        return
    (x0, y0), (x1, y1) = tail, head
    reach = (3.2 if halo else 2.2) * width + 1.0
    win = _win(cv.stage, (x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0) / 2 + reach, abs(y1 - y0) / 2 + reach)
    if win is None:
        return
    d, t = segment(win[2], win[3], x0, y0, x1, y1)
    w = width * (0.3 + 0.7 * t)
    shape = np.exp(-((d / w) ** 2)) * smoothstep(2.2, 1.6, d / w) * t ** 1.5
    if halo:
        r = np.sqrt((win[2] - x1) ** 2 + (win[3] - y1) ** 2)
        shape = shape + halo * np.exp(-((r / (1.3 * width)) ** 2)) * smoothstep(reach - 1, 0.6 * reach, r)
    cv.put(win, _ramp(tint, color, t), shape * k)
    if burn > 0:
        cv.put(win, hot, np.exp(-((d / (0.4 * w)) ** 2)) * t ** 4 * k * burn)


# ================================================================ comet ==


def _comet_ends(stage: Stage) -> tuple[Array, Array, bool]:
    """Where a comet starts and lands, and whether it lands on a target (it flies across the anchor without one)."""
    cw, ch = stage.cell
    if stage.to is None:
        x, y, w, h = stage.at
        return np.array([x + 0.06 * w, y + 0.7 * h], F32), np.array([x + 0.94 * w, y + 0.3 * h], F32), False
    a, b = np.array(centre(stage.at), F32), np.array(centre(stage.to), F32)
    if float(np.hypot(*(b - a))) < 2.5 * ch:  # the target is (nearly) the anchor: fly in from its lower left
        x, y, w, h = stage.to
        a = np.array([x - 2.5 * cw, y + h + 1.5 * ch], F32)
    lo, hi = np.array([ch, ch], F32), np.array([stage.width - ch, stage.height - ch], F32)
    return np.clip(a, lo, np.maximum(lo, hi)), b, True


def _arc(stage: Stage, a: Array, b: Array, bulge: float) -> tuple[Array, float]:
    """The side a comet's path bows out to (a unit normal) and by how much (px), so the path stays on the stage:
    upwards when there is room, like a thrown arc."""
    _, ch = stage.cell
    d = b - a
    dist = max(1e-3, float(np.hypot(*d)))
    s = np.linspace(0.05, 0.95, 19, dtype=F32)[:, None]
    m = 1.4 * ch
    lo = np.minimum(np.minimum(a, b), m)
    hi = np.maximum(np.maximum(a, b), np.array([stage.width - m, stage.height - m], F32))
    best: tuple[float, Array, float] = (-1.0, np.array([d[1], -d[0]], F32) / dist, 0.0)
    for n in (np.array([d[1], -d[0]], F32) / dist, np.array([-d[1], d[0]], F32) / dist):
        h = 0.0
        for f in (1.0, 0.8, 0.6, 0.4, 0.25, 0.1, 0.0):
            h = f * bulge * dist
            if np.all((a + d * s + n * h * 4 * s * (1 - s) >= lo) & (a + d * s + n * h * 4 * s * (1 - s) <= hi)):
                break
        score = f + (0.2 if n[1] < -0.05 else 0.0) + (0.1 if n[0] > 0 and abs(n[1]) <= 0.05 else 0.0)
        if score > best[0]:
            best = (score, n, h)
    return best[1], best[2]


def _band(stage: Stage, pts: Array, svals: Array, normal: Array, margin: float) -> tuple[Array, Array, Array]:
    """The stage's pixels within ``margin`` of a polyline: their flat indices, the path parameter of the nearest
    point, and the signed distance (positive on the ``normal`` side)."""
    x0 = max(0, int(pts[:, 0].min() - margin))
    x1 = min(stage.width, int(math.ceil(pts[:, 0].max() + margin)) + 1)
    y0 = max(0, int(pts[:, 1].min() - margin))
    y1 = min(stage.height, int(math.ceil(pts[:, 1].max() + margin)) + 1)
    best = np.full((y1 - y0, x1 - x0), np.inf, F32)
    param = np.zeros_like(best)
    side = np.zeros_like(best)
    for i in range(len(pts) - 1):
        (ax, ay), (bx, by) = pts[i], pts[i + 1]
        wx0, wx1 = max(x0, int(min(ax, bx) - margin)), min(x1, int(math.ceil(max(ax, bx) + margin)) + 1)
        wy0, wy1 = max(y0, int(min(ay, by) - margin)), min(y1, int(math.ceil(max(ay, by) + margin)) + 1)
        if wx0 >= wx1 or wy0 >= wy1:
            continue
        xs = np.arange(wx0, wx1, dtype=F32)[None, :] + F32(0.5)
        ys = np.arange(wy0, wy1, dtype=F32)[:, None] + F32(0.5)
        d, t = segment(xs, ys, ax, ay, bx, by)
        sub = slice(wy0 - y0, wy1 - y0), slice(wx0 - x0, wx1 - x0)
        nearer = d < best[sub]
        best[sub] = np.where(nearer, d, best[sub])
        param[sub] = np.where(nearer, svals[i] + t * (svals[i + 1] - svals[i]), param[sub])
        lean = (xs - (ax + t * (bx - ax))) * normal[0] + (ys - (ay + t * (by - ay))) * normal[1]
        side[sub] = np.where(nearer, np.where(lean >= 0, 1, -1), side[sub])
    rows, cols = np.nonzero(best <= margin)
    flat = (rows + y0) * stage.width + (cols + x0)
    return flat, param[rows, cols], best[rows, cols] * side[rows, cols]


def _glide(x: Array | float) -> Array:
    """The comet's progress along its path: a quick launch, a long glide, a soft landing."""
    x = np.clip(np.asarray(x, F32), 0, 1)
    return np.asarray(1 - (1 - x) ** 2.2 * (1 + 2.2 * x), F32)


class _Comet:
    """A comet's path, timing and sparkles, worked out once; :meth:`draw` draws it at any moment."""

    def __init__(self, stage: Stage) -> None:
        _, ch = stage.cell
        self.stage, self.ch, self.dark = stage, ch, stage.dark
        rnd = rng(stage)
        self.color, self.accent = _ink(stage, stage.color), _ink(stage, stage.accent)
        self.hot = _hot(stage, stage.color)
        self.a, self.b, self.lands = _comet_ends(stage)
        self.dist = float(np.hypot(*(self.b - self.a)))
        self.normal, self.bow = _arc(stage, self.a, self.b, 0.18)
        self.flight = float(np.clip(0.8 + 0.022 * self.dist / ch, 0.9, 1.3))
        self.trail = 0.5 * self.flight  # how long the trail glows where the head has been
        self.total = self.flight + (0.55 if self.lands else 0.4)

        # the band around the path: each pixel's nearest point, its side, and when the head passed it
        self.w0 = max(0.8, 0.34 * ch)
        self.drift = 0.9 * ch
        svals = np.linspace(0, 1, 49, dtype=F32)
        pts = self.at(svals)
        self.length = float(np.hypot(*np.diff(pts, axis=0).T).sum())
        self.flat, self.near, self.off = _band(stage, pts, svals, self.normal, self.drift + 5.0 * self.w0)
        self.table = np.linspace(0, 1, 257, dtype=F32)
        self.passed = self.when(self.near)

        # sparkles shed on the way: where and when, how they drift, twinkle and fade
        n = 40
        shed = np.sort(rnd.uniform(0.03, 0.97, n)).astype(F32)
        self.shed_t = self.when(shed)
        self.shed_at = (self.at(shed) + rnd.normal(0, 0.7 * self.w0, (n, 1)) * self.normal
                        + rnd.normal(0, 0.4 * self.w0, (n, 2)))
        self.shed_v = rnd.normal(0, 0.5 * ch, (n, 2)) + self.normal * rnd.uniform(0.3, 1.6, (n, 1)) * ch
        self.shed_life = rnd.uniform(0.3, 0.75, n)
        self.shed_k = rnd.uniform(0.5, 1.0, n)
        self.shed_phase = rnd.uniform(0, 2 * math.pi, n)
        self.shed_hz = rnd.uniform(4.0, 8.0, n)
        self.shed_accent = rnd.random(n) < 0.35
        self.shed_spike = rnd.random(n) < 0.25

        # the burst where it lands
        m = 14
        self.burst_dir = rnd.uniform(0, 2 * math.pi, m)
        self.burst_far = rnd.uniform(1.2, 3.0, m) * ch
        self.burst_life = rnd.uniform(0.3, 0.5, m)
        self.small = max(0.55 * unit(stage), 0.1 * ch)

    def at(self, s: Array) -> Array:
        """Points along the path (``s`` from 0 to 1)."""
        s = np.asarray(s, F32)[..., None]
        return self.a + (self.b - self.a) * s + self.normal * self.bow * 4 * s * (1 - s)

    def when(self, s: Array) -> Array:
        """When the head passes the points ``s`` of the path, in seconds."""
        return np.interp(s, _glide(self.table), self.table).astype(F32) * self.flight

    def draw(self, cv: _Canvas, t: float, fade: float) -> None:
        flight, ch, dark = self.flight, self.ch, self.dark
        s_head = float(_glide(t / flight))
        lit = min(1.0, t / 0.1)
        if self.lands:  # once it lands, what is left of the trail goes into the bloom
            self._trail(cv, t, s_head, fade * lit * (1 - float(smoothstep(flight, flight + 0.3, np.float32(t)))))
        else:
            self._trail(cv, t, s_head, fade * lit)
        self._sparkles(cv, t, fade)

        # it lights with a spark where it starts
        spark = math.exp(-(((t - 0.04) / 0.05) ** 2)) if t < 0.04 else math.exp(-(t - 0.04) / 0.07)
        if spark > 0.02:
            _sparkle(cv, float(self.a[0]), float(self.a[1]), spark * (1.4 if dark else 1.0),
                     self.hot if dark else self.color, self.small * 1.6, 1.2 * ch)

        # the head, which fades as it lands, or as it leaves the anchor without a target
        if self.lands:
            gone = float(smoothstep(flight - 0.02, flight + 0.14, np.float32(t)))
        else:
            gone = float(smoothstep(flight * 0.7, flight, np.float32(t)))
        if lit * (1 - gone) * fade > 0:
            hx, hy = (float(v) for v in self.at(np.float32(s_head)))
            self._head(cv, hx, hy, lit * (1 - gone) * fade)
        if self.lands and t - flight > -0.04:
            self._landing(cv, t - flight, fade)

    def _trail(self, cv: _Canvas, t: float, s_head: float, fade: float) -> None:
        """Brightest and widest where the head just passed, white-hot right behind it, turning to the accent as
        it ages; and a fainter, wider dust tail that drifts out to the side of the arc."""
        age = t - self.passed
        live = (age >= 0) & (age < self.trail)
        if fade <= 0 or not live.any():
            return
        w0 = self.w0
        idx, u, d = self.flat[live], age[live] / self.trail, self.off[live]
        behind = (s_head - self.near[live]) * self.length
        w = w0 * (1 - u) ** 0.5 + 0.3
        core = np.exp(-((d / w) ** 2)) * smoothstep(2.2, 1.6, np.abs(d) / w)
        sheen = np.exp(-((d / (1.6 * w)) ** 2)) * smoothstep(2.0, 1.4, np.abs(d) / (1.6 * w))
        hue = _ramp(self.color, self.accent, smoothstep(0.05, 0.75, u))
        dust_w = w0 * (1.0 + 1.3 * u)
        dust_d = (d - self.drift * u ** 1.2) / dust_w
        dust = np.exp(-(dust_d ** 2)) * smoothstep(2.1, 1.5, np.abs(dust_d))
        dust_k = 0.42 * (1 - u) ** 1.1 * smoothstep(0.0, 0.2, u) * dust * fade
        dust_hue = _ramp(self.color, self.accent, 0.45 + 0.55 * u)
        glow_k = ((1 - u) ** 1.5 * core + 0.3 * (1 - u) ** 2 * sheen) * fade
        hot_k = np.exp(-((d / (0.42 * w)) ** 2)) * (1 - u) ** 3 * np.exp(-behind / (2.2 * self.ch)) * fade
        if self.dark:
            cv.put_at(idx, dust_hue, dust_k)
            cv.put_at(idx, hue, glow_k * 1.05)
            cv.put_at(idx, self.hot, hot_k * 1.3)
        else:
            cv.put_at(idx, dust_hue, dust_k * 1.2)
            cv.put_at(idx, hue, np.minimum(1, glow_k * 0.75))
            cv.put_at(idx, self.hot, hot_k * 0.8)

    def _sparkles(self, cv: _Canvas, t: float, fade: float) -> None:
        for i in range(len(self.shed_t)):
            age = t - self.shed_t[i]
            if not 0 <= age < self.shed_life[i]:
                continue
            env = min(1.0, age / 0.05) * (1 - age / self.shed_life[i]) ** 1.3
            twinkle = 0.5 + 0.5 * math.cos(self.shed_phase[i] + 2 * math.pi * self.shed_hz[i] * age)
            x, y = self.shed_at[i] + self.shed_v[i] * (1 - math.exp(-age / 0.35)) * 0.35
            k = self.shed_k[i] * env * (0.35 + 0.65 * twinkle) * fade * (1.7 if self.dark else 1.0)
            spike = 0.9 * self.ch if self.shed_spike[i] else 0.0
            _sparkle(cv, float(x), float(y), k, self.accent if self.shed_accent[i] else self.color,
                     self.small * (1.25 if spike else 1.0), spike)

    def _head(self, cv: _Canvas, x: float, y: float, k: float) -> None:
        """A white-hot core in a halo of colour, and a faint flare across it."""
        ch = self.ch
        win = _win(cv.stage, x, y, 2.6 * ch, 2.0 * ch)
        if win is None:
            return
        dx, dy = win[2] - x, win[3] - y
        r = np.sqrt(dx * dx + dy * dy)
        coma = np.exp(-((r / (1.1 * ch)) ** 2)) * smoothstep(2.0 * ch, 1.3 * ch, r)
        halo = np.exp(-((r / (0.5 * ch)) ** 2))
        inner = np.exp(-((r / (0.22 * ch)) ** 2))
        kernel = smoothstep(0.22 * ch + 0.7, 0.22 * ch - 0.7, r)
        flare = (np.exp(-((dy / (0.06 * ch + 0.35)) ** 2) - np.abs(dx) / (0.7 * ch))
                 * smoothstep(2.6 * ch, 1.6 * ch, np.abs(dx)))
        if self.dark:
            cv.put(win, self.color, (coma * 0.3 + halo * 0.9 + inner * 1.2 + flare * 0.5) * k)
            cv.put(win, self.hot, (kernel * 1.8 + inner * 0.6 + flare * 0.25) * k)
        else:
            cv.put(win, self.color, (coma * 0.15 + halo * 0.45 + flare * 0.3) * k)
            cv.put(win, self.hot, np.maximum(kernel, inner * 0.8) * k)

    def _landing(self, cv: _Canvas, since: float, fade: float) -> None:
        """A flash with a flare across it, a ring and its echo, and a burst of sparkles."""
        ch = self.ch
        x, y = float(self.b[0]), float(self.b[1])
        flash = math.exp(-(((since - 0.04) / 0.07) ** 2)) if since < 0.04 else math.exp(-(since - 0.04) / 0.12)
        ring_k = (1 - min(1.0, max(0.0, since) / 0.45)) ** 1.4 * (since > 0)
        win = _win(cv.stage, x, y, 3.6 * ch)
        if win is not None:
            dx, dy = win[2] - x, win[3] - y
            r = np.sqrt(dx * dx + dy * dy)
            bloom = np.exp(-((r / (0.9 * ch)) ** 2)) * smoothstep(2.4 * ch, 1.6 * ch, r)
            core = np.exp(-((r / (0.32 * ch)) ** 2))
            flare = (np.exp(-((dy / (0.07 * ch + 0.35)) ** 2) - np.abs(dx) / (0.9 * ch))
                     + 0.6 * np.exp(-((dx / (0.07 * ch + 0.35)) ** 2) - np.abs(dy) / (0.6 * ch)))
            flare *= smoothstep(3.5 * ch, 2.2 * ch, r)
            rr = ch * (0.4 + 2.4 * ease_out(max(0.0, since) / 0.45))
            ring_w = 0.1 * ch + 0.5
            edge = smoothstep(3.5 * ch, 3.1 * ch, r)
            ring = np.exp(-(((r - rr) / ring_w) ** 2)) * edge
            echo = np.exp(-(((r - 0.7 * rr) / (1.6 * ring_w)) ** 2)) * edge
            if self.dark:
                hue = _ramp(self.color, self.accent, np.clip(r / (3 * ch), 0, 1))
                cv.put(win, self.color, (bloom * 1.1 + flare * 0.6) * flash * fade)
                cv.put(win, self.hot, (core * 1.9 + flare * 0.4) * flash * fade)
                cv.put(win, hue, (ring * 1.1 + echo * 0.35) * ring_k * fade)
            else:
                cv.put(win, self.color, bloom * 0.6 * flash * fade)
                cv.put(win, self.hot, core * flash * fade)
                cv.put(win, self.accent, (ring + echo * 0.3) * ring_k * fade)
        for i in range(len(self.burst_dir)):
            if not 0 <= since < self.burst_life[i]:
                continue
            life = since / self.burst_life[i]
            go = self.burst_far[i] * ease_out(life ** 0.7)
            _sparkle(cv, x + math.cos(self.burst_dir[i]) * go, y + math.sin(self.burst_dir[i]) * go,
                     (1 - life) ** 1.2 * 1.5 * fade, self.accent if i % 3 == 0 else self.color, self.small * 1.15,
                     0.7 * ch if i % 4 == 0 else 0.0)


def comet(stage: Stage) -> Frames:
    """A comet flies from the anchor to the target (``stage.to``; across the anchor without one): a white-hot
    head, a tail that glows where it has been, sparkles shed on the way, and a bloom where it lands."""
    c = _Comet(stage)
    mask = border(stage)
    # a frame every 35 to 40 ms, fewer when the comet is big in pixels, to keep the upload near 500 KiB
    upload = 0.09 * c.ch * (c.dist + 8 * c.ch)  # KiB at 48 frames, about
    times, gaps_ms = _timeline(c.total * 1000, min(round(c.total / 0.035), max(36, round(48 * 500 / upload))))
    end = times[-1]
    out = []
    for t in times[:-1]:
        cv = _Canvas(stage)
        c.draw(cv, t, 1.0 - float(smoothstep(end - 0.17, end, np.float32(t))))
        out.append(cv.frame(mask))
    out.append(_Canvas(stage).frame(mask))
    return out, gaps_ms


# ======================================================== shooting star ==


def shooting_star(stage: Stage) -> Frames:
    """One thin, quick streak across the upper part of the anchor, with a glint as it burns brightest."""
    cw, ch = stage.cell
    rnd = rng(stage)
    mask = border(stage)
    x, y, w, h = stage.at
    span = float(np.clip(0.6 * w, 7 * cw, 34 * cw))
    drop = span * math.tan(math.radians(rnd.uniform(10, 20)))
    edge = 1.2 * ch
    x0 = float(np.clip(x + (w - span) * rnd.uniform(0.2, 0.45), edge, max(edge, stage.width - edge - span)))
    y0 = y + float(np.clip(0.3 * h, 0.5 * ch, 3 * ch)) - 0.5 * drop
    y0 = float(np.clip(y0, edge, max(edge, stage.height - edge - drop)))
    ax, bx = (x0 + span, x0) if rnd.random() < 0.3 else (x0, x0 + span)
    ay, by = y0, y0 + drop
    # on a light theme the foreground is the text's ink, so it is drawn in the accent there
    main = stage.color if stage.dark else stage.accent
    color, tint, hot = _ink(stage, main), _ink(stage, stage.accent), _hot(stage, main)
    width = max(0.7 * unit(stage), 0.11 * ch)
    run, total, peak = 0.46, 0.62, 0.62  # it glints at ``peak`` of the way, where it burns brightest
    times, gaps_ms = _timeline(total * 1000, round(total / 0.031))

    def where(t: float) -> tuple[float, float]:
        f = 1 - (1 - min(1.0, max(0.0, t) / run)) ** 1.6
        return ax + (bx - ax) * f, ay + (by - ay) * f

    out = []
    for t in times[:-1]:
        cv = _Canvas(stage)
        p = min(1.0, t / run)
        burn = float(smoothstep(0.0, 0.25, np.float32(p)) * (1 - smoothstep(peak, 1.0, np.float32(p))))
        head = where(t)
        _streak(cv, where(t - 0.17), head, width, burn * 1.1, color, tint, hot, 0.9 if stage.dark else 0.0)
        if p >= 1.0:  # what is left of the trail after the head burns out fades where it was
            left = 1 - float(smoothstep(run, total - 0.04, np.float32(t)))
            _streak(cv, where(run - 0.17), (bx, by), width * 0.8, 0.35 * left, color, tint, hot, 0.0)
        glint = math.exp(-(((p - peak) / 0.09) ** 2))
        if glint > 0.01:
            _sparkle(cv, *head, glint * (1.6 if stage.dark else 1.0), hot if stage.dark else color, width * 1.4,
                     1.3 * ch)
            _sparkle(cv, *head, glint * 0.5, color, width * 3.5)
        out.append(cv.frame(mask))
    out.append(_Canvas(stage).frame(mask))
    return out, gaps_ms


# =============================================================== meteors ==


def meteors(stage: Stage) -> Frames:
    """A shower: streaks from a common radiant off one of the window's top corners, staggered, the ones far
    from it longer; a few burn out in a tiny flash, one or two in the accent colour."""
    _, ch = stage.cell
    rnd = rng(stage)
    mask = border(stage)
    W, H = stage.width, stage.height
    diag = math.hypot(W, H)
    rx, ry = (-0.1 * W if rnd.random() < 0.5 else 1.1 * W), -0.35 * H
    n = int(rnd.integers(8, 11))
    # where each one passes: spread over the window (a jittered grid), and when (staggered, shuffled in place)
    cells = rnd.permutation(12)[:n]
    mx = (cells % 4 + rnd.uniform(0.15, 0.85, n)) / 4 * 0.88 * W + 0.06 * W
    my = (cells // 4 + rnd.uniform(0.15, 0.85, n)) / 3 * 0.76 * H + 0.12 * H
    life = rnd.uniform(0.36, 0.56, n)
    starts = (np.arange(n) + rnd.uniform(0.0, 0.9, n)) * 1.2 / n
    starts -= starts[0] + 0.02
    reach = np.hypot(mx - rx, my - ry)
    length = (0.3 + 0.7 * reach / reach.max()) * rnd.uniform(0.28, 0.42, n) * diag
    k = rnd.uniform(0.7, 1.1, n)
    width = max(0.7 * unit(stage), 0.12 * ch) * rnd.uniform(0.8, 1.3, n)
    gold = rnd.permutation(n)[: 2 if n >= 9 else 1]
    flash = rnd.permutation(n)[:3]
    k[flash] = np.maximum(k[flash], 0.9)
    width[flash] *= 1.15
    color, accent = _ink(stage, stage.color), _ink(stage, stage.accent)
    hot, hot_gold = _hot(stage, stage.color), _hot(stage, stage.accent)
    tint = color * F32(0.6) if stage.dark else color
    tint_gold = accent * F32(0.6) if stage.dark else accent
    total = float(np.max(starts + life * 1.45)) + 0.08
    times, gaps_ms = _timeline(total * 1000, round(total / 0.04))
    out = []
    for t in times[:-1]:
        cv = _Canvas(stage)
        for i in range(n):
            age = t - starts[i]
            trail = 0.45 * life[i]
            if not 0 <= age < life[i] + trail:
                continue
            ux, uy = (mx[i] - rx) / reach[i], (my[i] - ry) / reach[i]
            p = min(1.0, age / life[i])
            go = length[i] * (-0.5 + (1 - (1 - p) ** 1.4))
            back = length[i] * (-0.5 + (1 - (1 - min(1.0, max(0.0, age - trail) / life[i])) ** 1.4))
            head = mx[i] + ux * go, my[i] + uy * go
            tail = mx[i] + ux * back, my[i] + uy * back
            burns_out = i in flash
            # it flares up and dims as it goes (or burns out bright), then its trail fades where it was
            burn = float(smoothstep(0.0, 0.2, np.float32(p)))
            if not burns_out:
                burn *= 1 - 0.7 * float(smoothstep(0.5, 1.0, np.float32(p)))
            if p >= 1:
                burn = (0.6 if burns_out else 0.3) * (1 - float(smoothstep(life[i], life[i] + trail, np.float32(age))))
            c, h, tn = (accent, hot_gold, tint_gold) if i in gold else (color, hot, tint)
            _streak(cv, tail, head, float(width[i]), k[i] * burn, c, tn, h, 0.9 if stage.dark else 0.0, 0.35 * burn)
            if burns_out:
                pop = math.exp(-(((age - life[i]) / 0.06) ** 2))
                if pop > 0.01:
                    _sparkle(cv, head[0], head[1], pop * (1.6 if stage.dark else 1.0), h, float(width[i]) * 1.5,
                             1.1 * ch)
                    _sparkle(cv, head[0], head[1], pop * 0.5, c, float(width[i]) * 4.5)
        out.append(cv.frame(mask))
    out.append(_Canvas(stage).frame(mask))
    return out, gaps_ms


def _ray_span(x: float, y: float, ux: float, uy: float, width: float, height: float) -> tuple[float, float] | None:
    """Where a ray from (x, y) along (ux, uy) is inside the rectangle (0, 0, width, height): from and to, along it."""
    lo, hi = 0.0, math.inf
    for origin, step, size in ((x, ux, width), (y, uy, height)):
        if abs(step) < 1e-9:
            if not 0 <= origin <= size:
                return None
            continue
        a, b = (0 - origin) / step, (size - origin) / step
        lo, hi = max(lo, min(a, b)), min(hi, max(a, b))
    return (lo, hi) if hi > lo else None


# ================================================================== warp ==


def warp(stage: Stage) -> Frames:
    """The jump to hyperspace: star points around the anchor's centre stretch into streaks, longer further out,
    then a flash at the centre and they shoot off the stage, faster and faster."""
    cw, ch = stage.cell
    rnd = rng(stage)
    mask = border(stage)
    cx, cy = centre(stage.at)
    W, H = stage.width, stage.height
    _, _, aw, ah = stage.at
    # the star field: the anchor and a margin around it (the kind's grow), as far as the stage goes
    field = math.hypot(aw / 2 + 10 * cw, ah / 2 + 4 * ch)
    n = int(np.clip(W * H / (ch * ch * 2.2), 40, 72))
    th = rnd.uniform(0, 2 * math.pi, n)
    r0 = np.sqrt(rnd.uniform(0.01, 1.0, n)) * field * 0.95 + 0.6 * ch
    px, py = cx + np.cos(th) * r0, cy + np.sin(th) * r0
    keep = (px > ch) & (px < W - ch) & (py > ch) & (py < H - ch)
    th, r0 = th[keep], r0[keep]
    n = len(th)
    exits = np.array([(_ray_span(cx, cy, math.cos(a), math.sin(a), W, H) or (0.0, 0.0))[1] for a in th])
    appear = rnd.uniform(-0.05, 0.18, n)
    k_star = rnd.uniform(0.4, 1.0, n)
    phase = rnd.uniform(0, 2 * math.pi, n)
    tinted = rnd.random(n) < 0.3
    color, accent, hot = _ink(stage, stage.color), _ink(stage, stage.accent), _hot(stage, stage.color)
    pale = color * F32(0.55) + hot * F32(0.45) if stage.dark else color
    width0 = max(0.6 * unit(stage), 0.08 * ch)
    jump, total = 0.68, 1.1
    times, gaps_ms = _timeline(total * 1000, round(total / 0.032))

    def head_out(t: float) -> float:
        """How far the heads have come, as a log of the radius: a slow stretch, then the jump."""
        stretch = 0.6 * min(1.0, max(0.0, t - 0.2) / (jump - 0.2)) ** 2
        return stretch + 5.0 * max(0.0, t - jump + 0.03) ** 1.6 / 0.5 ** 1.6

    def tail_out(t: float) -> float:
        """The tails stay put while the stars stretch, then follow the heads off the stage."""
        return 4.6 * max(0.0, t - jump) ** 1.8 / 0.42 ** 1.8

    out = []
    for t in times[:-1]:
        cv = _Canvas(stage)
        far, near = head_out(t), tail_out(t)
        for i in range(n):
            r_tail = r0[i] * math.exp(near)
            if r_tail > exits[i] + ch:
                continue
            r_head = min(r0[i] * math.exp(far), exits[i] + 3 * ch)
            ux, uy = math.cos(th[i]), math.sin(th[i])
            on = min(1.0, max(0.0, (t - appear[i]) / 0.12))
            stretch = min(1.0, (r_head - r_tail) / (2.5 * ch))
            twinkle = 0.7 + 0.3 * math.cos(phase[i] + 15 * t)
            k = k_star[i] * on * (twinkle + (1.25 - twinkle) * stretch)
            c = accent if tinted[i] else color
            if r_head - r_tail < 0.3 * ch:
                _sparkle(cv, cx + ux * r_head, cy + uy * r_head, k * (1.3 if stage.dark else 1.0),
                         c if tinted[i] or not stage.dark else pale, width0 * 1.3)
            else:
                _streak(cv, (cx + ux * r_tail, cy + uy * r_tail), (cx + ux * r_head, cy + uy * r_head),
                        width0 * (1 + 0.25 * stretch), k, c, c * F32(0.5) if stage.dark else c, hot,
                        0.9 * stretch if stage.dark else 0.0)
        _warp_core(cv, cx, cy, ch, color, accent, hot, t, jump)
        out.append(cv.frame(mask))
    out.append(_Canvas(stage).frame(mask))
    return out, gaps_ms


def _warp_core(cv: _Canvas, x: float, y: float, ch: float, color: Array, accent: Array, hot: Array, t: float,
               jump: float) -> None:
    """The warp's centre: a glow that gathers as the stars stretch, then the flash of the jump, a flare across
    it and a ring."""
    since = t - jump
    charge = float(smoothstep(0.2, jump, np.float32(t))) ** 2 * (since < 0)
    flash = math.exp(-((since / 0.035) ** 2)) if since < 0 else math.exp(-since / 0.09)
    ring_k = (1 - min(1.0, max(0.0, since) / 0.3)) * (since > 0)
    if max(charge, flash, ring_k) < 0.01:
        return
    win = _win(cv.stage, x, y, 4.0 * ch)
    if win is not None:
        dx, dy = win[2] - x, win[3] - y
        r = np.sqrt(dx * dx + dy * dy)
        bloom = np.exp(-((r / (1.1 * ch)) ** 2)) * smoothstep(3.0 * ch, 2.0 * ch, r)
        core = np.exp(-((r / (0.35 * ch)) ** 2))
        room = min(x, y, cv.stage.width - x, cv.stage.height - y) - 0.6 * ch  # the ring stays on the stage
        rr = min(ch * 3.7, max(room, 1.5 * ch)) * (0.14 + 0.86 * ease_out(max(0.0, since) / 0.3))
        ring = np.exp(-(((r - rr) / (0.12 * ch + 0.5)) ** 2)) * smoothstep(4.0 * ch, 3.6 * ch, r)
        if cv.dark:
            cv.put(win, color, bloom * (1.2 * flash + 0.35 * charge))
            cv.put(win, hot, core * (2.0 * flash + 0.5 * charge))
            cv.put(win, accent, ring * ring_k * 0.9)
        else:
            cv.put(win, color, bloom * (0.7 * flash + 0.25 * charge))
            cv.put(win, hot, core * (flash + 0.4 * charge))
            cv.put(win, accent, ring * ring_k)
    reach = min(7.0 * ch, x - ch, cv.stage.width - x - ch)
    if flash > 0.02 and reach > ch:
        win = _win(cv.stage, x, y, reach, 0.5 * ch)
        if win is not None:
            dx, dy = np.abs(win[2] - x), np.abs(win[3] - y)
            flare = np.exp(-((dy / (0.05 * ch + 0.4)) ** 2) - dx / (0.35 * reach)) * smoothstep(reach, 0.6 * reach, dx)
            cv.put(win, color, flare * 0.9 * flash)
            if cv.dark:
                cv.put(win, hot, flare * 0.5 * flash)


KINDS = {
    "comet": Flourish(comet, grow=(4, 3), color="primary", accent="secondary",
                      about="something moved or arrived: it flies from one widget to another"),
    "shooting-star": Flourish(shooting_star, grow=(6, 2), color="foreground", accent="primary",
                              about="small news: one quick streak over a widget"),
    "meteors": Flourish(meteors, screen=True, color="primary", accent="warning",
                        about="a lot happened at once: a shower across the window"),
    "warp": Flourish(warp, grow=(10, 4), color="primary", accent="secondary",
                     about="launch: a deploy or a release starts"),
}
