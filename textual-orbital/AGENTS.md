# Working on textual-orbital

Read [`README.md`](README.md) first: it says what orbital does and documents its public API. The rules that keep it working in Ghostty (rules 9, 13 and 20 matter most for dashboards), the layer table and the test harness are in [`../AGENTS.md`](../AGENTS.md); read them before changing anything. This file has a complete walkthrough for building a dashboard, the rules for wiring real sources, how to add a widget or a rule to the library, and the tests. Everything here was learned against Ghostty 1.4 and Textual 8.2.8.

## Where things live

Modules in `src/textual_orbital/` (everything public is exported from `textual_orbital`):

| Module | What it does |
| --- | --- |
| `model.py` | The records: `Entity`, `AttentionItem`, `TrackItem`, `Event`, `Goal`, `Task`, `DoneTask`, `SourceStatus`, `Alert` (and `ALERT_STYLES`), `Metrics`, `Snapshot`, `WeekStats`, `DayCount` and `week_stats`, `queue_load`. |
| `provider.py` | **The data seam.** `Source`, `SourceData`, `Provider`, `StaticProvider`, the `Store` that merges per-source data into a `Snapshot` (and rejects naive datetimes and duplicate keys), `timed_fetch`, `fetch_timeout`, `poll_delay`. |
| `alerts.py` | `AlertQueue` (priority, dedupe, snooze, resolve, snoozes restored from a file), `Rule`, `Condition`, `AlertRules` (snapshot transitions to alerts, with the startup baseline), `WaitingRule`, `alert_color`. |
| `notify.py` | `Notifier`: OSC 777 / OSC 9 notifications, the OSC 2 title badge and the bell, with Ghostty 1.4's behaviour documented; `notification_mode`. |
| `theme.py` | `ORBITAL` and `ORBITAL_NAVY` (Textual themes with glass's variables), `PALETTE`, the source and "why" colour tables, colour helpers, `fmt_age`, `fmt_interval`, `ellipsize`, `now`. |
| `app.py` | `OrbitalApp(Glass, App)`: polling (a daemon thread and a deadline per fetch, backoff), publishing snapshots in guarded steps, `report_error`, health, the minute tick, alerts and their popups or toasts (`alert_view`, `finish_alert`, moving a toast to the screen on top), `fetch`, notifications, the title badge, snoozing (and the snooze file), opening links, breakpoints, `call_from_worker`. |
| `widgets/header.py` | `HeaderStrip`, `Brand`, `SourceHealth`, `Clock`, `KeyBar`. |
| `widgets/beacons.py` | `BeaconRow`, `state_text`, `order_entities`, `STATE_COLORS`, `STATE_GLYPHS`. |
| `widgets/lists.py` | `PriorityList`, `TaskBoard`, `EventLog`, `DoneList` (all `SelectList` subclasses), `Section`, `signal_glyphs`, `task_color`. |
| `widgets/track.py` | `Track`, `TrackTable`, `status_word`, `HEALTH_COLORS`, `HEALTH_WORDS`. |
| `widgets/page.py` | `Page`. |
| `widgets/screens.py` | `OrbitalModal`, `AlertScreen`, `AlertToast`, `DetailScreen` (with lazy values), `HelpScreen`, `SourcesScreen`, `LEGEND`, `beacon_view`, `hint`. |

Elsewhere in `textual-orbital/`:

| Path | What it is |
| --- | --- |
| `examples/console/app.py` | Mission Console: keys, detail views for every record type, `--provider`, `--fake-speed`, `--static`, `--plain`, `--snooze-file`, `--alert-style`, `--toast-timeout`; `ConsoleToast` (its `HINTS`) and the keys it binds to `finish_alert`. |
| `examples/console/pages.py`, `console.tcss` | Its four pages and their layouts per breakpoint; the Week page steps through past weeks (`[` `]`) with `Snapshot.week(on)` and `OrbitalApp.fetch`. |
| `examples/console/rules.py` | `ReleaseFailedRule` and `IncidentRule`: app rules that read the demo's own record from `data`; `NeedsYouRule`, the library's `WaitingRule` with its alerts kept popups (`style="modal"`). |
| `examples/console/demo.py` | `DemoProvider`: five demo sources over one scripted world, and `history(first, last)`, a slow call for older weeks. Only for the demo and the tests; don't import it from real code. |
| `tests/` | `test_units.py`, `test_app.py` (`OrbitalApp` over fake sources), `test_pilot.py`, `test_pty.py`, `hung_app.py` (a dashboard whose source never returns, for the pty), `conftest.py` (the `output`, `no_browser` and `outside_tmux` fixtures), `screenshots.py`. |

## Build a dashboard

This walkthrough goes from an empty directory to a running, tested dashboard: **CI Board**, which watches CI pipelines. It has one source (a JSON file you can edit while it runs, then GitHub Actions), one alert rule, two pages that adapt to the window size, and `--provider` to swap the data. Every file below runs as shown.

### 1. The project

```sh
uv init --app ci-board && cd ci-board && rm main.py
uv add "textual-glass[images,testing] @ git+https://github.com/misterek/textual-glass#subdirectory=textual-glass" \
       "textual-orbital @ git+https://github.com/misterek/textual-glass#subdirectory=textual-orbital"
uv add --dev pytest pytest-asyncio
mkdir ci_board tests && touch ci_board/__init__.py
```

The `testing` extra brings the pty harness for screenshots (step 6). From a local checkout of the libraries, use the editable line in the repository's [README](../README.md#install) instead of the two git URLs. The end result:

```
ci-board/
  pyproject.toml
  pipelines.json        the data, for now
  ci_board/
    __init__.py
    sources.py          your record, the source, make_provider()
    rules.py            the alert rule
    pages.py            two pages
    board.tcss          the layout and its breakpoints
    app.py              the app, its keys, main()
    github.py           a real source (step 7)
  tests/
    conftest.py         fixtures: capture glass's output, never open the browser
    test_board.py
    shots.py            approximate screenshots
```

### 2. The data and the source

`pipelines.json`, one row per pipeline (workflow and branch):

```json
[
  {"repo": "web", "workflow": "deploy", "branch": "main", "run": 412, "status": "running",
   "url": "https://ci.example.com/web/412"},
  {"repo": "api", "workflow": "ci", "branch": "fix-auth", "run": 977, "status": "failed",
   "url": "https://ci.example.com/api/977"},
  {"repo": "api", "workflow": "ci", "branch": "main", "run": 975, "status": "passed"},
  {"repo": "docs", "workflow": "build", "branch": "main", "run": 88, "status": "running"},
  {"repo": "worker", "workflow": "ci", "branch": "retry-jobs", "run": 301, "status": "queued"}
]
```

`ci_board/sources.py` keeps your own record (`Pipeline`) and maps it to orbital's generic records in one function, `to_items`. Every pipeline becomes a `TrackItem` on the stages `QUEUED`, `RUNNING`, `DONE`; a failed one also becomes an `AttentionItem` in the queue, linked back to its track item by `ref`. Your record rides along in `data`.

```python
"""Where the data comes from: one Source per system."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from textual_orbital import AttentionItem, SourceData, StaticProvider, TrackHealth, TrackItem, now

STAGES = ("QUEUED", "RUNNING", "DONE")
#: status -> (stage index, health)
STATUS = {"queued": (0, TrackHealth.PENDING), "running": (1, TrackHealth.PENDING),
          "passed": (2, TrackHealth.OK), "failed": (2, TrackHealth.FAIL)}


@dataclass(frozen=True)
class Pipeline:
    """Your own record. It rides along in ``data``; the library never reads it."""

    repo: str
    workflow: str
    branch: str
    run: int
    status: str  # queued | running | passed | failed
    url: str | None = None


def to_items(p: Pipeline, source: str, since: datetime | None) -> tuple[TrackItem, AttentionItem | None]:
    """One pipeline as a track item, plus a queue row when it failed."""
    key = f"{source}:{p.repo}:{p.workflow}:{p.branch}"  # stable across fetches
    stage, health = STATUS[p.status]
    item = TrackItem(key=key, source=source, title=f"{p.repo} {p.workflow}", label=f"#{p.run}", stage=stage,
                     health=health, stage_since=since, url=p.url, detail=f"{p.status} · {p.branch}", data=p)
    if p.status != "failed":
        return item, None
    return item, AttentionItem(key=f"fix:{key}", source=source, score=8, why="fix build",
                               title=f"{p.repo} {p.workflow}", where=p.branch, detail=f"run #{p.run} failed",
                               since=since, url=p.url, ref=("track", key))


class PipelineSource:
    """Pipelines from a JSON file (edit it while the board runs)."""

    key, name, interval = "CI", "CI pipelines", 10.0  # interval: seconds between fetches

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entered: dict[tuple[str, int], datetime] = {}  # (pipeline, stage) -> first seen there

    def fetch(self) -> SourceData:  # runs in a worker thread; may block
        rows = json.loads(self.path.read_text())  # a missing or broken file raises: the source turns red
        at = now()
        entered: dict[tuple[str, int], datetime] = {}
        track, attention = [], []
        for row in rows:
            p = Pipeline(**row)
            stage = STATUS[p.status][0]
            ident = f"{p.repo}:{p.workflow}:{p.branch}"
            since = entered[(ident, stage)] = self._entered.get((ident, stage), at)  # the file has no timestamps
            item, row_item = to_items(p, self.key, since)
            track.append(item)
            if row_item is not None:
                attention.append(row_item)
        self._entered = entered  # forget pipelines that are gone
        return SourceData(track=track, attention=attention)


def make_provider() -> StaticProvider:
    """What ``--provider ci_board.sources:make_provider`` calls (the default)."""
    return StaticProvider([PipelineSource("pipelines.json")])
```

Check the source before there is any UI:

```sh
uv run python -c "
from ci_board.sources import make_provider
from textual_orbital import timed_fetch
for source in make_provider().sources():
    data, error, ms = timed_fetch(source)
    print(source.key, error, f'{ms:.1f} ms', len(data.track), 'track items', len(data.attention), 'attention items')
"
```

It prints `CI None 0.2 ms 5 track items 1 attention items`.

### 3. The alert rule

`ci_board/rules.py`. A rule yields a `Condition` for every alert-worthy situation that holds *now*; orbital runs the rules after every fetch of any source, raises the alert when a condition first appears and drops it when it goes away. The run number in the key makes a new failing run a new alert. Read your record back typed from `data`.

```python
"""Alert rules: what is worth a popup."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from textual_orbital import Alert, Condition, Snapshot, TrackHealth

from .sources import Pipeline


class PipelineFailedRule:
    """A pipeline failed. A new failing run (a new run number) is a new alert."""

    kind = "pipeline failed"

    def conditions(self, snapshot: Snapshot) -> Iterator[Condition]:
        for t in snapshot.track:
            if t.health is not TrackHealth.FAIL:
                continue
            p = cast(Pipeline, t.data)  # your own record, typed again
            key = f"failed:{t.key}:{p.run}"
            yield Condition(key, t.source, Alert(
                key=key, kind=self.kind, headline="PIPELINE FAILED", title=t.title,
                body=(f"run #{p.run} on {p.branch}",),
                priority=80, color="#ff4d6d", url=t.url, target=("track", t.key)))
```

Failures already in the file when the board starts are the baseline: they show in the queue but don't pop up. Only kinds in `AlertRules.startup_kinds` (by default `"waiting"`) alert at startup.

### 4. The pages and the layout

`ci_board/pages.py`. A `Page` gets every new snapshot in `update_snapshot` and fills its widgets. `KEYS` is what the key bar shows for it, and `DEFAULT_FOCUS` takes focus when it shows. `self.snoozed()` lists the items the user snoozed.

```python
"""The pages. Each one fills its widgets from every new snapshot."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical

from textual_glass import GlassPanel, RingGauge
from textual_orbital import Page, PriorityList, Snapshot, Track, TrackHealth, TrackTable, health_gradient

from .sources import STAGES


class OverviewPage(Page):
    KEYS = [("1-2", "pages"), ("j/k", "move"), ("⏎", "details"), ("tab", "panel"), ("?", "help"), ("q", "quit")]
    DEFAULT_FOCUS = "#queue"

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with GlassPanel(title="NEEDS ATTENTION", id="queue-panel"):
                yield PriorityList(id="queue")
            with Vertical(id="side"):
                with GlassPanel(title="HEALTH", id="gauge-panel"):
                    yield RingGauge("PASSING", id="passing")
                with GlassPanel(title="PIPELINES", id="track-panel"):
                    yield Track(STAGES, id="track")

    def update_snapshot(self, snapshot: Snapshot) -> None:
        snoozed = self.snoozed()
        items = [i for i in snapshot.needs_attention if i.key not in snoozed]
        self.query_one("#queue", PriorityList).set_items(items, at=snapshot.taken_at)
        self.query_one("#queue-panel", GlassPanel).set_title("NEEDS ATTENTION", (str(len(items)), "bold"))
        self.query_one("#track", Track).set_items(snapshot.track, snapshot.taken_at)
        done = [t for t in snapshot.track if t.health in (TrackHealth.OK, TrackHealth.FAIL)]
        passed = sum(1 for t in done if t.health is TrackHealth.OK)
        share = passed / len(done) if done else 1.0
        self.query_one("#passing", RingGauge).set(share, f"{share:.0%}", health_gradient(share),
                                                  sublabel=f"{passed} of {len(done)}")


class RunsPage(Page):
    KEYS = [("1-2", "pages"), ("j/k", "move"), ("⏎", "details"), ("?", "help"), ("q", "quit")]
    DEFAULT_FOCUS = "#runs"

    def compose(self) -> ComposeResult:
        with GlassPanel(title="RUNS", id="runs-panel"):
            yield TrackTable(STAGES, id="runs")

    def update_snapshot(self, snapshot: Snapshot) -> None:
        failing = [t for t in snapshot.track if t.health is TrackHealth.FAIL]
        others = [t for t in snapshot.track if t.health is not TrackHealth.FAIL]
        self.query_one("#runs", TrackTable).set_groups([("FAILING", failing), ("OTHERS", others)], snapshot.taken_at)
```

The PASSING gauge is worked out from the track. A number that no record carries (a disk's fill, a queue depth from an API) comes from a source instead: return it in `SourceData(metrics={"disk": 0.42})`, rounded to what the gauge shows, and read it in `update_snapshot` with `snapshot.metric("disk")` ([README](README.md#the-data-model) has a complete example).

`ci_board/board.tcss`. `OrbitalApp` sets breakpoint classes on every screen, so the layout adapts with plain CSS:

```css
/* OrbitalApp puts width and height classes on every screen:
   -narrow <80, -compact 80-111, -regular 112-139, -wide 140+; -short <25, -mid 25-37, -tall 38+ */
#pages { height: 1fr; }
Page { height: 1fr; }
#main { height: 1fr; }
#queue-panel { width: 1fr; height: 1fr; }
#side { width: 40; height: 1fr; }
#gauge-panel { height: 9; }
#track-panel { height: 1fr; }
#runs-panel { height: 1fr; }

Screen.-narrow #side { display: none; }  /* a small window keeps only the queue */
Screen.-wide #side { width: 60; }
Screen.-short #gauge-panel { display: none; }
```

### 5. The app

`ci_board/app.py`. `OrbitalApp` does the polling, merging, alerts and notifications; your subclass composes the header, the pages and the key bar, binds keys (`OrbitalApp` has none), and says what `enter` shows (`show_detail`, which alerts use too through `jump_to`). `main()` takes `--provider module:factory` and passes `open_urls=True`, because this is the real dashboard; tests never do (rule 20).

```python
"""CI Board: a textual-orbital dashboard.

    uv run python -m ci_board.app
    uv run python -m ci_board.app --provider mymodule:make_provider
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.widgets import ContentSwitcher

from textual_glass import SelectList
from textual_orbital import (
    SOURCE_COLORS,
    WHY_COLORS,
    Alert,
    AttentionItem,
    DetailScreen,
    HeaderStrip,
    HelpScreen,
    KeyBar,
    OrbitalApp,
    Provider,
    SourcesScreen,
    Track,
    TrackItem,
    fmt_age,
    now,
)
from textual_orbital.theme import PALETTE
from textual_orbital.widgets import HEALTH_COLORS, status_word

from .pages import OverviewPage, RunsPage
from .rules import PipelineFailedRule
from .sources import STAGES, Pipeline

PAGES = [("overview", "Overview", OverviewPage), ("runs", "Runs", RunsPage)]
SOURCE_COLORS.update({"CI": "#4ea5ff", "GA": "#d6e2ff"})  # badge colours for your source keys
WHY_COLORS["fix build"] = PALETTE["red"]  # and for your "why" words


class CIBoard(OrbitalApp):
    TITLE = "CI Board"
    CSS_PATH = Path(__file__).parent / "board.tcss"
    BINDINGS = [("q", "quit", "quit"), ("1", "page(0)", "overview"), ("2", "page(1)", "runs"),
                ("o", "open", "open"), ("s", "snooze", "snooze"), ("h", "sources", "sources"),
                ("question_mark", "help", "help")]

    def __init__(self, provider: Provider, **kwargs: Any) -> None:
        super().__init__(provider, rules=(PipelineFailedRule(),), **kwargs)

    def compose(self) -> ComposeResult:
        yield HeaderStrip("CI BOARD", [name for _, name, _ in PAGES], id="header")
        with ContentSwitcher(initial=PAGES[0][0], id="pages"):
            for page_id, _, page in PAGES:
                yield page(id=page_id)
        yield KeyBar(OverviewPage.KEYS, id="keys")

    def on_mount(self) -> None:
        self.call_after_refresh(self.query_one(OverviewPage).focus_default)

    def alert_beacon(self, alert: Alert) -> str:
        return "error"  # the drawn beacon in the popup: every alert here is a failure

    def action_page(self, index: int) -> None:
        if self.screen is not self.screen_stack[0]:
            return  # not under a popup
        page_id, _, page = PAGES[index]
        self.query_one("#pages", ContentSwitcher).current = page_id
        self.query_one(HeaderStrip).brand.set_page(index)
        self.query_one(KeyBar).set_keys(page.KEYS)
        self.call_after_refresh(self.query_one(f"#{page_id}", page).focus_default)

    def _row(self) -> Any:
        focused = self.focused
        if isinstance(focused, SelectList):
            return focused.highlighted
        if isinstance(focused, Track):
            return focused.selected
        return None

    def action_open(self) -> None:
        self.open_link(getattr(self._row(), "url", None))

    def action_snooze(self) -> None:
        row = self._row()
        if isinstance(row, AttentionItem):
            self.snooze_item(row.key)  # hidden for an hour (ITEM_SNOOZE)

    def action_sources(self) -> None:
        self.push_screen(SourcesScreen(self.store.statuses(), errors=self.errors))  # and what the app caught

    def action_help(self) -> None:
        self.push_screen(HelpScreen([("1 2", "pages"), ("j k", "move"), ("enter", "details"), ("o", "open"),
                                     ("s", "snooze 1h"), ("h", "source health"), ("q", "quit")]))

    def on_select_list_selected(self, message: SelectList.Selected) -> None:
        self.show_detail(message.row)

    def on_track_selected(self, message: Track.Selected) -> None:
        self.show_detail(message.item)

    def show_detail(self, record: Any) -> None:
        """What enter shows, from a list or from an alert (``jump_to``)."""
        if isinstance(record, AttentionItem) and record.ref and self.snapshot:
            record = self.snapshot.track_item(record.ref[1]) or record
        if isinstance(record, TrackItem):
            p = record.data if isinstance(record.data, Pipeline) else None
            in_stage = fmt_age(now() - record.stage_since) if record.stage_since else "–"
            screen = DetailScreen(
                record.title, record.label,
                [("stage", STAGES[record.stage]), ("status", status_word(record)), ("in stage", in_stage),
                 ("branch", p.branch if p else "–"), ("link", record.url or "–")],
                url=record.url, accent=HEALTH_COLORS[record.health], kind="PIPELINE")
            url = record.url
            self.push_screen(screen, lambda result: self.open_link(url) if result == "open" else None)


def load_provider(spec: str) -> Provider:
    """``module:factory`` -> the provider the factory returns."""
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name or "make_provider")()


def main() -> None:
    parser = argparse.ArgumentParser(description="CI Board")
    parser.add_argument("--provider", default="ci_board.sources:make_provider", metavar="MODULE:FACTORY")
    parser.add_argument("--plain", action="store_true", help="text only, no pixels")
    args = parser.parse_args()
    sys.path.insert(0, os.getcwd())  # so --provider finds modules next to you
    CIBoard(load_provider(args.provider), open_urls=True, snooze_file="~/.cache/ci-board/snoozes.json",
            glass=False if args.plain else None).run()


if __name__ == "__main__":
    main()
```

What each piece does:

- `HeaderStrip` shows the tabs, a health dot per source and the clock. The app feeds the dots (`health_changed`); the clock lives outside the pages, so `minute_tick` ticks it.
- `ContentSwitcher` holds the pages. A page switch lands its glass in the same frame as its text (rule 19).
- Every `SelectList` (the queue, the table) posts `SelectList.Selected` on `enter`; `Track` posts `Track.Selected`.
- `DetailScreen` dismisses with `"open"` when the user presses `o` in it; the callback opens the link. A value that takes a slow call (the run's log, its comments) can be a zero-argument callable, `("log", lambda: fetch_log(record.key))`: it loads in a thread while "loading…" shows.
- `snooze_file` keeps what you snoozed (`s`) across restarts, so a restart doesn't bring every snoozed row and popup back.

### 6. Run it, test it, look at it

In Ghostty:

```sh
uv run python -m ci_board.app           # glass
uv run python -m ci_board.app --plain   # text only
```

While it runs, change a `"status"` in `pipelines.json` to `"failed"`: within ten seconds (the source's interval) the pipeline turns red on the track, a row joins the queue, and a "PIPELINE FAILED" popup appears with a desktop notification. Break the JSON and the CI dot turns into a red `✕`; `h` shows the error.

You can't see Ghostty from a test, so check it headlessly (see [`../AGENTS.md`](../AGENTS.md#checking-your-work-without-seeing-the-terminal)). Add to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
pythonpath = ["."]
```

`tests/conftest.py`: one fixture captures what glass would send to the terminal, the other makes sure no test opens the real browser (rule 20):

```python
import webbrowser

import pytest

import textual_glass._textual_hooks as hooks
from textual_glass.testing.pilot_helpers import Output


@pytest.fixture(autouse=True)
def no_browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No test may open the real browser: record the URLs instead."""
    opened: list[str] = []

    def record(url: str, *args: object, **kwargs: object) -> bool:
        opened.append(url)
        return True

    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, record)
    monkeypatch.setenv("BROWSER", "true")
    return opened


@pytest.fixture
def output(monkeypatch: pytest.MonkeyPatch) -> Output:
    """What glass would send to the terminal, captured; output.model() checks it."""
    out = Output()
    monkeypatch.setattr(hooks, "driver_write", out)
    return out
```

`tests/test_board.py` runs the app headless with glass forced on, then breaks a pipeline and waits for the popup:

```python
"""CI Board, headless: glass forced on, its output captured, nothing sent to a terminal."""

import asyncio
import json
import shutil
from collections.abc import Callable
from pathlib import Path

from ci_board.app import CIBoard
from ci_board.sources import PipelineSource
from textual_glass.engine import MAIN
from textual_glass.items import Ring
from textual_glass.testing.pilot_helpers import Output, idle, start
from textual_orbital import AlertScreen, StaticProvider


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


async def test_a_new_failure_raises_an_alert(output: Output, no_browser: list[str], tmp_path: Path) -> None:
    data = tmp_path / "pipelines.json"
    shutil.copy("pipelines.json", data)
    app = CIBoard(StaticProvider([PipelineSource(data)]), notify="off")  # open_urls stays False in tests
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)  # glass on, as in Ghostty
        await until(lambda: app.snapshot is not None)
        await idle(app, pilot)
        assert len(app.snapshot.track) == 5
        assert any(isinstance(item, Ring) for item in app.glass.layers[MAIN].dynamic), "the gauge's ring"
        assert not isinstance(app.screen, AlertScreen), "failures already there at startup are the baseline"
        await pilot.press("o")  # the queue's row has a link: shown, not opened
        assert no_browser == []

        rows = json.loads(data.read_text())
        rows[0]["status"] = "failed"
        data.write_text(json.dumps(rows))
        app.poll_all()
        await until(lambda: isinstance(app.screen, AlertScreen))
        await idle(app, pilot)
        assert app.screen.alert.headline == "PIPELINE FAILED"
    assert output.model().errors == [], "valid kitty graphics"
```

`tests/shots.py` runs the real app in a pty at three sizes and writes approximate composites you can look at (with the Read tool, or an image viewer):

```python
"""Approximate screenshots of CI Board at three sizes: uv run python tests/shots.py"""

import sys
from pathlib import Path

from textual_glass.testing.harness import Step
from textual_glass.testing.screenshots import shoot

ROOT = Path(__file__).parents[1]
STEPS = [Step(3.0, "overview"), Step(0.0, "to runs", "2"), Step(1.5, "runs"), Step(0.0, "quit", "q")]

for size in ((70, 22), (90, 28), (140, 45)):
    for path in shoot([sys.executable, "-m", "ci_board.app"], str(ROOT), size, STEPS, ("overview", "runs"),
                      str(ROOT / "shots")):
        print(path)
```

```sh
uv run pytest
uv run python tests/shots.py      # shots/70x22_overview.png ... shots/140x45_runs.png
```

The composites show layout, overlap and truncation; they are not Ghostty (no animation, no dim text). The last check is the user's: ask them to run it in Ghostty and tell you what they see. Never open a terminal window or take a screenshot yourself.

### 7. Real data, and `--provider`

`ci_board/github.py` reads GitHub Actions through the `gh` CLI (installed and logged in) and reuses `to_items`, so the pages and the rule work unchanged. The API has timestamps, so `stage_since` comes from it instead of a cache.

```python
"""GitHub Actions runs through the gh CLI (installed and logged in).

    CI_BOARD_REPOS="me/web me/api" uv run python -m ci_board.app --provider ci_board.github:make_provider
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime

from textual_orbital import SourceData, StaticProvider

from .sources import Pipeline, to_items

FIELDS = "databaseId,workflowName,headBranch,status,conclusion,createdAt,startedAt,updatedAt,url"


def status_of(run: dict) -> str:
    if run["status"] == "completed":
        return "passed" if run["conclusion"] in ("success", "neutral", "skipped") else "failed"
    return "running" if run["status"] == "in_progress" else "queued"


def when(text: str) -> datetime | None:
    return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None  # aware, as the model wants


class ActionsSource:
    key, name, interval = "GA", "GitHub Actions", 60.0

    def __init__(self, repos: list[str]) -> None:
        self.repos = repos

    def fetch(self) -> SourceData:
        track, attention = [], []
        for repo in self.repos:
            gh = subprocess.run(["gh", "run", "list", "--repo", repo, "--limit", "30", "--json", FIELDS],
                                capture_output=True, text=True, timeout=30)
            if gh.returncode:  # raise: the source turns red and this line is the reason (h)
                raise RuntimeError(f"gh run list {repo}: {gh.stderr.strip() or gh.returncode}")
            latest: dict[tuple[str, str], dict] = {}
            for run in json.loads(gh.stdout):  # newest first: keep the latest run per workflow and branch
                latest.setdefault((run["workflowName"], run["headBranch"]), run)
            for run in latest.values():
                status = status_of(run)
                p = Pipeline(repo.split("/")[-1], run["workflowName"], run["headBranch"], run["databaseId"], status,
                             run["url"])
                since = when({"queued": run["createdAt"], "running": run["startedAt"]}.get(status, run["updatedAt"]))
                item, row = to_items(p, self.key, since)  # the API has timestamps: no cache needed
                track.append(item)
                if row is not None:
                    attention.append(row)
        return SourceData(track=track, attention=attention)


def make_provider() -> StaticProvider:
    return StaticProvider([ActionsSource(os.environ["CI_BOARD_REPOS"].split())])
```

```sh
CI_BOARD_REPOS="me/web me/api" uv run python -m ci_board.app --provider ci_board.github:make_provider
```

To watch both, put both sources in one provider: `StaticProvider([PipelineSource("pipelines.json"), ActionsSource([...])])`. Each source has its own key, interval and health dot.

## Wiring real sources

Write one `Source` per system. A source is a plain object with `key`, `name`, `interval` and `fetch() -> SourceData`; no base class is needed.

- **Keys.** The source `key` is short ("GH", "CI") and unique; it is the badge in lists and the health dot. Queues and tables show two characters of it (`EventLog` three), so "GIT" shows as "GI". Record keys must be stable across fetches (`AttentionItem.key`, `TrackItem.key`, `Entity.id`, `Task.id`): selection, snooze, alert dedupe and diffing all depend on them. They must also be unique across sources, because snoozes, `item()` and jumps use the bare key: prefix them with the source key (`"CI:web:deploy:main"`). A fetch whose attention key, track key or entity id another source already uses fails, as "duplicate key 'X' (also from GH): prefix keys with the source" (rule 23 in [`../AGENTS.md`](../AGENTS.md)).
- **What to return.** Return a `SourceData`. Each field you set *replaces* that source's previous value for that field. `None` (the default) means "this source doesn't report this", and `[]` means "it reports none". So a source that owns the track must return `track=[]` when the track is empty, or the old items stay (and `metrics={}` when it has no numbers left). Sources never see each other's data; the store concatenates them.
- **Errors.** Raise on failure; don't return partial or empty data. The first line of the exception becomes the red dot's reason (`h`), and the last good data stays on screen. `subprocess.run(..., check=True)` raises without the error output, so check `returncode` and raise with `stderr`.
- **Timestamps.** Use timezone-aware datetimes (`textual_orbital.now()`, or `datetime.fromisoformat` on an ISO string with an offset; a naive one fails the fetch, as "naive datetime in AttentionItem.since"), and put *when it started* in the record: `since`, `stage_since`, `opened_at`, `status_since`, `done_at`. Never an age, a count of minutes, or "updated just now". Ages are computed at render time through `fmt_age`, so time passing doesn't make the data "change", and a fetch that returns equal data costs nothing downstream (rule 9).
- **`stage_since`** drives the trail and "stuck" on the `Track`. Use the API's "entered this stage at" if it has one. If not, keep a small cache in the source keyed by `(item key, stage)`, as `PipelineSource` does, and drop keys that are gone.
- **Your own records in `data`.** Keep everything you know about an item in a frozen dataclass of your own and put it in `data` (`Entity`, `AttentionItem`, `TrackItem`, `Event`). It must be hashable: no lists or dicts in it (use tuples). Derive the generic fields (`stage`, `health`, `score`, `why`) from it in one function, and read it back in rules and detail views with `cast(MyRecord, item.data)`.
- **Attention items.** `score` is 0-10 and orders the queue (9+ is red). `why` is a short verb ("review", "respond", "fix build"); add a colour for yours to `WHY_COLORS`. `section="aware"` puts an item in the be-aware list. `ref=("track", key)` or `("entity", id)` links an item to a record, so `enter` can show that record.
- **Numbers.** For a gauge fed straight from a system (a disk's fill, a queue depth), return `metrics={"name": value}` and read `snapshot.metric("name")`. Round each value to what the screen shows (a fraction shown as a whole percent: `round(x, 2)`): every changed value is a content change and redraws (rule 9). Prefix the names with the source key (`"MD.disk"`) when two sources might use the same one: they merge in source order and the later one wins.
- **Entities.** `EntityState.WAITING` means "needs you"; `since` is when it entered that state, so a new wait gets a new `since` and therefore a new alert from `WaitingRule`. **Keep `since` stable while the wait lasts**: `since=now()` on every fetch is a new wait on every poll, and the popup comes back every few seconds. Without an API timestamp, remember the first time you saw the wait, as `PipelineSource` does for stages. Pass `WaitingRule(source="XX")` with your source key: its first fetch is the baseline. An entity doesn't need a `BeaconRow` to count: `WAITING` ones drive the title badge and `WaitingRule` either way.
- **Events.** Emit one `Event` per thing that happened, for at least this week and last week; `Snapshot.week()` counts them by `kind`. There is no separate stats API. For `week(on)` of last week (counted against the week before it), report one more week; older weeks are fetched on demand (below).
- **Intervals.** Claude Code sessions every 10 s, most APIs every 60 s. Each fetch runs in a daemon thread of its own, and the next one starts `interval` seconds after it ends, so a slow fetch is never re-run at once. A failing source waits twice as long after each failure in a row (timeouts included), up to 15 minutes, which spares rate-limited APIs; `r` (`app.poll_all()`) fetches now anyway.
- **Timeouts.** Give every network call and subprocess in `fetch()` its own timeout (`subprocess.run(..., timeout=30)`, as `ActionsSource` does): a thread can't be stopped from outside. The app also gives each fetch a deadline, the source's `timeout` attribute or `max(2 * interval, 10)` seconds. Past it the source turns red, "timed out after 20 s", the late result is ignored, and after two such hung fetches the source pauses until one returns (rule 21 in [`../AGENTS.md`](../AGENTS.md)). The next fetch may start while a hung one is still blocked, so keep `fetch()` safe to run twice at once: `PipelineSource` replaces its cache in one assignment.
- **Test a source without the UI:** `timed_fetch(source)` returns `(data, error, ms)`. For a source that calls a CLI or an API, test the mapping against canned output (monkeypatch `subprocess.run` or your HTTP call).

## Fetching on demand: a past week

Sources are fetched on their interval only. Anything with parameters (a past week, a pull request's comments) is fetched on demand with `OrbitalApp.fetch(func, *args, then=callback, timeout=30.0)`: `func(*args)` runs in a daemon thread of its own with a deadline, and `then(data, error)` runs on the app thread, guarded like every other step (rule 22). Past the deadline `then` gets `(None, TimeoutError)` and the late result is dropped (rule 21). `then` runs even if what asked has moved on, so check there that the result is still wanted.

A Week page that steps back with `[` and `]`: the snapshot holds this week and last (`Snapshot.week(on)`), and older weeks come from your own `history(first, last)` call, fetched once each. The console's `WeekPage` (`examples/console/pages.py`) is the full version, with tiles and a cache of fetched weeks.

```python
from dataclasses import replace
from datetime import date, timedelta
from typing import cast

from textual.app import ComposeResult
from textual.binding import Binding

from textual_glass import GlassPanel
from textual_orbital import Event, EventLog, OrbitalApp, Page, Snapshot, WeekStats
from textual_orbital.model import monday_of


def history(first: date, last: date) -> list[Event]:
    """Events from first to last, from your API (with its own timeouts)."""
    return []


class WeekPage(Page):
    BINDINGS = [Binding("left_square_bracket", "step(-1)"), Binding("right_square_bracket", "step(1)")]
    snapshot: Snapshot | None = None
    week_on: date | None = None  # the Monday shown; None for this week

    def compose(self) -> ComposeResult:
        with GlassPanel(title="THIS WEEK", id="week-panel"):
            yield EventLog(id="events")

    def update_snapshot(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.show_week()

    def action_step(self, weeks: int) -> None:
        if self.snapshot is not None:
            this = monday_of(self.snapshot.taken_at.astimezone().date())
            monday = (self.week_on or this) + timedelta(weeks=weeks)
            self.week_on = None if monday >= this else monday
            self.show_week()

    def show_week(self) -> None:
        snapshot, monday = self.snapshot, self.week_on
        if snapshot is None:
            return
        if monday is None or monday >= monday_of(snapshot.taken_at.astimezone().date()) - timedelta(weeks=1):
            self.show(snapshot.week(monday))  # this week or last: the snapshot holds them
            return
        self.query_one("#week-panel", GlassPanel).set_title(f"WEEK OF {monday:%b %d}", ("loading…", "dim"))
        cast(OrbitalApp, self.app).fetch(history, monday - timedelta(weeks=1), monday + timedelta(days=6),
                                         then=lambda events, error: self.fetched(monday, events, error))

    def fetched(self, monday: date, events: list[Event] | None, error: BaseException | None) -> None:
        if monday != self.week_on or self.snapshot is None:
            return  # the user stepped on: not wanted any more
        if error is not None or events is None:
            self.query_one("#week-panel", GlassPanel).set_title(f"WEEK OF {monday:%b %d}", (str(error), "red"))
            return
        self.show(replace(self.snapshot, events=tuple(events)).week(monday))  # counted like the snapshot's own

    def show(self, week: WeekStats) -> None:
        self.query_one("#events", EventLog).set_rows(list(week.events))
        self.query_one("#week-panel", GlassPanel).set_title(f"WEEK OF {week.start:%b %d}",
                                                           (f"{len(week.events)} events", "dim"))
```

## Adding a widget

1. Subclass `PixelWidget`, or `SelectList` for a selectable list (both from `textual_glass.widgets`). Follow "Write your own pixel widget" in [textual-glass's README](../textual-glass/README.md#write-your-own-pixel-widget): text in `render_line` with `strip(parts, width)`, a text fallback with the same layout, items from `pixel_items(ctx)` in screen cells with stable `ident`s, sprites through `self.glass.sprite(...)`.
2. Take plain records and give it a setter (`set_items(items, at=None)`). Store `at` and give it `tick(at)` if it shows ages, so `Page.minute_tick` updates them; format every age with `fmt_age`.
3. Colours come from `PALETTE` and the helpers in `theme.py` (`urgency_color`, `source_color`...), or from constructor arguments.
4. Don't add attributes Textual already has (rule 14). Don't write anything for popups: glass dims the screen below.
5. To add it to the library: put it in the right `widgets/` module, export it from `widgets/__init__.py` and `textual_orbital/__init__.py`, add it to `test_app_and_widgets_add_no_attribute_textual_has`, and test it in `tests/test_pilot.py` in both modes (see `test_widgets_take_any_stages_and_kinds`). If it shows a new kind of visual, add a row to `LEGEND` in `widgets/screens.py` and to the README.

## An exit effect for rows that leave the queue

`PriorityList.set_items` replaces its rows at once, so a row that is snoozed, resolved or dropped by its source just disappears. After each `set_items` the list posts `PriorityList.RowsLeft`, whose `items` are the ones that left (in neither section any more; folding the be-aware section doesn't count). Handle it in the page, no subclass needed. This one says what cleared in the panel's title for three seconds, in both modes, and costs two redraws (rule 9):

```python
from textual_glass import GlassPanel
from textual_orbital import Page, PriorityList


class OverviewPage(Page):
    cleared = 0  # rows that left in the last three seconds

    def on_priority_list_rows_left(self, message: PriorityList.RowsLeft) -> None:
        count = len(message.items)
        self.cleared += count
        self.show_cleared()
        self.set_timer(3.0, lambda: self.forget(count))

    def forget(self, count: int) -> None:
        self.cleared -= count
        self.show_cleared()

    def show_cleared(self) -> None:  # call it from update_snapshot too, where the title is set
        self.query_one("#queue-panel", GlassPanel).set_title(
            "NEEDS ATTENTION", (f"✓ {self.cleared} cleared" if self.cleared else "", "green"))
```

For a pixel effect, keep each item that left with the time it left, and have a `PixelWidget` over the list yield a fading item for it (a `Rule` with a falling `alpha`) for a fraction of a second: call `ctx.animating()` from `pixel_items` while anything fades, so the engine ticks until it is gone and then goes quiet again (rule 9).

## Adding a rule

- **In an app**, write a class with `conditions(snapshot)` (step 3 above, or `examples/console/rules.py`) and pass it in `rules=(...)`. Include `WaitingRule()` if you have entities. The `Condition`'s key must change when the situation is "new again"; its `source` decides the startup baseline; add the kind to `startup_kinds` (`OrbitalApp.alert_rules.startup_kinds`) only if it should alert for conditions already true at startup.
- **For a one-off**, call `app.raise_alert(Alert(key=..., kind=..., title=..., body=(...), headline=..., priority=..., color=..., url=..., target=...))`. The key dedupes it; it is not dropped by `resolve`.
- **Pictures:** `OrbitalApp(alert_images={"pipeline failed": "~/art.gif"})`, or `image=` on the alert. Without one, the popup draws a beacon in the alert's colour, in the state `alert_beacon(alert)` returns.
- **Popups or toasts:** `OrbitalApp(alert_style="toast")` shows alerts as toasts in the bottom-right corner that take no keys and go away after `toast_timeout` seconds; a rule can keep its own alerts popups with `style="modal"` (the console's `NeedsYouRule`). A toast has no keys of its own: bind yours to `app.finish_alert("snooze")` and `("dismiss")` while `isinstance(app.alert_view, AlertToast)`, and name them in a subclass's `HINTS` (set `ALERT_TOAST` to it), as the console does.
- **In the library**, a rule must be generic (only the generic records, never `data`). Add it to `alerts.py`, export it, and add a unit test in `tests/test_units.py` like `test_waiting_rule_alert` and `test_rules_alert_on_transitions_not_on_baseline`.

## Tests

From the repository root:

```sh
uv run pytest textual-orbital/tests                     # about two minutes; test_pty.py is the slow part
uv run pytest textual-orbital/tests/test_units.py       # seconds
uv run ruff check . && uv run mypy
uv run python textual-orbital/tests/test_pty.py --png /tmp/orbital-steps   # a composite per pty step
uv run python textual-orbital/tests/screenshots.py      # regenerate docs/screenshots
uv run python textual-orbital/examples/console/app.py --fake-speed 30      # every alert in a few seconds (in Ghostty)
```

The tests import the console example's modules by name (`app`, `demo`, `pages`, `rules`); `tests/conftest.py` puts `examples/console` on the path. The README's [Tests](README.md#tests) section says what each file checks.

## Known gaps

- Untested in kitty and in tmux. Notifications were checked only against Ghostty's source; `--notify auto` picks OSC 9 for iTerm2, WezTerm and kitty untested. Under tmux they go through DCS passthrough (`allow-passthrough on`), checked only in a recorded byte stream.
- The window title is cleared on exit, not restored: Ghostty 1.4 has no title pop.
- The widgets' text colours come from `PALETTE`, made for dark themes.
- `OrbitalApp` has no key bindings; every app binds its own (see the console's and CI Board's `BINDINGS`).
- A finite-play GIF below a popup plays its run again after the popup closes.
