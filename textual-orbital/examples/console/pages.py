"""The four pages of Mission Console, built only from orbital and glass widgets.

Each page gets ``update_snapshot(snapshot)`` whenever the data changes (and
when items are snoozed) and ``minute_tick(at)`` for ages and timers.
"""

from __future__ import annotations

from datetime import datetime, timezone

from demo import PROD, STAGE, STAGES
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical

from textual_glass import GlassPanel, InfoCard, ProgressArc, RingGauge, StatTile
from textual_orbital import (
    AttentionItem,
    BeaconRow,
    DoneList,
    EntityState,
    EventLog,
    Page,
    PriorityList,
    Snapshot,
    TaskBoard,
    Track,
    TrackHealth,
    TrackItem,
    TrackTable,
    queue_load,
)
from textual_orbital.theme import PALETTE, fmt_age, health_gradient, load_color, source_color, urgency_color, why_color

TASK_GROUPS = ("Projects", "Ongoing", "Small tasks")
#: Event kinds and their colours (the Week page's timeline and tiles).
EVENT_COLORS = {
    "shipped": PALETTE["green"],
    "merged": PALETTE["violet"],
    "reviewed": PALETTE["accent"],
    "ticket closed": PALETTE["teal"],
    "ticket opened": PALETTE["blue"],
    "incident": PALETTE["orange"],
}


def releases_title(panel: GlassPanel, name: str, items: list[TrackItem], track: Track) -> None:
    stuck = sum(1 for t in items if track.is_stuck(t))
    promote = sum(1 for t in items if t.stage == STAGE and t.health is TrackHealth.OK)
    panel.set_title(name, (str(len(items)), f"bold {PALETTE['text_soft']}"),
                    (f"◉ {stuck} stuck" if stuck else "", f"bold {PALETTE['red']}"),
                    (f"▲ {promote} promote" if promote else "", f"bold {PALETTE['green']}"))


# ================================================================== console ==


class ConsolePage(Page):
    """Page 1: what needs me, at a glance, in a corner window."""

    KEYS = [("1-4", "pages"), ("j/k", "move"), ("⏎", "open"), ("tab", "panel"), ("o", "browser"),
            ("s", "snooze"), ("space", "be aware"), ("h", "sources"), ("?", "help"), ("q", "quit")]
    DEFAULT_FOCUS = "#queue"

    def compose(self) -> ComposeResult:
        with GlassPanel(title="CLAUDE CODE", id="beacons-panel"):
            yield BeaconRow(empty="no Claude Code sessions", id="beacons")
        with Horizontal(id="console-main"):
            with GlassPanel(title="NEEDS ATTENTION", id="queue-panel"):
                yield PriorityList(id="queue")
            with Vertical(id="console-side"):
                with GlassPanel(title="GAUGES", id="gauges-panel"), Horizontal(id="gauges"):
                    yield RingGauge("LOAD", id="g-load")
                    yield RingGauge("PAGES", id="g-pages")
                    yield RingGauge("RELEASES", id="g-releases")
                with GlassPanel(title="SELECTED", id="preview-panel"):
                    yield InfoCard(id="preview")
                with GlassPanel(title="RELEASES", id="track-panel"):
                    yield Track(STAGES, label_width=6, id="track")
        with GlassPanel(title="RELEASES", id="strip-panel"):
            yield Track(STAGES, id="track-strip")

    def on_select_list_highlighted(self, message: PriorityList.Highlighted) -> None:
        if isinstance(message.row, AttentionItem):
            self.preview(message.row)

    def preview(self, item: AttentionItem) -> None:
        """Fill the SELECTED card (shown on wide, tall windows) with the highlighted row."""
        at = datetime.now(timezone.utc)
        self.query_one("#preview", InfoCard).set_card(
            item.title,
            [("why", Text(item.why, style=f"bold {why_color(item.why)}")),
             ("score", Text(f"{item.score} / 10", style=f"bold {urgency_color(item.score)}")),
             ("waiting", fmt_age(at - item.since) if item.since else "–"),
             ("detail", item.detail or "–")],
            body=item.url or "",
            subtitle=Text.assemble((item.source, f"bold {source_color(item.source)}"), ("  ", ""),
                                   (item.where, PALETTE["text_soft"])))

    def update_snapshot(self, snapshot: Snapshot) -> None:
        at = snapshot.taken_at
        snoozed = self.snoozed()
        attention = [i for i in snapshot.needs_attention if i.key not in snoozed]
        aware = [i for i in snapshot.be_aware if i.key not in snoozed]
        queue_list = self.query_one("#queue", PriorityList)
        queue_list.set_items(attention, aware, at)
        if isinstance(queue_list.highlighted, AttentionItem):
            self.preview(queue_list.highlighted)
        load, capacity = queue_load(snapshot, snoozed)
        top = attention[0].score if attention else 0
        queue = self.query_one("#queue-panel", GlassPanel)
        queue.set_title("NEEDS ATTENTION", (str(len(attention)), f"bold {urgency_color(top)}"),
                        (f"load {load}", PALETTE["text_dim"]),
                        (f"{len(snoozed)} snoozed" if snoozed else "", PALETTE["text_faint"]))
        queue.set_accent(urgency_color(top) if top >= 7 else PALETTE["accent"], PALETTE["accent_2"])

        entities = list(snapshot.entities)
        self.query_one("#beacons", BeaconRow).set_entities(entities, at)
        waiting = sum(1 for e in entities if e.state is EntityState.WAITING)
        errors = sum(1 for e in entities if e.state is EntityState.ERROR)
        beacons = self.query_one("#beacons-panel", GlassPanel)
        beacons.set_title("CLAUDE CODE", (str(len(entities)), f"bold {PALETTE['text_soft']}"),
                          (f"◉ {waiting} waiting" if waiting else "", f"bold {PALETTE['amber']}"),
                          (f"✕ {errors} error" if errors else "", f"bold {PALETTE['red']}"))
        beacons.set_accent(PALETTE["amber"] if waiting else PALETTE["accent"], PALETTE["accent_2"])

        fraction = min(1.0, load / capacity)
        self.query_one("#g-load", RingGauge).set(fraction, str(load), load_color(fraction), sublabel="attention")
        pages = [i for i in attention if i.source == "IM"]
        paged = min(1.0, len(pages) / 4)
        self.query_one("#g-pages", RingGauge).set(paged, str(len(pages)), load_color(paged), sublabel="on call")
        moving = [t for t in snapshot.track if t.health is not TrackHealth.IDLE]
        healthy = sum(1 for t in moving if t.health in (TrackHealth.OK, TrackHealth.PENDING))
        share = healthy / len(moving) if moving else 1.0
        self.query_one("#g-releases", RingGauge).set(share, f"{round(share * 100)}%", health_gradient(share),
                                                      sublabel=f"{healthy}/{len(moving)} healthy")

        items = list(snapshot.track)
        for track_id in ("#track", "#track-strip"):
            self.query_one(track_id, Track).set_items(items, at)
        track = self.query_one("#track", Track)
        releases_title(self.query_one("#track-panel", GlassPanel), "RELEASES", items, track)
        releases_title(self.query_one("#strip-panel", GlassPanel), "RELEASES", items, track)


# ================================================================= releases ==


class ReleasesPage(Page):
    """Page 2: every release on the big track, and in a table."""

    KEYS = [("1-4", "pages"), ("j/k", "move"), ("⏎", "open"), ("tab", "track/table"), ("o", "browser"),
            ("h", "sources"), ("?", "help"), ("q", "quit")]
    DEFAULT_FOCUS = "#release-table"

    def compose(self) -> ComposeResult:
        with GlassPanel(title="RELEASE TRACK", id="release-track-panel"):
            yield Track(STAGES, label_width=6, id="release-track")
        with GlassPanel(title="RELEASES", id="release-table-panel"):
            yield TrackTable(STAGES, id="release-table")

    def update_snapshot(self, snapshot: Snapshot) -> None:
        at = snapshot.taken_at
        items = sorted(snapshot.track, key=lambda t: (-t.stage, t.opened_at or at))
        flying = [t for t in items if t.stage < PROD or t.health is not TrackHealth.OK]
        shipped = [t for t in items if t not in flying]
        track = self.query_one("#release-track", Track)
        track.set_items(items, at)
        self.query_one("#release-table", TrackTable).set_groups([("IN FLIGHT", flying), ("IN PROD", shipped)], at)
        releases_title(self.query_one("#release-track-panel", GlassPanel), "RELEASE TRACK", items, track)
        failing = sum(1 for t in items if t.health is TrackHealth.FAIL)
        self.query_one("#release-table-panel", GlassPanel).set_title(
            "RELEASES", (f"{len(flying)} in flight, {len(shipped)} in prod", PALETTE["text_soft"]),
            (f"✕ {failing} failing" if failing else "", f"bold {PALETTE['red']}"))


# ===================================================================== week ==

TILES: list[tuple[str, str, tuple[str, ...], tuple[str, str], str]] = [
    ("t-shipped", "SHIPPED", ("shipped",), (PALETTE["green"], PALETTE["teal"]), "up"),
    ("t-merged", "MERGED", ("merged",), (PALETTE["violet"], PALETTE["magenta"]), "up"),
    ("t-reviewed", "REVIEWED", ("reviewed",), (PALETTE["accent"], "#7c8cff"), "up"),
    ("t-closed", "TICKETS CLOSED", ("ticket closed",), (PALETTE["teal"], PALETTE["green"]), "up"),
    ("t-incidents", "INCIDENTS", ("incident",), (PALETTE["orange"], PALETTE["red"]), "down"),
]


class WeekPage(Page):
    """Page 3: this week in brief, as telemetry."""

    KEYS = [("1-4", "pages"), ("j/k", "move"), ("⏎", "open"), ("o", "browser"), ("?", "help"), ("q", "quit")]
    DEFAULT_FOCUS = "#events"

    def compose(self) -> ComposeResult:
        with Horizontal(id="tiles"):
            for tile_id, label, _, colors, _ in TILES:
                yield StatTile(label, versus="vs last wk", colors=colors, id=tile_id)
        with GlassPanel(title="THIS WEEK", id="log-panel"):
            yield EventLog(colors=EVENT_COLORS, id="events")

    def update_snapshot(self, snapshot: Snapshot) -> None:
        week = snapshot.week()
        today = (snapshot.taken_at.astimezone().date() - week.start).days
        labels = [d.day.strftime("%a")[:1] for d in week.days]
        for tile_id, _, kinds, _, better in TILES:
            series = [sum(d.counts.get(k, 0) for k in kinds) for d in week.days]
            ghost = [sum(d.counts.get(k, 0) for k in kinds) for d in week.previous]
            total = sum(series)
            previous = sum(ghost[: week.elapsed])
            self.query_one(f"#{tile_id}", StatTile).set(total, total - previous, series, ghost, labels, today,
                                                         better="down" if better == "down" else "up")
        self.query_one("#events", EventLog).set_rows(list(week.events))
        self.query_one("#log-panel", GlassPanel).set_title(
            f"THIS WEEK · since {week.start:%a %b} {week.start.day}",
            (f"{len(week.events)} events", PALETTE["text_soft"]),
            (f"{week.total('shipped')} shipped", f"bold {PALETTE['green']}"))


# ==================================================================== board ==


class BoardPage(Page):
    """Page 4: goals as progress arcs, the work in flight, and what landed."""

    KEYS = [("1-4", "pages"), ("j/k", "move"), ("⏎", "open"), ("tab", "tasks/done"), ("o", "browser"),
            ("?", "help"), ("q", "quit")]
    DEFAULT_FOCUS = "#tasks"
    MAX_ARCS = 6

    def compose(self) -> ComposeResult:
        with GlassPanel(title="GOALS", id="goals-panel"), Horizontal(id="arcs"):
            for i in range(self.MAX_ARCS):
                yield ProgressArc(id=f"arc-{i}")
        with Horizontal(id="board-bottom"):
            with GlassPanel(title="IN FLIGHT", id="tasks-panel"):
                yield TaskBoard(id="tasks")
            with GlassPanel(title="DONE · 7 DAYS", id="done-panel"):
                yield DoneList(id="done")

    def update_snapshot(self, snapshot: Snapshot) -> None:
        goals = list(snapshot.goals)
        for i in range(self.MAX_ARCS):
            arc = self.query_one(f"#arc-{i}", ProgressArc)
            goal = goals[i] if i < len(goals) else None
            arc.display = goal is not None
            if goal is None:
                continue
            mine = [t for t in snapshot.tasks if t.goal_id == goal.id]
            blocked = sum(1 for t in mine if t.status == "blocked")
            count = f"{len(mine)} task{'s' if len(mine) != 1 else ''}"
            detail = [(count, PALETTE["text_soft"] if mine else PALETTE["text_ghost"])]
            if blocked:
                detail.append((f" · {blocked} blocked", f"bold {PALETTE['red']}"))
            arc.set(goal.progress, goal.name, goal.owner, detail)
        mean = sum(g.progress for g in goals) / len(goals) if goals else 0
        self.query_one("#goals-panel", GlassPanel).set_title(
            "GOALS", (f"{len(goals)} goals", PALETTE["text_soft"]),
            (f"{round(mean * 100)}% overall", PALETTE["text_dim"]))
        tasks = list(snapshot.tasks)
        self.query_one("#tasks", TaskBoard).set_tasks(tasks, TASK_GROUPS, snapshot.taken_at)
        blocked = sum(1 for t in tasks if t.status == "blocked")
        self.query_one("#tasks-panel", GlassPanel).set_title(
            "IN FLIGHT", (str(len(tasks)), f"bold {PALETTE['text_soft']}"),
            (f"✕ {blocked} blocked" if blocked else "", f"bold {PALETTE['red']}"))
        done = sorted(snapshot.done, key=lambda d: d.done_at, reverse=True)
        self.query_one("#done", DoneList).set_rows(done)
        self.query_one("#done-panel", GlassPanel).set_title("DONE · 7 DAYS",
                                                            (str(len(done)), f"bold {PALETTE['green']}"))


#: (id, tab name, page class), in key order.
PAGES: list[tuple[str, str, type[Page]]] = [
    ("console", "Console", ConsolePage),
    ("releases", "Releases", ReleasesPage),
    ("week", "Week", WeekPage),
    ("board", "Board", BoardPage),
]

