"""The showcase's two views.

* ``StockView``: nothing but stock Textual widgets (DataTables, an OptionList,
  ProgressBars, Markdown, a RichLog). With glass they get pixel chrome for
  free; the code doesn't know glass exists.
* ``PixelView``: the same world on textual-glass's own widgets (``GlassPanel``,
  ``SelectList``, ``RingGauge``, ``Telemetry``, ``StatTile``, ``ProgressArc``,
  ``SpriteView``, ``InfoCard``), which add pixels under their text and fall
  back to block characters and glyphs without glass.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.widgets import DataTable, Label, Markdown, OptionList, ProgressBar, RichLog
from textual.widgets.option_list import Option
from world import (
    ACTIVITY,
    DAYS,
    DEPLOYS,
    DEPLOYS_LAST_WEEK,
    DEPLOYS_WEEK,
    ERRORS_HOURLY,
    INCIDENTS,
    RELEASE,
    REQUESTS,
    RUNBOOK,
    STATES,
    TODAY,
    Service,
    Tones,
    World,
)

from textual_glass.items import Meter, Signal
from textual_glass.render import FRect, Item
from textual_glass.scene import PixelContext
from textual_glass.sprites import beacon_frames
from textual_glass.widgets import (
    GlassPanel,
    InfoCard,
    Part,
    ProgressArc,
    RingGauge,
    SelectList,
    SpriteView,
    StatTile,
    StatValue,
    Telemetry,
    ellipsize,
    text_meter,
    tone,
)

WIDE = 70  # a list or table this wide shows the extra columns


def fmt_ms(ms: int) -> str:
    if ms <= 0:
        return "–"
    return f"{ms / 1000:.2f} s" if ms >= 1000 else f"{ms} ms"


def fmt_rps(rps: int) -> str:
    return f"{rps / 1000:.1f}k" if rps >= 1000 else str(rps)


def beacon(state: str, tones: Tones) -> tuple[tuple[str, str, str], Any]:
    """The sprite for a service state: its key and its maker."""
    animation, colour = STATES[state][1], tones.state(state)
    return ("beacon", animation, colour), lambda w, h: beacon_frames(w, h, animation, colour)


class View(Vertical):
    """A page of the showcase: redraws itself from the world and the theme."""

    def show(self, world: World, tones: Tones) -> None:
        raise NotImplementedError

    def focus_default(self) -> None:
        raise NotImplementedError

    def current(self) -> str | None:
        """The service under the cursor, if any."""
        return None


# ============================================================ stock widgets ==


class StockView(View):
    """Stock Textual widgets only."""

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="stock-left"):
                yield DataTable(id="services", cursor_type="row", zebra_stripes=True)
                yield DataTable(id="incidents", cursor_type="row")
                yield RichLog(id="activity", min_width=20, wrap=False)
            with Vertical(id="stock-right"):
                yield OptionList(id="deploys")
                with Vertical(id="rollout"):
                    for key in ("ledger", "search", "checkout"):
                        yield Label(id=f"rollout-{key}")
                        yield ProgressBar(total=100, show_eta=False, id=f"bar-{key}")
                with VerticalScroll(id="runbook"):
                    yield Markdown(RUNBOOK)

    def on_mount(self) -> None:
        self.query_one("#services").border_title = "Services"
        self.query_one("#incidents").border_title = "Incidents"
        self.query_one("#activity").border_title = "Activity"
        self.query_one("#deploys").border_title = "Deploy queue"
        self.query_one("#rollout").border_title = "Rollouts"
        self.query_one("#runbook").border_title = "Runbook"
        self._wide: bool | None = None
        self._layout: tuple[int, int] = (0, 0)
        self._world: World | None = None
        self._tones: Tones | None = None

    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self._relayout)

    def _relayout(self) -> None:
        """Columns depend on the tables' widths: rebuild them when those change."""
        if self._world is not None and self._tones is not None and self._layout_key() != self._layout:
            self.show(self._world, self._tones)

    def _layout_key(self) -> tuple[int, int]:
        return self.query_one("#services").content_size.width, self.query_one("#incidents").content_size.width

    def _is_wide(self) -> bool:
        return self.query_one("#services").content_size.width >= WIDE

    def focus_default(self) -> None:
        self.query_one("#services", DataTable).focus()

    def current(self) -> str | None:
        table = self.screen.focused
        if not isinstance(table, DataTable) or not table.row_count or table.id not in ("services", "incidents"):
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if table.id == "incidents":
            return next((i.service for i in INCIDENTS if i.key == key), None)
        return str(key)

    def show(self, world: World, tones: Tones) -> None:
        self._world, self._tones = world, tones
        wide = self._is_wide()
        layout = self._layout_key()
        rebuild = wide != self._wide or layout != self._layout
        self._wide, self._layout = wide, layout
        self._services(world, tones, rebuild)
        self._incidents(tones, rebuild)
        self._deploys(world, tones)
        self._rollouts(world, tones)
        log = self.query_one(RichLog)
        log.clear()
        for when, kind, source, message in ACTIVITY:
            log.write(Text.assemble((when, tones.faint), "  ", (f"{source:<8}", tones.text(kind)), message))

    def _services(self, world: World, tones: Tones, rebuild: bool) -> None:
        table = self.query_one("#services", DataTable)
        current = table.coordinate_to_cell_key(table.cursor_coordinate).row_key if table.row_count else None
        table.clear(columns=rebuild)
        wide = bool(self._wide)
        if rebuild:
            table.add_column("Service", key="service")
            table.add_column("State", key="state")
            if wide:
                table.add_column("Version", key="version")
            table.add_column("p95", key="p95")
            table.add_column("Errors", key="errors")
            if wide:
                table.add_column("Load", key="load")
                table.add_column("Req/s", key="rps")
            table.add_column("Host", key="host")
        for s in world.services:
            colour = tones.state_text(s.state)
            cells: list[Any] = [Text(s.key, style="bold"), Text(f"● {s.label}", style=colour)]
            if wide:
                cells.append(Text(s.version, style=tones.muted))
            cells.append(Text(fmt_ms(s.p95).rjust(7), style=colour if s.p95 >= 400 else ""))
            cells.append(Text(f"{s.errors:.1f}%".rjust(6), style=tones.text_error if s.errors >= 1 else tones.muted))
            if wide:
                cells.append(Text(text_meter(s.cpu, 6), style=tones.load(s.cpu)[1] if s.cpu >= 0.75 else tones.primary))
                cells.append(Text(fmt_rps(s.rps).rjust(5), style=tones.muted))
            cells.append(Text(s.host, style=tones.muted))
            table.add_row(*cells, key=s.key)
        if current is not None:  # the cursor stays on its service when the order changes
            table.move_cursor(row=table.get_row_index(current), animate=False)
        worst = world.services[0]
        table.border_subtitle = f"{len(world.services)} services · {worst.key} {worst.label}"

    def _incidents(self, tones: Tones, rebuild: bool) -> None:
        table = self.query_one("#incidents", DataTable)
        row = table.cursor_row
        wide = bool(self._wide)
        fixed = 8 + 11 + 5 + (9 + 8 if wide else 0)  # every column but the title, with cell padding
        title_width = max(12, table.content_size.width - fixed - 3)
        table.clear(columns=rebuild)
        if rebuild:
            table.add_column("Sev", key="sev")
            if wide:
                table.add_column("ID", key="id")
            table.add_column("Service", key="service")
            table.add_column("Title", key="title", width=title_width)
            if wide:
                table.add_column("Owner", key="owner")
            table.add_column("Age", key="age")
        for i in INCIDENTS:
            ink = "#1d1630" if i.severity == 3 else "#ffffff"  # dark text on the light badge
            cells: list[Any] = [Text(f" SEV{i.severity} ", style=f"bold {ink} on {tones.severity(i.severity)}")]
            if wide:
                cells.append(Text(i.key, style=tones.muted))
            cells += [Text(i.service, style="bold"), Text(ellipsize(i.title, title_width))]
            if wide:
                cells.append(Text(i.owner, style=tones.muted))
            cells.append(Text(i.age.rjust(3), style=tones.muted))
            table.add_row(*cells, key=i.key)
        table.move_cursor(row=row, animate=False)
        table.border_title = f"Incidents ({len(INCIDENTS)})"

    def _deploys(self, world: World, tones: Tones) -> None:
        options = self.query_one("#deploys", OptionList)
        highlighted = options.highlighted
        marks = {"waiting": ("◆", tones.waiting), "rolling": ("▶", tones.text_primary),
                 "building": ("◌", tones.muted), "scheduled": ("◷", tones.muted), "done": ("✓", tones.text_success)}
        options.clear_options()
        for d in DEPLOYS:
            status, detail = d.status, d.detail
            if (d.service, d.version) == RELEASE and world.release is not None:
                status, detail = ("done", "just now") if world.release >= 1 else ("rolling", f"{world.release:.0%}")
            mark, colour = marks[status]
            target = f" → {d.target}" if self._wide else ""
            prompt = Content.assemble((f"{mark} ", colour), (d.service, "bold"), f" {d.version}{target}  ",
                                      (detail, colour if status == "waiting" else tones.muted))
            options.add_option(Option(prompt, id=f"{d.service}@{d.version}"))
        options.highlighted = highlighted if highlighted is not None else 0

    def _rollouts(self, world: World, tones: Tones) -> None:
        release = world.release
        rows = {
            "ledger": ("ledger v0.9.0", f"{round(world.rollout * 5)} of 5 regions", world.rollout),
            "search": ("search v1.9.3", "staging build", 0.35),
            "checkout": ("checkout v2.14.0",
                         "waiting on you" if release is None else ("live" if release >= 1 else "shipping"),
                         release or 0.0),
        }
        for key, (name, status, fraction) in rows.items():
            colour = tones.waiting if status.startswith("waiting") else tones.muted
            self.query_one(f"#rollout-{key}", Label).update(Content.assemble((name, "bold"), "  ", (status, colour)))
            self.query_one(f"#bar-{key}", ProgressBar).update(progress=round(fraction * 100))


# ============================================================ pixel widgets ==


class ServiceList(SelectList[Service]):
    """Services, most urgent first. Per row: a signal meter for how much it
    needs a person, its state, p95, a load meter and its host (more columns
    when wide, and its note on a second line when tall)."""

    tones: Tones | None = None

    def row_key(self, row: Service) -> str:
        return row.key

    @property
    def lines_per_row(self) -> int:
        return 2 if self.size.height >= 16 else 1  # room for each service's note on a second line

    def _layout(self, width: int) -> list[tuple[str, int]]:
        """Columns and their widths; "name" takes what is left."""
        cols = [("signal", 5), ("name", 0), ("state", 10), ("p95", 7), ("load", 6), ("host", 8)]
        if width >= WIDE:
            cols[3:3] = [("version", 8)]
            cols[-1:-1] = [("errors", 6), ("rps", 6)]
        fixed = sum(w for _, w in cols) + len(cols) - 1
        return [(name, w or max(6, width - fixed)) for name, w in cols]

    def _offset(self, width: int, column: str) -> int:
        x = 0
        for name, w in self._layout(width):
            if name == column:
                return x
            x += w + 1
        return -1

    def pill_color(self, row: Service) -> str | None:
        return self.tones.state(row.state) if self.tones else None

    def render_row(self, row: Service, line: int, width: int, selected: bool) -> list[Part]:
        pal = self.theme_palette
        tones = self.tones or Tones({})
        text = f"bold {tone(pal, -0.45)}" if selected else tone(pal)
        muted = tone(pal, 0.42)
        if line:
            return [(" " * 6, ""), (ellipsize(row.note, width - 6), tone(pal, 0.5))]
        state_colour = tones.state_text(row.state)
        cells: dict[str, Part] = {
            "signal": ("     ", "") if self.glass_on else (text_meter(row.attention / 10, 5), state_colour),
            "name": (row.key, text),
            "state": (row.label, state_colour),
            "version": (row.version, muted),
            "p95": (fmt_ms(row.p95).rjust(7), state_colour if row.p95 >= 400 else muted),
            "load": ("      ", "") if self.glass_on else (text_meter(row.cpu, 6), tones.load(row.cpu)[1]),
            "errors": (f"{row.errors:.1f}%".rjust(6), tones.text_error if row.errors >= 1 else muted),
            "rps": (fmt_rps(row.rps).rjust(6), muted),
            "host": (row.host, muted),
        }
        parts: list[Part] = []
        for name, w in self._layout(width):
            value, spec = cells[name]
            parts += [(ellipsize(value, w).ljust(w), spec), (" ", "")]
        return parts[:-1]

    def row_pixels(self, row: Service, rect: FRect, selected: bool, ctx: PixelContext) -> Iterable[Item]:
        x, y, w, _ = rect
        tones = self.tones or Tones({})
        load = self._offset(int(w), "load")
        c1, c2 = tones.load(row.cpu)
        return (
            Signal((x, y, 5.0, 1.0), float(row.attention), tones.state(row.state), ident=f"signal:{row.key}"),
            Meter((x + load, y, 6.0, 1.0), row.cpu, c1, c2, ident=f"load:{row.key}"),
        )

    def empty_text(self) -> str:
        return "no services"


class PixelView(View):
    """textual-glass's own widgets."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="px-top"):
            with GlassPanel(title="CLUSTER", id="cluster"), Horizontal():
                yield RingGauge("CPU", id="ring-cpu")
                yield RingGauge("MEMORY", id="ring-mem")
                yield RingGauge("DISK", id="ring-disk")
                yield RingGauge("NETWORK", id="ring-net", classes="wide-only")
            with GlassPanel(title="RELEASES", id="releases"), Horizontal():
                yield ProgressArc(id="arc-ledger")
                yield ProgressArc(id="arc-checkout")
                yield ProgressArc(id="arc-gateway")
        with Horizontal(id="px-mid"):
            with GlassPanel(title="SERVICES", id="services-panel"):
                yield ServiceList(id="service-list")
            with GlassPanel(title="", id="detail"):
                with Horizontal(id="detail-top"):
                    yield SpriteView(*beacon("paused", Tones({})), id="beacon")  # show() picks the real one
                    yield InfoCard(id="card")
                yield StatValue(versus="vs 12 h ago", id="detail-stat")
                yield Telemetry(mode="area", id="detail-chart")
        with Horizontal(id="px-bottom"):
            yield StatTile("DEPLOYS", versus="vs last week", id="tile-deploys")
            yield StatTile("REQUESTS / MIN", mode="area", versus="vs 12 h ago", id="tile-requests")
            yield StatTile("ERRORS / HOUR", versus="vs last hour", id="tile-errors")

    def on_mount(self) -> None:
        self._world: World | None = None
        self._tones: Tones | None = None

    def focus_default(self) -> None:
        self.query_one(ServiceList).focus()

    def current(self) -> str | None:
        row = self.query_one(ServiceList).highlighted
        return row.key if row is not None else None

    def on_select_list_highlighted(self, message: SelectList.Highlighted) -> None:
        if self._world is not None and self._tones is not None:
            self._detail(message.row, self._tones)

    def show(self, world: World, tones: Tones) -> None:
        self._world, self._tones = world, tones
        services = self.query_one(ServiceList)
        services.tones = tones
        services.set_rows(world.services)  # redraws the rows even when they are the same (new tones)
        counts = world.counts
        worst = world.services[0]
        panel = self.query_one("#services-panel", GlassPanel)
        extras: list[Part] = [(f"{counts.get(state, 0)} {STATES[state][0]}", f"bold {tones.state_text(state)}")
                              for state in ("down", "degraded", "approval") if counts.get(state)]
        panel.set_title(f"SERVICES {len(world.services)}", *extras)
        panel.set_accent(tones.state(worst.state), tones.state(world.services[1].state))
        self._cluster(tones)
        self._releases(world, tones)
        self._tiles(tones)
        row = services.highlighted
        if row is not None:
            self._detail(row, tones)

    def _cluster(self, tones: Tones) -> None:
        for key, fraction, text, sublabel in (("cpu", 0.58, "58%", "of 96 cores"), ("mem", 0.81, "81%", "of 384 GB"),
                                              ("disk", 0.34, "34%", "of 12 TB"), ("net", 0.47, "4.7G", "of 10 Gb/s")):
            self.query_one(f"#ring-{key}", RingGauge).set(fraction, text, tones.load(fraction), sublabel)

    def _releases(self, world: World, tones: Tones) -> None:
        release = world.release
        regions = round(world.rollout * 5)
        self.query_one("#arc-ledger", ProgressArc).set(
            world.rollout, "ledger", f"canary {regions}/5", [("v0.9.0", f"bold {tones.text_primary}")])
        if release is None:
            checkout = (0.0, "needs you", [("v2.14.0", f"bold {tones.waiting}")])
        else:
            checkout = (release, "live" if release >= 1 else "shipping", [("v2.14.0", f"bold {tones.text_success}")])
        fraction, status, detail = checkout
        self.query_one("#arc-checkout", ProgressArc).set(fraction, "checkout", status, detail)
        self.query_one("#arc-gateway", ProgressArc).set(1.0, "gateway", "live",
                                                        [("v5.1.2", f"bold {tones.text_success}")])

    def _tiles(self, tones: Tones) -> None:
        so_far = sum(DEPLOYS_WEEK[:TODAY + 1])
        before = sum(DEPLOYS_LAST_WEEK[:TODAY + 1])
        self.query_one("#tile-deploys", StatTile).set(so_far, so_far - before, DEPLOYS_WEEK, DEPLOYS_LAST_WEEK, DAYS,
                                                      today=TODAY)
        self.query_one("#tile-requests", StatTile).set(f"{REQUESTS[-1]:.1f}k", round(REQUESTS[-1] - REQUESTS[0], 1),
                                                       REQUESTS)
        errors = self.query_one("#tile-errors", StatTile)
        errors.set_accent(tones.warning, tones.error)
        errors.chart.set_series(ERRORS_HOURLY, colors=(tones.warning, tones.error))
        errors.stat.set(str(ERRORS_HOURLY[-1]), ERRORS_HOURLY[-1] - ERRORS_HOURLY[-2], better="down")

    def _detail(self, s: Service, tones: Tones) -> None:
        colour, text_colour = tones.state(s.state), tones.state_text(s.state)
        panel = self.query_one("#detail", GlassPanel)
        panel.set_title(s.key.upper(), (s.label, f"bold {text_colour}"))
        panel.set_accent(colour)
        sprite = self.query_one(SpriteView)
        sprite.fallback_color = colour
        sprite.set_sprite(*beacon(s.state, tones))
        load = f"{s.cpu:.0%} · {fmt_rps(s.rps)}/s" if s.rps else "scaled to zero"
        self.query_one(InfoCard).set_card(
            Text.assemble((s.key, "bold"), " ", (s.version, tones.muted)),
            [("errors", Text(f"{s.errors:.1f}%", style=tones.text_error if s.errors >= 1 else "")),
             ("load", load), ("host", s.host), ("team", s.team), ("region", s.region)],
            s.note)
        stat = self.query_one(StatValue)
        if s.p95:
            change = s.latency[-1] - s.latency[0]
            delta = round(change / 1000, 2) if s.p95 >= 1000 else change
            stat.set(f"p95 {fmt_ms(s.p95)}", delta, better="down", color=text_colour)
        else:
            stat.set("p95 –", None, color=tones.muted)
        chart = (colour, tones.foreground) if s.p95 else (tones.muted, tones.muted)
        self.query_one(Telemetry).set_series(s.latency, colors=chart)
