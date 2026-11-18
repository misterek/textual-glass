"""``LineChart``: numbers over time as glowing lines (the ``Lines`` item), or braille lines without glass.

It takes named series of ``(unix seconds, value)`` points and a time window, and knows nothing about where they
come from.

With glass each series is a glowing line in its own colour, the newest point a bright dot, with a soft fill under
the first series and faint guides at the value labels. Without glass the same lines are braille dots in the same
cells, so the layout is identical in both modes::

    ● api 212  ● web 180  ● db 95          <- legend: more than one series (name, last value)
    250 ┊        ╭╮   ╭─╮                  <- y labels: 2 or 3, short numbers
    125 ┊ ──╮╭──╯ ╰──╯  ╰──●
      0 ┊   ╰╯                             <- the bottom of the scale
     ms -15m     -10m     -5m       now    <- the unit under the labels, time along the bottom

It redraws only when its data changes (a NaN-safe comparison), never on a clock (rule 9): the time labels are
relative to the window's end, which the caller derives from its data.
"""

from __future__ import annotations

import colorsys
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from textual.strip import Strip

from ..items import Lines
from ..palette import RGB, Palette, mix
from ..render import Colour, Item
from ..scene import PixelContext
from .base import DIM, FAINT, SOFT, Part, PixelWidget, ThemeColours, hex_colour, strip, tone

__all__ = ["MAX_LINES", "LineChart", "nice_ceil", "round_in", "scale_for", "series_colours", "short_number",
           "time_ticks"]

#: The most series a chart draws; later ones are left out.
MAX_LINES = 8

Point = tuple[float, float]
Run = tuple[Point, ...]
SeriesArg = Sequence[tuple[str, Sequence[tuple[float, float]]]]


# ================================================================================================= numbers ==


def short_number(value: float) -> str:
    """A short label: ``0``, ``0.42``, ``4.2``, ``42.5``, ``350``, ``1.2k``, ``12k``, ``3.4M``."""
    if not math.isfinite(value):
        return "–"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    a = abs(value)
    for div, suffix in ((1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, "")):
        if a >= div * 0.9995 or div == 1.0:
            text = _three(a / div)
            if suffix and text == "1000":  # 999.96k rounds up: say 1M
                continue
            return sign + text + suffix
    return sign + _three(a)  # pragma: no cover - the loop always returns


def _three(a: float) -> str:
    """About three significant digits, trailing zeros dropped."""
    if a >= 99.95:
        return f"{a:.0f}"
    if a >= 9.995:
        text = f"{a:.1f}"
    elif a >= 0.9995:
        text = f"{a:.2f}"
    else:
        text = f"{a:.3g}"
        if "e" in text:
            return text
    return text.rstrip("0").rstrip(".") if "." in text else text


_NICE = (1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)


def nice_ceil(value: float) -> float:
    """The smallest round number at or above ``value`` (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6 or 8 times a power of 10)."""
    if value <= 0 or not math.isfinite(value):
        return 1.0
    power = 10.0 ** math.floor(math.log10(value))
    for step in _NICE:
        if step * power >= value * (1 - 1e-9):
            return step * power
    return 10 * power  # pragma: no cover


def scale_for(values: Iterable[float], ymin: float | None = None, ymax: float | None = None) -> tuple[float, float]:
    """The value range a chart draws: from ``ymin`` (default 0, or the data's minimum rounded down when it is
    below 0) to ``ymax`` (default a little over the top, on a round number)."""
    finite = [v for v in values if math.isfinite(v)]
    low = min(finite, default=0.0)
    high = max(finite, default=0.0)
    bottom = ymin if ymin is not None else (-nice_ceil(-low * 1.05) if low < 0 else 0.0)
    if ymax is not None:
        top = ymax
    elif high <= bottom:
        top = bottom + (nice_ceil(abs(bottom)) if bottom else 1.0)
    elif bottom < 0 <= high:
        top = nice_ceil(high * 1.05) if high > 0 else 0.0
        if top == 0.0:
            top = -bottom * 0.1 if bottom else 1.0
    else:
        top = bottom + nice_ceil((high - bottom) * 1.05)
    if top <= bottom:
        top = bottom + 1.0
    return bottom, top


def round_in(value: float, slack: float) -> float:
    """The roundest number within ``slack`` of ``value`` (a label for a row of a chart, which stands for a range
    of values): 175 rather than 166.7 for a row that spans 150 to 183."""
    if slack <= 0 or not math.isfinite(value):
        return value
    power = 10.0 ** math.ceil(math.log10(slack * 2))
    for _ in range(12):
        for step in (5.0, 2.5, 2.0, 1.0):
            grain = power * step / 10
            nearest = round(value / grain) * grain
            if abs(nearest - value) <= slack:
                return nearest
        power /= 10
    return value


# ============================================================================================ time labels ==

_STEPS = (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800)


def _ago(seconds: float) -> str:
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes}m{secs}s" if secs else f"{minutes}m"
    if total < 86400:
        hours, minutes = divmod(total // 60, 60)
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(total // 3600, 24)
    return f"{days}d{hours}h" if hours else f"{days}d"


def time_ticks(span: float, width: int) -> list[tuple[float, str]]:
    """Time labels along a chart ``width`` cells wide that shows ``span`` seconds: ``(u, label)`` with ``u`` from
    0 (the start) to 1 (``now``), on round steps back from the end."""
    if span <= 0 or width < 3:
        return []
    ticks: list[tuple[float, str]] = [(1.0, "now")]
    room = max(1, width // 12)  # a label every 12 cells at most
    step: float = next((float(s) for s in _STEPS if span / s <= room), span / room)
    k = 1
    while k * step <= span * (1 + 1e-9) and k < 100:
        ticks.append((1 - k * step / span, f"-{_ago(k * step)}"))
        k += 1
    return ticks


# ================================================================================================ colours ==


def _rotate(c: RGB, turn: float) -> RGB:
    h, light, s = colorsys.rgb_to_hls(c[0] / 255, c[1] / 255, c[2] / 255)
    r, g, b = colorsys.hls_to_rgb((h + turn) % 1.0, light, s)
    return (round(r * 255), round(g * 255), round(b * 255))


def series_colours(palette: Palette, count: int = MAX_LINES) -> list[RGB]:
    """Distinct line colours from the theme: primary, secondary, accent and success, then blends of them."""
    out = [palette.primary, palette.secondary, palette.accent, palette.success,
           mix(palette.primary, palette.secondary, 0.5), mix(palette.accent, palette.error, 0.5),
           mix(palette.primary, palette.success, 0.5), mix(palette.secondary, palette.error, 0.5)]
    while len(out) < count:
        out.append(_rotate(palette.primary, 0.13 * len(out)))
    return out[:count]


# ================================================================================================= widget ==

_BRAILLE = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # [row][col] dot bits of a cell


@dataclass(frozen=True)
class _Line:
    name: str
    runs: tuple[Run, ...]  # (unix seconds, value), no NaN
    last: float | None


@dataclass(frozen=True)
class _Layout:
    legend: int  # rows
    rows: int  # plot rows
    axis: int  # time rows
    gutter: int  # cells left of the plot (labels and a space)
    width: int  # plot cells
    labels: tuple[tuple[int, str], ...]  # (plot row, text)


class LineChart(PixelWidget):
    """Numbers over time: a glowing line per series, or braille lines without glass.

    ``set_series(series, start=..., end=..., unit="", ymin=None, ymax=None, colors=None)``: ``series`` is up to
    :data:`MAX_LINES` ``(name, points)`` pairs, points ``(unix seconds, value)`` oldest first, NaN for a gap. The
    chart shows ``start`` to ``end`` (``end`` is labelled "now"), values from ``ymin`` (default 0, or below the
    data when it dips under 0) to ``ymax`` (default a little over the top, on a round number). ``colors`` are the
    lines' colours (default: distinct colours from the theme); ``None`` keeps them, ``THEME`` goes back to the
    theme's. Nothing redraws when the same data comes again.
    """

    DEFAULT_CSS = """
    LineChart { height: 1fr; min-height: 3; }
    """

    def __init__(self, *, colors: Sequence[Colour] | None = None, name: str | None = None, id: str | None = None,
                 classes: str | None = None) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.lines: tuple[_Line, ...] = ()
        self.start = 0.0
        self.end = 0.0
        self.unit = ""
        self.ymin: float | None = None
        self.ymax: float | None = None
        self.tones: tuple[Colour, ...] | None = tuple(colors) if colors else None
        self.scale = (0.0, 1.0)
        self._braille_cache: tuple[object, tuple[list[list[int]], list[list[int]]]] | None = None

    # -- data -------------------------------------------------------------------------------------------------

    def set_series(self, series: SeriesArg, *, start: float, end: float, unit: str = "", ymin: float | None = None,
                   ymax: float | None = None, colors: Sequence[Colour] | ThemeColours | None = None) -> None:
        """Show ``series`` over ``start`` to ``end`` (unix seconds); see the class docstring."""
        lines = tuple(_normalise(name, points) for name, points in list(series)[:MAX_LINES])
        tones: tuple[Colour, ...] | None
        if colors is None:
            tones = self.tones
        elif isinstance(colors, ThemeColours):
            tones = None
        else:
            tones = tuple(colors) or None
        new = (lines, float(start), float(end), unit, ymin, ymax, tones)
        if new == (self.lines, self.start, self.end, self.unit, self.ymin, self.ymax, self.tones):
            return
        self.lines, self.start, self.end, self.unit, self.ymin, self.ymax, self.tones = new
        self.scale = scale_for((v for line in lines for run in line.runs for _, v in run), ymin, ymax)
        self.refresh()

    def colours(self, palette: Palette) -> list[Colour]:
        """Each line's colour, in order."""
        if self.tones:
            return [self.tones[k % len(self.tones)] for k in range(max(1, len(self.lines)))]
        return list(series_colours(palette, max(1, len(self.lines))))

    # -- layout -----------------------------------------------------------------------------------------------

    def layout_for(self, width: int, height: int) -> _Layout:
        legend = 1 if len(self.lines) > 1 and height >= 4 else 0
        axis = 1 if height - legend >= 3 else 0
        rows = max(0, height - legend - axis)
        bottom, top = self.scale
        if rows >= 5:
            label_rows = [0, (rows - 1) // 2, rows - 1]
        elif rows >= 2:
            label_rows = [0, rows - 1]
        elif rows == 1:
            label_rows = [0]
        else:
            label_rows = []
        step = (top - bottom) / (rows - 1) if rows > 1 else 0.0
        labels = tuple((r, short_number(round_in(top - step * r, 0.0 if r in (0, rows - 1) else 0.25 * step)))
                       for r in label_rows)
        gutter = max([len(text) for _, text in labels] + [min(len(self.unit), 6) if axis else 0], default=0)
        gutter = gutter + 1 if gutter else 0
        if width - gutter < 8:
            gutter, labels = 0, ()
        return _Layout(legend, rows, axis, gutter, max(0, width - gutter), labels)

    def _plot_v(self, value: float) -> float:
        bottom, top = self.scale
        return (value - bottom) / (top - bottom)

    def _plot_u(self, t: float) -> float:
        span = self.end - self.start
        return (t - self.start) / span if span > 0 else 1.0

    def _visible_runs(self, line: _Line) -> tuple[tuple[Point, ...], ...]:
        """A line's runs as ``(u, v)``, cut to just past the window."""
        out: list[tuple[Point, ...]] = []
        for run in line.runs:
            pts = [(self._plot_u(t), self._plot_v(v)) for t, v in run]
            keep = [i for i, (u, _) in enumerate(pts) if -0.0001 <= u <= 1.0001]
            if not keep:
                continue
            lo, hi = max(0, keep[0] - 1), min(len(pts), keep[-1] + 2)  # one point either side, for the slope
            out.append(tuple((round(u, 5), round(min(1.0, max(0.0, v)), 5)) for u, v in pts[lo:hi]))
        return tuple(out)

    # -- text -------------------------------------------------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        width, height = self.size
        pal = self.theme_palette
        if not self.lines or width < 4:
            return Strip.blank(width)
        lay = self.layout_for(width, height)
        colours = [hex_colour(c) for c in self.colours(pal)]
        if y < lay.legend:
            return strip(self._legend(width, colours, pal), width)
        row = y - lay.legend
        if row < lay.rows:
            label = dict(lay.labels).get(row, "")
            parts: list[Part] = []
            if lay.gutter:
                parts.append((label.rjust(lay.gutter - 1) + " ", tone(pal, DIM)))
            if self.glass_on:
                parts.append((" " * lay.width, ""))
            else:
                parts.extend(self._braille_row(row, lay, colours, pal))
            return strip(parts, width)
        if lay.axis and row == lay.rows:
            return strip(self._axis(lay, pal), width)
        return Strip.blank(width)

    def _legend(self, width: int, colours: list[str], pal: Palette) -> list[Part]:
        parts: list[Part] = []
        used = 0
        shown = 0
        for k, line in enumerate(self.lines):
            value = short_number(line.last) if line.last is not None else "–"
            entry: list[Part] = [("● ", colours[k]), (line.name or "value", tone(pal, SOFT)), (" ", ""),
                                 (value, f"bold {colours[k]}")]
            size = sum(len(t) for t, _ in entry) + (2 if parts else 0)
            rest = len(self.lines) - k - 1
            more = len(f"  +{rest}") if rest else 0
            if used + size + more > width:
                break
            if parts:
                parts.append(("  ", ""))
            parts.extend(entry)
            used += size
            shown += 1
        if shown < len(self.lines):
            parts.append((("  " if parts else "") + f"+{len(self.lines) - shown}", tone(pal, FAINT)))
        return parts

    def _axis(self, lay: _Layout, pal: Palette) -> list[Part]:
        cells = [" "] * lay.width
        taken = [False] * lay.width
        for u, label in time_ticks(self.end - self.start, lay.width):
            centre = int(0.3 + u * (lay.width - 1.0))
            if u >= 1.0:
                begin = lay.width - len(label)
            elif u <= 0.0:
                begin = max(0, centre)
            else:
                begin = centre - len(label) // 2
            begin = max(0, min(lay.width - len(label), begin))
            span = range(max(0, begin - 1), min(lay.width, begin + len(label) + 1))
            if any(taken[i] for i in span):
                continue
            for i, ch in enumerate(label):
                cells[begin + i] = ch
                taken[begin + i] = True
        unit = self.unit[: max(0, lay.gutter - 1)]
        parts: list[Part] = []
        if lay.gutter:
            parts.append((unit.rjust(lay.gutter - 1) + " ", tone(pal, FAINT)))
        parts.append(("".join(cells), tone(pal, FAINT)))
        return parts

    def _braille_row(self, row: int, lay: _Layout, colours: list[str], pal: Palette) -> list[Part]:
        bits, owner = self._braille(lay)
        guide_rows = {r for r, _ in lay.labels}
        parts: list[Part] = []
        faint = tone(pal, FAINT)
        for cx in range(lay.width):
            b = bits[row][cx]
            if b:
                parts.append((chr(0x2800 + b), colours[owner[row][cx]]))
            elif row in guide_rows and cx % 3 == 1:
                parts.append(("·", faint))
            else:
                parts.append((" ", ""))
        return parts

    def _braille(self, lay: _Layout) -> tuple[list[list[int]], list[list[int]]]:
        """Every plot cell's braille dots and the series that owns its colour (the first series on top)."""
        key = (lay, self.lines, self.start, self.end, self.scale)
        cached = self._braille_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        w, h = lay.width, lay.rows
        bits = [[0] * w for _ in range(h)]
        owner = [[0] * w for _ in range(h)]
        sub_w, sub_h = 2 * w, 4 * h

        def dot(sx: int, sy: int, k: int) -> None:
            if 0 <= sx < sub_w and 0 <= sy < sub_h:
                cy, cx = sy // 4, sx // 2
                bits[cy][cx] |= _BRAILLE[sy % 4][sx % 2]
                owner[cy][cx] = k

        for k in reversed(range(len(self.lines))):
            for run in self._visible_runs(self.lines[k]):
                pts = [(round((0.3 + u * (w - 1.0)) * 2 - 0.5), round(((h - 1) * (1 - v) + 0.5) * 4 - 0.5))
                       for u, v in run]
                pts = [(min(sub_w - 1, max(0, x)), min(sub_h - 1, max(0, y))) for x, y in pts]
                if len(pts) == 1:
                    dot(*pts[0], k)
                    continue
                for (xa, ya), (xb, yb) in zip(pts, pts[1:], strict=False):
                    if xb <= xa:
                        for yy in range(min(ya, yb), max(ya, yb) + 1):
                            dot(xa, yy, k)
                        continue
                    prev = ya
                    for xx in range(xa, xb + 1):
                        yy = round(ya + (yb - ya) * (xx - xa) / (xb - xa))
                        for y2 in range(min(prev, yy), max(prev, yy) + 1):
                            dot(xx, y2, k)
                        prev = yy
        self._braille_cache = (key, (bits, owner))
        return bits, owner

    # -- pixels -----------------------------------------------------------------------------------------------

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        r = self.content_region
        if not self.lines or r.width < 4:
            return ()
        lay = self.layout_for(r.width, r.height)
        if lay.rows < 1 or lay.width < 2:
            return ()
        runs = tuple(self._visible_runs(line) for line in self.lines)
        bottom, top = self.scale
        base = min(1.0, max(0.0, self._plot_v(0.0))) if bottom < 0 < top else 0.0
        guides = tuple(round(1 - row / (lay.rows - 1), 5) for row, _ in lay.labels if lay.rows > 1)
        ticks = tuple(u for u, _ in time_ticks(self.end - self.start, lay.width) if u < 1.0) if lay.axis else ()
        rect = (float(r.x + lay.gutter), float(r.y + lay.legend), float(lay.width), float(lay.rows))
        colours = tuple(self.colours(ctx.palette))
        return (Lines(rect, runs, colours, True, guides, base, ticks, ident=f"lines:{id(self)}",
                      clip=self.visible_clip()),)


def _normalise(name: str, points: Sequence[tuple[float, float]]) -> _Line:
    """A series as runs without NaN (NaN is never equal to itself, so it would make equal data look new)."""
    runs: list[Run] = []
    current: list[Point] = []
    last: float | None = None
    for t, v in points:
        t, v = float(t), float(v)
        if not (math.isfinite(t) and math.isfinite(v)):
            if current:
                runs.append(tuple(current))
                current = []
            continue
        current.append((t, v))
        last = v
    if current:
        runs.append(tuple(current))
    return _Line(str(name), tuple(runs), last)
