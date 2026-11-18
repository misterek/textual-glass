"""Panel widgets: the frame around a panel (``PanelView``) and one body per kind.

A panel is a :class:`~textual_glass.GlassPanel` titled with the panel's title, its body a kind widget, and a faint
footer in the bottom border (``<added_by> · <unit> · 1h20m left``). Every kind shows the same states:

* **loading**: before the first run, a faint ``loading…``;
* **error**: the last good data stays, dimmed, with the error on the bottom line in red, and the border turns
  red (with no good data yet, the error takes the body);
* **empty**: the frame's ``note``, or ``nothing``.

``set_frame(frame, error=None)`` redraws only what changed (equal data, NaN included, costs nothing: rule 9) and
``tick(at)`` updates the footer's time left, at minute resolution. Nothing here runs commands or reads files: the
page hands each panel the frames it gets.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import ClassVar

from textual.app import ComposeResult
from textual.color import Color
from textual.containers import Vertical
from textual.markup import escape
from textual.strip import Strip
from textual.widget import Widget

from textual_glass import THEME, PixelContext, PixelWidget, RingGauge, StatValue, Telemetry, ThemeColours
from textual_glass.items import HeaderBar, Meter
from textual_glass.palette import Palette, border_pair, hex_of, mix
from textual_glass.render import Colour, Item
from textual_glass.widgets import (
    DIM,
    FAINT,
    SOFT,
    GlassPanel,
    LineChart,
    Part,
    ellipsize,
    series_colours,
    short_number,
    strip,
    text_meter,
    tone,
)

from ..theme import PALETTE
from .model import Frame, Kind, PanelSpec, Series, Table, fmt_duration

__all__ = [
    "BODIES", "DIM_DATA", "BarsBody", "Body", "ChartBody", "GaugeBody", "PanelErrorLine", "PanelInfo", "PanelMessage",
    "PanelView", "StatBody", "StatRange", "TableBody", "TextBody", "cut_middle", "dimmed", "fit_columns", "fmt_value",
    "footer_text", "frame_lines", "same_frame", "spark_scale", "status_colour",
]

#: How far the last good data of a failing panel fades towards the background.
DIM_DATA = 0.55

# ================================================================================================ helpers ==


def fmt_value(value: float | None, unit: str = "") -> str:
    """A reading for a stat or a gauge: ``42``, ``4.2``, ``99.5%``, ``1,234 req/s``, ``3.4M``."""
    if value is None or not math.isfinite(value):
        return "–"
    a = abs(value)
    if a >= 1e6:
        text = short_number(value)
    elif a >= 100:
        text = f"{value:,.0f}"
    elif a >= 10:
        text = f"{value:.1f}".rstrip("0").rstrip(".")
    elif a == 0:
        text = "0"
    elif a >= 0.001:
        text = f"{value:.3g}"
    else:
        text = f"{value:.2g}"
    if not unit:
        return text
    return f"{text}{unit}" if unit in ("%", "°", "x", "×") else f"{text} {unit}"


def same_frame(a: Frame | None, b: Frame | None) -> bool:
    """Frames with the same data. NaN equals NaN here (a fresh parse makes new NaN objects, which ``==`` on
    tuples would call different, and an idle panel would redraw on every run)."""
    if a is b:
        return True
    if a is None or b is None:
        return False
    if (a.table, a.text, a.note) != (b.table, b.text, b.note) or len(a.series) != len(b.series):
        return False
    for sa, sb in zip(a.series, b.series, strict=True):
        if sa.name != sb.name or len(sa.points) != len(sb.points):
            return False
        for (ta, va), (tb, vb) in zip(sa.points, sb.points, strict=True):
            if ta != tb or (va != vb and not (math.isnan(va) and math.isnan(vb))):
                return False
    return True


def footer_text(spec: PanelSpec, at: datetime | None) -> str:
    """``<added_by> · <unit> · 1h20m left``: only the parts that exist, at minute resolution."""
    parts = [p for p in (spec.added_by, spec.unit) if p]
    if spec.expires_at is not None and at is not None:
        left = (spec.expires_at - at).total_seconds()
        if left <= 0:
            parts.append("expired")
        elif left < 60:
            parts.append("<1m left")
        else:
            parts.append(f"{fmt_duration(left // 60 * 60)} left")
    return " · ".join(parts)


_GOOD = frozenset({"running", "ready", "ok", "healthy", "active", "succeeded", "success", "passed", "completed",
                   "complete", "up", "true", "bound", "available", "green", "done", "online", "pass"})
_WAIT = frozenset({"pending", "waiting", "containercreating", "podinitializing", "terminating", "progressing",
                   "unknown", "starting", "queued", "scheduled", "degraded", "warning", "warn", "amber",
                   "yellow", "notready", "paused", "updating", "restarting"})
_BAD = frozenset({"failed", "error", "crashloopbackoff", "imagepullbackoff", "errimagepull", "oomkilled", "evicted",
                  "down", "false", "red", "critical", "fail", "failure", "dead", "unhealthy", "offline", "lost",
                  "createcontainerconfigerror", "invalidimagename", "backoff"})


def status_colour(value: str) -> str | None:
    """The colour of a status-like word (``Running`` green, ``Pending`` amber, ``Failed`` red), or None."""
    word = value.strip().lower().replace(" ", "").replace("_", "")
    if not word or len(word) > 32:
        return None
    if word in _BAD or word.endswith(("error", "backoff")) or word.startswith(("init:error", "init:crash")):
        return PALETTE["red"]
    if word in _WAIT or word.startswith("init:"):
        return PALETTE["amber"]
    if word in _GOOD:
        return PALETTE["green"]
    return None


def cut_middle(text: str, width: int) -> str:
    """``text`` cut in the middle to ``width`` characters (``checkout-api-7f9…x2kq``): both ends stay readable."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    head = (width - 1 + 1) // 2
    tail = width - 1 - head
    return text[:head] + "…" + (text[-tail:] if tail else "")


def fit_columns(table: Table, width: int, gap: int = 2) -> list[int]:
    """Widths for the columns that fit in ``width``: columns are dropped from the right until each fits at its
    least width, then the room left goes to the columns that need the least to show in full (so the long ones,
    names and ids, are the ones cut). A column of status words is never cut, and the first column, which names
    the row, keeps at least a third of the width."""
    natural = [max([len(name), *(len(row[i]) for row in table.rows if i < len(row))]) for i, name in
               enumerate(table.columns)]
    least = []
    for i, (n, name) in enumerate(zip(natural, table.columns, strict=True)):
        words = [row[i] for row in table.rows if i < len(row) and row[i].strip()]
        status = bool(words) and sum(status_colour(w) is not None for w in words) * 2 >= len(words)
        least.append(n if status else min(n, max(4, min(len(name), 10))))
    if least:  # the first column names the row: it keeps a third of the width before others are dropped
        least[0] = min(natural[0], max(least[0], 12, width // 3))
    count = len(natural)
    while count > 1 and sum(least[:count]) + gap * (count - 1) > width:
        count -= 1
    if count == 0:
        return []
    widths = least[:count]
    room = width - sum(widths) - gap * (count - 1)
    for i in sorted(range(count), key=lambda i: (natural[i] - widths[i], i)):
        extra = min(room, natural[i] - widths[i])
        widths[i] += extra
        room -= extra
    if count == 1:
        widths[0] = min(widths[0], max(1, width))
    return widths


def spark_scale(lo: float, hi: float) -> tuple[float, float]:
    """A sparkline's floor and height for values from ``lo`` to ``hi``: the band they move in, at least a fifth
    of their size (a change of 1% stays a small change), from 0 when that is close."""
    size = max(abs(lo), abs(hi))
    span = max(hi - lo, 0.2 * size) or 1.0
    mid = (lo + hi) / 2
    floor = mid - span * 0.65
    if lo >= 0:
        floor = max(0.0, floor)
    return floor, mid + span * 0.6 - floor


def dimmed(colours: Sequence[Colour], palette: Palette, amount: float = DIM_DATA) -> list[str]:
    """Colours faded towards the theme's background (the last good data of a failing panel)."""
    return [hex_of(mix(Color.parse(c).rgb if isinstance(c, str) else c, palette.background, amount)) for c in colours]


def _fade(colour: Colour, palette: Palette, amount: float = DIM_DATA) -> str:
    return dimmed([colour], palette, amount)[0]


def _first(frame: Frame) -> Series | None:
    return frame.series[0] if frame.series else None


def _newest(frame: Frame) -> float:
    """The newest point's time, or the run's: where a chart's "now" is (data-derived, so equal data is equal)."""
    times = [s.points[-1][0] for s in frame.series if s.points]
    return max(times) if times else frame.at


# ================================================================================================= bodies ==


class Body:
    """What every kind's body does: show a frame (with the panel's spec), dimmed while the panel fails."""

    #: The frame shapes this body can draw; others show as a message.
    SHAPES: ClassVar[tuple[str, ...]] = ("series",)

    def show(self, frame: Frame, spec: PanelSpec, dim: bool, palette: Palette) -> None:
        """Draw ``frame``; ``palette`` is the theme's (the body may not be mounted yet)."""
        raise NotImplementedError

    def title_parts(self, frame: Frame, spec: PanelSpec, dim: bool, palette: Palette) -> list[Part]:
        """Extras for the panel's title (a single line's newest value)."""
        return []


class ChartBody(LineChart, Body):
    """``timeseries``: every series as a line."""

    def show(self, frame: Frame, spec: PanelSpec, dim: bool, palette: Palette) -> None:
        end = _newest(frame)
        colours: Sequence[Colour] | ThemeColours = THEME
        if dim:
            colours = dimmed(series_colours(palette), palette, DIM_DATA)
        self.set_series([(s.name or spec.title, s.points) for s in frame.series], start=end - spec.window, end=end,
                        unit=spec.unit, ymin=spec.ymin, ymax=spec.ymax, colors=colours)

    def title_parts(self, frame: Frame, spec: PanelSpec, dim: bool, palette: Palette) -> list[Part]:
        if len(frame.series) != 1:
            return []  # several lines: the legend has their values
        colour = series_colours(palette, 1)[0]
        shade = _fade(colour, palette) if dim else hex_of(colour)
        return [(fmt_value(frame.series[0].last, spec.unit), f"bold {shade}")]


class StatRange(PixelWidget):
    """Under a stat's value: the lowest and highest of its history, and how far back that goes."""

    DEFAULT_CSS = """
    StatRange { height: 1; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.text = ""

    def set_range(self, text: str) -> None:
        if text != self.text:
            self.text = text
            self.refresh()

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        return strip([(ellipsize(self.text, width), tone(self.theme_palette, FAINT))] if y == 0 else [], width)


class StatBody(Vertical, Body):
    """``stat``: the newest value of the first series, big, and a small area sparkline of its history, with its
    lowest and highest values."""

    DEFAULT_CSS = """
    StatBody { height: 1fr; }
    StatBody > StatValue { height: 1; }
    StatBody > StatRange { height: 1; }
    StatBody > Telemetry { height: 1fr; min-height: 1; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.value = StatValue()
        self.range_line = StatRange()
        self.spark = Telemetry(mode="area")

    def compose(self) -> ComposeResult:
        yield self.value
        yield self.range_line
        yield self.spark

    def show(self, frame: Frame, spec: PanelSpec, dim: bool, palette: Palette) -> None:
        pal = palette
        first = _first(frame)
        value = first.last if first else None
        colour = _fade(pal.primary, pal) if dim else hex_of(pal.primary)
        self.value.set(fmt_value(value, spec.unit), color=colour)
        history = [v for _, v in first.points if math.isfinite(v)] if first else []
        chart = self.spark
        if len(history) < 2:
            if chart.display:
                chart.display = False
            self.range_line.set_range("first reading: the history builds as it runs" if history else "")
            return
        lo, hi = min(history), max(history)
        back = first.points[-1][0] - first.points[0][0] if first else 0.0
        self.range_line.set_range(
            f"min {fmt_value(lo)} · max {fmt_value(hi)}" + (f" · {fmt_duration(back)}" if back >= 60 else ""))
        floor, top = spark_scale(lo, hi)
        shifted = [v - floor for v in history]
        pair = border_pair(pal.primary)
        tones: tuple[Colour, Colour] = (_fade(pair[0], pal), _fade(pair[1], pal)) if dim else pair
        chart.set_series(shifted, colors=tones, scale_max=top)
        if not chart.display:
            chart.display = True


class GaugeBody(RingGauge, Body):
    """``gauge``: the newest value over ``ymax`` (default 100) as a ring, the value written inside."""

    def show(self, frame: Frame, spec: PanelSpec, dim: bool, palette: Palette) -> None:
        first = _first(frame)
        value = first.last if first else None
        low = spec.ymin if spec.ymin is not None else 0.0
        high = spec.ymax if spec.ymax is not None else 100.0
        fraction = 0.0 if value is None or high <= low else min(1.0, max(0.0, (value - low) / (high - low)))
        pal = palette
        pair = border_pair(pal.primary)
        colours: tuple[Colour, Colour] | ThemeColours = (_fade(pair[0], pal), _fade(pair[1], pal)) if dim else THEME
        percent = spec.unit == "%" and (low, high) == (0.0, 100.0)  # the value says it already
        label = "" if percent else f"of {fmt_value(high, spec.unit)}"
        if label != self.label:
            self.label = label
            self.refresh()
        self.set(fraction, fmt_value(value, spec.unit), colors=colours)


class BarsBody(PixelWidget, Body):
    """``bars``: one row per series, biggest first: its name, a glowing meter and its value."""

    DEFAULT_CSS = """
    BarsBody { height: 1fr; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.rows: tuple[tuple[str, float], ...] = ()
        self.unit = ""
        self.top = 1.0
        self.dim = False

    def show(self, frame: Frame, spec: PanelSpec, dim: bool, palette: Palette) -> None:
        rows = [(s.name or spec.title, s.last) for s in frame.series]
        ordered = tuple(sorted(((n, v) for n, v in rows if v is not None), key=lambda r: -r[1]))
        ordered += tuple((n, math.nan) for n, v in rows if v is None)
        values = [v for _, v in ordered if math.isfinite(v)]
        top = spec.ymax if spec.ymax is not None else max([*values, 0.0]) or 1.0
        key = (tuple((n, None if math.isnan(v) else v) for n, v in ordered), spec.unit, top, dim)
        if key != (tuple((n, None if math.isnan(v) else v) for n, v in self.rows), self.unit, self.top, self.dim):
            self.rows, self.unit, self.top, self.dim = ordered, spec.unit, top, dim
            self.refresh()

    def _layout(self, width: int) -> tuple[int, int, int]:
        """Name, meter and value widths."""
        names = max((len(n) for n, _ in self.rows), default=0)
        values = max((len(fmt_value(None if math.isnan(v) else v, self.unit)) for _, v in self.rows), default=1)
        name_w = min(names, max(6, width // 3))
        meter_w = width - name_w - values - 2
        if meter_w < 3:
            meter_w = 0
            name_w = max(0, width - values - 1)
        return name_w, meter_w, values

    def _visible(self, height: int) -> int:
        return len(self.rows) if len(self.rows) <= height else max(0, height - 1)

    def _pitch(self, height: int) -> int:
        """Rows per bar: two (a bar and a gap) when they all fit that way, else one."""
        return 2 if self.rows and 2 * len(self.rows) - 1 <= height else 1

    def _colours(self, palette: Palette) -> tuple[Colour, Colour]:
        pair = border_pair(palette.primary)
        return (_fade(pair[0], palette), _fade(pair[1], palette)) if self.dim else pair

    def render_line(self, y: int) -> Strip:
        width, height = self.size
        pal = self.theme_palette
        shown = self._visible(height)
        pitch = self._pitch(height)
        if y % pitch:
            return Strip.blank(width)
        k = y // pitch
        if k >= shown:
            if k == shown and shown < len(self.rows):
                return strip([(f"+{len(self.rows) - shown} more", tone(pal, FAINT))], width)
            return Strip.blank(width)
        name, value = self.rows[k]
        name_w, meter_w, value_w = self._layout(width)
        c1, _ = self._colours(pal)
        text = tone(pal, DIM if self.dim else SOFT)
        parts: list[Part] = [(ellipsize(name, name_w).ljust(name_w), text), (" ", "")]
        if meter_w:
            fraction = 0.0 if math.isnan(value) else value / self.top
            meter = " " * meter_w if self.glass_on else text_meter(fraction, meter_w)
            parts += [(meter, hex_of(c1) if not isinstance(c1, str) else c1), (" ", "")]
        number = fmt_value(None if math.isnan(value) else value, self.unit)
        parts.append((number.rjust(value_w), f"bold {tone(pal, DIM) if self.dim else tone(pal, -0.2)}"))
        return strip(parts, width)

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        r = self.content_region
        name_w, meter_w, _ = self._layout(r.width)
        if not meter_w:
            return ()
        c1, c2 = self._colours(ctx.palette)
        clip = self.visible_clip()
        out: list[Item] = []
        pitch = self._pitch(r.height)
        for k in range(self._visible(r.height)):
            name, value = self.rows[k]
            fraction = 0.0 if math.isnan(value) else max(0.0, min(1.0, value / self.top))
            out.append(Meter((float(r.x + name_w + 1), float(r.y + k * pitch), float(meter_w), 1.0), fraction, c1, c2,
                             ident=f"bar:{id(self)}:{name}", clip=clip))
        return out


class TableBody(PixelWidget, Body):
    """``table``: the rows under their header, columns fitted to the width, status words coloured."""

    SHAPES = ("table",)
    DEFAULT_CSS = """
    TableBody { height: 1fr; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.table: Table | None = None
        self.dim = False

    def show(self, frame: Frame, spec: PanelSpec, dim: bool, palette: Palette) -> None:
        if (frame.table, dim) != (self.table, self.dim):
            self.table, self.dim = frame.table, dim
            self.refresh()

    def _shown(self, height: int) -> int:
        """Data rows that fit (the header takes one line, "+N more" another when they don't all fit)."""
        rows = len(self.table.rows) if self.table else 0
        room = max(0, height - 1)
        return rows if rows <= room else max(0, room - 1)

    def render_line(self, y: int) -> Strip:
        width, height = self.size
        pal = self.theme_palette
        table = self.table
        if table is None or not table.columns:
            return Strip.blank(width)
        widths = fit_columns(table, width)
        shown = self._shown(height)
        if y == 0:
            parts: list[Part] = []
            head = f"bold {tone(pal, DIM if self.dim else SOFT)}"
            for i, w in enumerate(widths):
                parts += [(cut_middle(table.columns[i], w).ljust(w), head), ("  ", "")]
            return strip(parts, width)
        k = y - 1
        if k >= shown:
            if k == shown and shown < len(table.rows):
                return strip([(f"+{len(table.rows) - shown} more", tone(pal, FAINT))], width)
            return Strip.blank(width)
        row = table.rows[k]
        parts = []
        plain = tone(pal, DIM) if self.dim else tone(pal, 0.05)
        for i, w in enumerate(widths):
            value = row[i] if i < len(row) else ""
            colour = status_colour(value)
            if colour and self.dim:
                colour = _fade(colour, pal)
            parts += [(cut_middle(value, w).ljust(w), colour or plain), ("  ", "")]
        return strip(parts, width)

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        r = self.content_region
        if self.table is None or not self.table.columns or r.width < 4:
            return ()
        pal = ctx.palette
        c1, c2 = border_pair(pal.primary)
        if self.dim:
            c1, c2 = mix(c1, pal.background, DIM_DATA), mix(c2, pal.background, DIM_DATA)
        return (HeaderBar((float(r.x), float(r.y), float(r.width), 1.0), c1, c2, ident=f"thead:{id(self)}",
                          clip=self.visible_clip()),)


class TextBody(PixelWidget, Body):
    """``text``: the last lines that fit."""

    SHAPES = ("text", "table", "series")
    DEFAULT_CSS = """
    TextBody { height: 1fr; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.lines: tuple[str, ...] = ()
        self.dim = False

    def show(self, frame: Frame, spec: PanelSpec, dim: bool, palette: Palette) -> None:
        lines = tuple(frame_lines(frame))
        if (lines, dim) != (self.lines, self.dim):
            self.lines, self.dim = lines, dim
            self.refresh()

    def render_line(self, y: int) -> Strip:
        width, height = self.size
        pal = self.theme_palette
        shown = self.lines[-height:] if height else ()
        if y >= len(shown):
            return Strip.blank(width)
        return strip([(ellipsize(shown[y], width), tone(pal, DIM) if self.dim else tone(pal, 0.08))], width)


def frame_lines(frame: Frame) -> list[str]:
    """Any frame as lines of text (the ``text`` kind shows tables and numbers this way too)."""
    if frame.text is not None:
        return [line.expandtabs(4).rstrip() for line in frame.text.rstrip("\n").split("\n")]
    if frame.table is not None:
        widths = [max([len(c), *(len(r[i]) for r in frame.table.rows if i < len(r))])
                  for i, c in enumerate(frame.table.columns)]
        rows = [frame.table.columns, *frame.table.rows]
        return ["  ".join(v.ljust(w) for v, w in zip(row, widths, strict=False)).rstrip() for row in rows]
    return [f"{s.name or 'value'}  {fmt_value(s.last)}" for s in frame.series]


#: The body for each kind.
BODIES: dict[str, type[Widget]] = {
    "timeseries": ChartBody, "stat": StatBody, "gauge": GaugeBody, "bars": BarsBody, "table": TableBody,
    "text": TextBody,
}


# ================================================================================================= states ==


class PanelMessage(PixelWidget):
    """The body's place when there is nothing to draw: ``loading…``, the empty note, or an error, centred."""

    DEFAULT_CSS = """
    PanelMessage { height: 1fr; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.text = ""
        self.kind = "faint"  # "faint" or "error"

    def set_message(self, text: str, kind: str = "faint") -> None:
        if (text, kind) != (self.text, self.kind):
            self.text, self.kind = text, kind
            self.refresh()

    def wrapped(self, width: int, height: int) -> list[str]:
        words = self.text.split()
        lines: list[str] = []
        line = ""
        for word in words:
            if line and len(line) + 1 + len(word) > width:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        if line:
            lines.append(line)
        lines = [ellipsize(t, width) for t in lines]
        if len(lines) > height:
            lines = lines[: max(1, height)]
            lines[-1] = ellipsize(lines[-1] + " …", width) if height else ""
        return lines

    def render_line(self, y: int) -> Strip:
        width, height = self.size
        lines = self.wrapped(max(1, width - 2), height)
        top = max(0, (height - len(lines)) // 2)
        k = y - top
        if not 0 <= k < len(lines):
            return Strip.blank(width)
        style = f"{PALETTE['red']}" if self.kind == "error" else tone(self.theme_palette, FAINT)
        return strip([(lines[k].center(width), style)], width)


class PanelErrorLine(PixelWidget):
    """The bottom line of a failing panel that still shows its last good data: ``✕ <error>`` in red."""

    DEFAULT_CSS = """
    PanelErrorLine { height: 1; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.text = ""

    def set_error(self, text: str) -> None:
        if text != self.text:
            self.text = text
            self.refresh()

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if y:
            return Strip.blank(width)
        return strip([("✕ ", f"bold {PALETTE['red']}"), (ellipsize(self.text, width - 2), PALETTE["red"])], width)


class PanelInfo(PixelWidget):
    """The full-screen view's lines under the body: the command (or the inline data), and when it last ran."""

    DEFAULT_CSS = """
    PanelInfo { height: 2; }
    """

    def __init__(self, spec: PanelSpec, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.spec = spec
        self.last_run: datetime | None = None
        self.running = False
        self.at: datetime | None = None

    def set_run(self, last_run: datetime | None, running: bool) -> None:
        if (last_run, running) != (self.last_run, self.running):
            self.last_run, self.running = last_run, running
            self.refresh()

    def set_spec(self, spec: PanelSpec) -> None:
        if spec != self.spec:
            self.spec = spec
            self.refresh()

    def tick(self, at: datetime) -> None:
        if footer_text(self.spec, at) != footer_text(self.spec, self.at):
            self.at = at
            self.refresh()
        self.at = at

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        pal = self.theme_palette
        spec = self.spec
        if y == 0:
            if spec.cmd is not None:
                return strip([("$ ", f"bold {hex_of(pal.primary)}"),
                              (cut_middle(" ".join(spec.cmd.split()), width - 2), tone(pal, SOFT))], width)
            return strip([("inline data", tone(pal, DIM))], width)
        if y == 1:
            when = self.last_run.astimezone().strftime("%H:%M") if self.last_run else "not yet"
            parts = [f"last run {when}" + (" · running" if self.running else "")]
            if spec.cmd is not None:
                parts.append(f"every {fmt_duration(spec.every)}")
            if spec.kind == "timeseries":
                parts.append(f"window {fmt_duration(spec.window)}")
            footer = footer_text(spec, self.at or datetime.now(timezone.utc))
            if footer:
                parts.append(footer)
            return strip([(ellipsize(" · ".join(parts), width), tone(pal, DIM))], width)
        return Strip.blank(width)


# ================================================================================================== panel ==


class PanelView(GlassPanel, can_focus=True):
    """One panel: a glass panel titled with the panel's title, its kind's body, and a faint footer.

    Args:
        spec: The panel.
        at: Now, for the footer's time left (default: the clock); later through :meth:`tick`.
        big: The full-screen version: the command and when it last ran under the body, and no footer.
    """

    DEFAULT_CSS = """
    PanelView { height: 1fr; width: 1fr; padding: 0 1; border-subtitle-color: $foreground 45%;
                border-subtitle-style: none; }
    PanelView > PanelMessage { height: 1fr; }
    PanelView > .panel-body { height: 1fr; }
    """

    def __init__(self, spec: PanelSpec, *, at: datetime | None = None, big: bool = False, id: str | None = None,
                 classes: str | None = None, dialog: bool = False) -> None:
        super().__init__(title=spec.title, id=id, classes=classes, dialog=dialog)
        self.spec = spec
        self.big = big
        self.frame: Frame | None = None
        self.error: str | None = None
        self.at = at or datetime.now(timezone.utc)
        self.body = BODIES[spec.kind](id=None)
        self.body.add_class("panel-body")
        self.message = PanelMessage()
        self.error_line = PanelErrorLine()
        self.info = PanelInfo(spec) if big else None
        self._title_key: tuple[object, ...] | None = None

    @property
    def kind(self) -> Kind:
        return self.spec.kind

    def compose(self) -> ComposeResult:
        yield self.body
        yield self.message
        yield self.error_line
        if self.info is not None:
            yield self.info

    def on_mount(self) -> None:
        super().on_mount()
        self._apply()
        self._footer()

    # -- updates ----------------------------------------------------------------------------------------------

    def set_spec(self, spec: PanelSpec) -> None:
        """The same panel with new fields (a replace that kept the kind): title, unit, scale, footer."""
        if spec == self.spec:
            return
        if spec.kind != self.spec.kind:
            raise ValueError("a panel's kind can't change in place: make a new PanelView")
        self.spec = spec
        if self.info is not None:
            self.info.set_spec(spec)
        if self.is_attached:
            self._apply()
            self._footer()

    def set_frame(self, frame: Frame | None, error: str | None = None) -> None:
        """The panel's latest good data (None before the first good run) and the last run's error, if it failed."""
        if same_frame(frame, self.frame) and error == self.error:
            return
        self.frame, self.error = frame, error
        if self.is_attached:
            self._apply()

    def set_run(self, last_run: datetime | None, running: bool) -> None:
        """When the panel last ran (the full-screen view says so)."""
        if self.info is not None:
            self.info.set_run(last_run, running)

    def tick(self, at: datetime) -> None:
        """A minute passed: the footer's time left (only its text changes, and only when the minute does)."""
        self.at = at
        if self.is_attached:
            self._footer()
        if self.info is not None:
            self.info.tick(at)

    # -- drawing ----------------------------------------------------------------------------------------------

    def _footer(self) -> None:
        text = "" if self.big else footer_text(self.spec, self.at)
        markup = escape(text) if text else ""
        if markup != (self.border_subtitle or ""):
            self.border_subtitle = markup

    def _palette(self) -> Palette:
        return self.message.theme_palette

    def _apply(self) -> None:
        frame, error, spec = self.frame, self.error, self.spec
        body = self.body
        shapes = body.SHAPES if isinstance(body, Body) else ("series",)
        drawable = frame is not None and frame.shape in shapes
        dim = error is not None
        if drawable and frame is not None and isinstance(body, Body):
            body.show(frame, spec, dim, self._palette())
            message = None
        elif error is not None:
            message = (error, "error")
        elif frame is None:
            message = ("loading…", "faint")
        elif frame.shape == "empty":
            message = (frame.note or "nothing", "faint")
        else:
            message = (f"no {'numbers' if 'series' in shapes else spec.kind} in the output ({frame.shape})", "faint")
        self._show(body, message is None)
        self._show(self.message, message is not None)
        if message is not None:
            self.message.set_message(*message)
        self._show(self.error_line, error is not None and message is None)
        if error is not None:
            self.error_line.set_error(error)
        self._title(drawable, dim)
        if error is not None:
            self.set_accent(PALETTE["red"], PALETTE["orange"])
        else:
            self.set_accent(None)

    @staticmethod
    def _show(widget: Widget, visible: bool) -> None:
        if widget.display != visible:
            widget.display = visible

    def _title(self, drawable: bool, dim: bool) -> None:
        extras: list[Part] = []
        if drawable and self.frame is not None and isinstance(self.body, Body):
            extras = self.body.title_parts(self.frame, self.spec, dim, self._palette())
        if self.error is not None:
            extras.append(("✕", f"bold {PALETTE['red']}"))
        key = (self.spec.title, tuple(extras))
        if key != self._title_key:
            self._title_key = key
            self.set_title(self.spec.title, *extras)
