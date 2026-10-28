"""Mission Console: the textual-orbital example. Four pages over a scripted
demo world with five sources (Claude Code, GitHub, a ticket tracker, an infra
monitor and a security scanner).

Run it from the repository root, in Ghostty::

    uv run python textual-orbital/examples/console/app.py
    uv run python textual-orbital/examples/console/app.py --fake-speed 10   # every alert within seconds
    uv run python textual-orbital/examples/console/app.py --plain           # text only, as in any terminal
    uv run python textual-orbital/examples/console/app.py --snooze-file ~/.cache/console-snoozes.json
    uv run python textual-orbital/examples/console/app.py --provider mydata:make_provider

Keys: 1-4 pages, j/k or arrows to move, Enter for details, Tab between panels,
o to open in the browser, s to snooze, z for what is snoozed, h for source
health, t for a test alert, ? for help, q to quit.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import ContentSwitcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from demo import STAGES, DemoProvider, Release  # noqa: E402
from pages import PAGES  # noqa: E402
from rules import INCIDENT, RELEASE_FAILED, IncidentRule, ReleaseFailedRule  # noqa: E402

from textual_glass import SelectList  # noqa: E402
from textual_orbital import (  # noqa: E402
    SOURCE_COLORS,
    WHY_COLORS,
    Alert,
    AttentionItem,
    BeaconRow,
    DetailScreen,
    DoneTask,
    Entity,
    Event,
    HeaderStrip,
    HelpScreen,
    KeyBar,
    OrbitalApp,
    Page,
    Provider,
    SourceHealth,
    SourcesScreen,
    Task,
    Track,
    TrackItem,
    WaitingRule,
)
from textual_orbital.theme import PALETTE, fmt_age, now, source_color, urgency_color, why_color  # noqa: E402
from textual_orbital.widgets import (  # noqa: E402
    HEALTH_COLORS,
    STATE_COLORS,
    STATE_GLYPHS,
    beacon_view,
    state_text,
    status_word,
    task_color,
)

HERE = Path(__file__).parent

# The demo's sources and verbs get colours of their own (the library knows CC and GH).
SOURCE_COLORS.update({"TT": "#4ea5ff", "IM": "#c77dff", "SS": "#ff5c7a"})
WHY_COLORS.update({"incident": PALETTE["red"], "fix release": PALETTE["red"], "assigned": PALETTE["orange"],
                   "canary": PALETTE["magenta"], "promote": PALETTE["green"], "patch": PALETTE["orange"],
                   "due today": PALETTE["amber"], "warning": PALETTE["amber"]})

ALERT_KINDS = ("waiting", RELEASE_FAILED, INCIDENT)

HELP_KEYS = [  # 14 lines: the dialog then leaves the beacons in view at 90x28
    ("1-4", "switch page"),
    ("j k ↑ ↓", "move in a list"),
    ("← →", "beacons, dots"),
    ("tab", "next panel"),
    ("enter", "details (alert: jump)"),
    ("o", "open in browser"),
    ("s", "snooze (1h; alert 10m)"),
    ("u z", "wake all / list snoozed"),
    ("space", "fold the be-aware list"),
    ("[ ]", "older / newer week"),
    ("r h", "refresh / source health"),
    ("t", "test alert"),
    ("? q", "this help / quit"),
]


class MissionConsole(OrbitalApp):
    """The example dashboard."""

    CSS_PATH = HERE / "console.tcss"
    TITLE = "Mission Console"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "quit", priority=True),
        Binding("ctrl+c", "quit", show=False, priority=True),
        Binding("1", "page(0)", show=False),
        Binding("2", "page(1)", show=False),
        Binding("3", "page(2)", show=False),
        Binding("4", "page(3)", show=False),
        Binding("o", "open", show=False),
        Binding("s", "snooze", show=False),
        Binding("u", "wake", show=False),
        Binding("z", "snoozed", show=False),
        Binding("r", "refresh", show=False),
        Binding("h,H", "sources", show=False),
        Binding("question_mark", "help", show=False),
        Binding("t", "test_alert", show=False),
    ]

    def __init__(self, provider: Provider, **kwargs: Any) -> None:
        super().__init__(provider, rules=(WaitingRule(), ReleaseFailedRule(), IncidentRule()), **kwargs)
        self.page = 0

    # -- layout -----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield HeaderStrip("MISSION CONSOLE", [name for _, name, _ in PAGES], id="header")
        with ContentSwitcher(initial=PAGES[0][0], id="pages"):
            for page_id, _, cls in PAGES:
                yield cls(id=page_id)
        yield KeyBar(PAGES[0][2].KEYS, id="keys")

    def on_mount(self) -> None:
        self.call_after_refresh(self.current_page().focus_default)
        self._status()

    def current_page(self) -> Page:
        return self.query_one(f"#{PAGES[self.page][0]}", Page)

    def action_page(self, index: int) -> None:
        if not 0 <= index < len(PAGES) or self.screen is not self.screen_stack[0]:
            return  # no page switches under a popup
        self.page = index
        self.query_one("#pages", ContentSwitcher).current = PAGES[index][0]
        self.query_one("#header", HeaderStrip).brand.set_page(index)
        self.query_one("#keys", KeyBar).set_keys(PAGES[index][2].KEYS)
        self.call_after_refresh(self.current_page().focus_default)

    def alert_beacon(self, alert: Alert) -> str:
        return "error" if alert.kind in (RELEASE_FAILED, INCIDENT) else "waiting"

    # -- data hooks ---------------------------------------------------------------------

    def badge_changed(self, text: str | None, waiting: int) -> None:
        self.query_one("#header", HeaderStrip).brand.set_badge(f"{waiting} WAITING" if waiting else None)

    def minute_tick(self, at: datetime) -> None:
        super().minute_tick(at)  # pages and the header's clock
        self._status()

    def snapshot_changed(self, snapshot: Any) -> None:
        super().snapshot_changed(snapshot)
        self._status()

    def _status(self) -> None:
        parts: list[tuple[str, str]] = []
        if self.snoozed:
            parts.append((f"{len(self.snoozed)} snoozed  ", PALETTE["text_faint"]))
        # The last good fetch of any source (not the last change, nor the last attempt): it stops
        # moving when everything fails. At minute resolution, so the minute tick keeps it current.
        last_ok = max((s.last_ok for s in self.store.statuses() if s.last_ok), default=None)
        if last_ok is not None:
            parts.append((f"updated {last_ok.astimezone():%H:%M}", PALETTE["text_faint"]))
        self.query_one("#keys", KeyBar).set_status(parts)

    # -- actions -------------------------------------------------------------------------

    def _focused_row(self) -> Any:
        focused = self.focused
        if isinstance(focused, (SelectList, BeaconRow)):
            return focused.highlighted
        if isinstance(focused, Track):
            return focused.selected
        return None

    def action_open(self) -> None:
        self.open_link(getattr(self._focused_row(), "url", None))

    def action_snooze(self) -> None:
        row = self._focused_row()
        if isinstance(row, AttentionItem):
            self.snooze_item(row.key)
            self.notify(f"Snoozed for an hour: {row.title}", timeout=3)
            self._status()
        else:
            self.notify("Select a row in the queue to snooze it", timeout=2)

    def action_wake(self) -> None:
        count = self.wake_items()
        self.notify(f"Woke {count} snoozed item{'s' if count != 1 else ''}", timeout=2)
        self._status()

    def action_snoozed(self) -> None:
        """What is snoozed, and when each item comes back (``snoozed`` and ``alert_queue.snoozed``)."""
        at = now()
        alerts = len(self.alert_queue.snoozed)
        if not self.snoozed and not alerts:
            self.notify("Nothing is snoozed", timeout=2)
            return
        fields: list[tuple[str, Text | str]] = []
        for key, until in sorted(self.snoozed.items(), key=lambda pair: pair[1]):
            item = self.snapshot.item(key) if self.snapshot else None
            back = (f"  back in {fmt_age(until - at)}", PALETTE["text_dim"])
            fields.append((item.source if item else "–", Text.assemble((item.title if item else key, PALETTE["text"]),
                                                                        back)))
        subtitle = "u wakes them" + (f"  ·  {alerts} alert{'s' if alerts != 1 else ''} snoozed" if alerts else "")
        self.push_screen(DetailScreen(f"{len(self.snoozed)} snoozed", subtitle, fields, kind="SNOOZED"))

    def action_refresh(self) -> None:
        self.poll_all()
        self.notify("Refreshing every source", timeout=2)

    def action_sources(self) -> None:
        self.push_screen(SourcesScreen(self.store.statuses(), errors=self.errors))

    def on_source_health_clicked(self, message: SourceHealth.Clicked) -> None:
        self.action_sources()

    def action_help(self) -> None:
        self.push_screen(HelpScreen(HELP_KEYS))

    def action_test_alert(self) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.raise_alert(Alert(key=f"test:{stamp}", kind="waiting", title="test-transmission",
                               body=("this is a test · mission console", "Press s to snooze, esc to dismiss."),
                               priority=100, image=self.alert_rules.images.get("waiting"), headline="NEEDS YOU",
                               notify=False))

    # -- details ---------------------------------------------------------------------------

    def on_select_list_selected(self, message: SelectList.Selected) -> None:
        self.show_detail(message.row)

    def on_beacon_row_selected(self, message: BeaconRow.Selected) -> None:
        self.show_detail(message.entity)

    def on_track_selected(self, message: Track.Selected) -> None:
        self.show_detail(message.item)

    def show_detail(self, record: Any) -> None:
        snap = self.snapshot
        if snap is None:
            return
        at = now()
        screen: DetailScreen
        if isinstance(record, AttentionItem):
            linked: Any = None
            if record.ref is not None:
                kind, key = record.ref
                linked = snap.entity(key) if kind == "entity" else snap.track_item(key) if kind == "track" else None
            if linked is not None:
                self.show_detail(linked)
                return
            screen = DetailScreen(
                record.title, Text.assemble((record.source, f"bold {source_color(record.source)}"), ("  ", ""),
                                            (record.where, PALETTE["text_soft"])),
                [("why", Text(record.why, style=f"bold {why_color(record.why)}")),
                 ("score", Text(f"{record.score} / 10", style=f"bold {urgency_color(record.score)}")),
                 ("detail", record.detail or "–"), ("waiting", fmt_age(at - record.since) if record.since else "–"),
                 ("list", "needs attention" if record.section == "attention" else "be aware"),
                 ("link", record.url or "–")],
                url=record.url, accent=urgency_color(record.score), kind="ITEM")
        elif isinstance(record, Entity):
            colour = STATE_COLORS.get(record.state, PALETTE["accent"])
            screen = DetailScreen(
                record.name,
                Text.assemble(("CC", f"bold {source_color('CC')}"), ("  ", ""), (record.where, PALETTE["text_soft"])),
                [("state", Text.assemble(*state_text(record, at))),
                 ("since", fmt_age(at - record.since) if record.since else "–"),
                 ("reason", record.reason or "–"), ("link", record.url or "–")],
                body=record.detail,
                visual=beacon_view(record.state.value, colour, STATE_GLYPHS.get(record.state, "●")),
                url=record.url, accent=colour, kind="CLAUDE CODE SESSION")
        elif isinstance(record, TrackItem):
            colour = HEALTH_COLORS[record.health]
            release = record.data if isinstance(record.data, Release) else None
            track = Track(STAGES, label_width=6, id="detail-track")
            track.set_items(list(snap.track), at)
            track.selected_key = record.key
            stage = STAGES[max(0, min(len(STAGES) - 1, record.stage))]
            screen = DetailScreen(
                record.title, Text.assemble((record.source, f"bold {source_color(record.source)}"), ("  ", ""),
                                            (record.label, PALETTE["text_soft"]),
                                            (f"  by {release.owner}" if release else "", PALETTE["text_dim"])),
                [("stage", Text(stage, style=f"bold {colour}")),
                 ("status", Text(status_word(record), style=colour)),
                 ("in stage", fmt_age(at - record.stage_since) if record.stage_since else "–"),
                 ("started", fmt_age(at - record.opened_at) + " ago" if record.opened_at else "–"),
                 ("run", str(release.run) if release else "–"), ("link", record.url or "–")],
                visual=track, url=record.url, accent=colour, kind="RELEASE")
        elif isinstance(record, Task):
            colour = task_color(record.status)
            goal = next((g.name for g in snap.goals if g.id == record.goal_id), "–")
            screen = DetailScreen(
                record.title,
                Text.assemble(("TT", f"bold {source_color('TT')}"), ("  ", ""), (record.id, PALETTE["text_soft"])),
                [("status", Text(record.status, style=f"bold {colour}")), ("assignee", record.assignee or "–"),
                 ("list", record.group or "–"),
                 ("in status", fmt_age(at - record.status_since) if record.status_since else "–"),
                 ("goal", goal), ("link", record.url or "–")],
                url=record.url, accent=colour, kind="TASK")
        elif isinstance(record, Event):
            screen = DetailScreen(
                record.title, Text.assemble((record.source, f"bold {source_color(record.source)}"), ("  ", ""),
                                            (record.where, PALETTE["text_soft"])),
                [("what", record.kind), ("when", f"{record.when.astimezone():%a %d %b %H:%M}"),
                 ("link", record.url or "–")],
                url=record.url, kind="EVENT")
        elif isinstance(record, DoneTask):
            finished = f"{record.done_at.astimezone():%a %d %b %H:%M}"
            screen = DetailScreen(record.title, record.id, [("status", record.status), ("finished", finished),
                                                            ("link", record.url or "–")], url=record.url, kind="DONE")
        else:
            return
        url = getattr(record, "url", None)
        self.push_screen(screen, lambda result: self.open_link(url) if result == "open" else None)


def load_provider(spec: str) -> Provider:
    """``module:factory`` -> the provider that factory returns."""
    module_name, _, attr = spec.partition(":")
    module = importlib.import_module(module_name)
    factory = getattr(module, attr or "make_provider")
    provider = factory() if callable(factory) else factory
    if not isinstance(provider, Provider):
        raise TypeError(f"{spec} did not return a Provider (it needs a sources() method)")
    return provider


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Mission Console: a textual-orbital dashboard over a demo world.")
    parser.add_argument("--provider", metavar="MODULE:FACTORY", help="use a real provider instead of the demo")
    parser.add_argument("--fake-speed", type=float, default=1.0, help="run the demo world faster (tests use 10-40)")
    parser.add_argument("--static", action="store_true", help="freeze the demo world (nothing changes)")
    parser.add_argument("--plain", action="store_true", help="text only, no pixels")
    parser.add_argument("--no-alerts", action="store_true", help="no popups")
    parser.add_argument("--notify", default="auto", choices=["auto", "osc777", "osc9", "off"],
                        help="desktop notifications (default: by terminal)")
    parser.add_argument("--alert-image", metavar="PATH", help="a picture or GIF for every alert")
    parser.add_argument("--snooze-file", metavar="PATH", help="keep snoozes in this JSON file across restarts")
    args = parser.parse_args(argv)

    if args.provider:
        sys.path.insert(0, os.getcwd())
        provider = load_provider(args.provider)
    else:
        provider = DemoProvider(speed=args.fake_speed, dynamics=not args.static)
    images = dict.fromkeys(ALERT_KINDS, args.alert_image) if args.alert_image else {}
    app = MissionConsole(provider, alert_images=images, alerts=not args.no_alerts, notify=args.notify,
                         snooze_file=args.snooze_file, open_urls=bool(args.provider),
                         glass=False if args.plain else None)
    app.run()


if __name__ == "__main__":
    main()
