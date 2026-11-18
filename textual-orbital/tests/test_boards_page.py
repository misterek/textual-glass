"""``BoardsPage`` in pilot, with a fake runner (a callable returning ``Frame``s) and ``set_panels`` driven directly:
panels come and go, replaced panels run again, boards switch and vanish, the grid follows ``boards.layout.plan``,
``+`` ``-`` ``<`` ``>`` arrange (and save through ``arrange``), ``x`` asks before it removes, ``enter`` opens the full
view, late results are dropped, history accumulates, and with glass the protocol stays clean and an idle page sends
nothing."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, cast

import pytest

from textual_glass.engine import MAIN
from textual_glass.items import Lines, Meter, Pill, Ring
from textual_glass.testing.pilot_helpers import Output, settle, start
from textual_orbital import OrbitalApp, Snapshot, StaticProvider
from textual_orbital.boards.layout import need, plan
from textual_orbital.boards.model import Frame, Kind, PanelSpec, Series, Size, Table
from textual_orbital.boards.page import BoardRow, BoardsPage, ConfirmRemove, EmptyState, PanelGrid, PanelScreen
from textual_orbital.boards.widgets import PanelInfo, PanelView, StatBody

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
T1 = T0.timestamp()


def spec(pid: str, board: str = "main", kind: Kind | str = "stat", order: int = 0, **fields: Any) -> PanelSpec:
    fields.setdefault("every", 3600.0)  # the tests run panels themselves
    if "data" not in fields:
        fields.setdefault("cmd", f"echo {pid}")
    return PanelSpec(id=pid * 8 if len(pid) == 1 else pid, board=board, title=f"panel {pid}", kind=cast(Kind, kind),
                     added_at=T0 + timedelta(seconds=order), **fields)


class FakeRunner:
    """Runs a panel the way ``boards.runner.run`` would, from a table of answers; thread-safe."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.values: dict[str, float] = {}
        self.fail: dict[str, str] = {}
        self.gates: dict[str, threading.Event] = {}
        self.lock = threading.Lock()

    def __call__(self, panel: PanelSpec) -> Frame:
        with self.lock:
            self.calls.append(panel.id)
            n = self.calls.count(panel.id)
        gate = self.gates.get(panel.id)
        if gate is not None:
            gate.wait(10)
        if panel.id in self.fail:
            raise RuntimeError(self.fail[panel.id])
        if panel.kind == "table":
            return Frame(table=Table(("NAME", "STATUS"), (("api", "Running"), ("db", "Pending"))), at=T1 + n)
        if panel.kind == "text":
            return Frame(text=f"run {n}", at=T1 + n)
        value = self.values.get(panel.id, 42.0)
        return Frame(series=(Series("", ((T1 + n, value),)),), at=T1 + n)

    def count(self, pid: str) -> int:
        with self.lock:
            return self.calls.count(pid)


class Arranged:
    """Records ``arrange(panel_id, size=..., position=...)`` calls; ``fail`` makes them raise."""

    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[tuple[str, Size | None, float | None]] = []
        self.fail = fail

    def __call__(self, panel_id: str, *, size: Size | None = None, position: float | None = None) -> None:
        if self.fail:
            raise OSError(self.fail)
        self.calls.append((panel_id, size, position))


class Host(OrbitalApp):
    def __init__(self, runner: Callable[[PanelSpec], Frame], remove: Callable[[PanelSpec], object] | None = None,
                 arrange: Arranged | None = None, **kwargs: Any) -> None:
        super().__init__(StaticProvider([]), **kwargs)
        self.page_args: dict[str, Any] = {"runner": runner, "remove": remove, "arrange": arrange}

    def compose(self) -> Any:
        yield BoardsPage(id="page", **self.page_args)


async def until(pilot: Any, check: Callable[[], object], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not check() and loop.time() < end:
        await pilot.pause()
        await asyncio.sleep(0.02)
    assert check(), "timed out waiting"


def page_of(app: Host) -> BoardsPage:
    return app.query_one(BoardsPage)


def newest(page: BoardsPage, pid: str) -> float | None:
    """The panel's newest value, once its run is over."""
    state = page.runs[pid]
    if state.running or state.frame is None or not state.frame.series:
        return None
    return state.frame.series[0].last


def ids(page: BoardsPage) -> list[str]:
    return [view.spec.id for view in page.views()]


def widths(page: BoardsPage) -> list[int]:
    return [view.region.width for view in page.views()]


def rows_of(page: BoardsPage) -> list[list[int]]:
    """The views' widths, row by row (by their top line on screen)."""
    rows: dict[int, list[int]] = {}
    for view in page.views():
        rows.setdefault(view.region.y, []).append(view.region.width)
    return [rows[y] for y in sorted(rows)]


# -- layout ----------------------------------------------------------------------------------------------------


async def test_the_empty_state() -> None:
    app = Host(FakeRunner())
    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        page = page_of(app)
        empty = page.query_one(EmptyState)
        assert empty.display and not page.query_one(PanelGrid).display
        text = "\n".join(empty.render_line(y).text.strip() for y in range(empty.size.height))
        assert "No panels yet" in text and "ask Claude to add one" in text
        assert page.query_one(BoardRow).render_line(0).text.strip() == ""
        page.set_panels([spec("a")])
        await pilot.pause()
        assert not empty.display and page.query_one(PanelGrid).display
        page.set_panels([])
        await pilot.pause()
        assert empty.display, "the last panel gone: empty again"
        assert list(app.errors) == []


@pytest.mark.parametrize(("size", "columns"), [((60, 24), 1), ((90, 28), 3), ((150, 45), 4)])
async def test_the_grid_follows_the_plan(size: tuple[int, int], columns: int) -> None:
    app = Host(FakeRunner())
    async with app.run_test(size=size) as pilot:
        page = page_of(app)
        page.set_panels([spec(c, order=k) for k, c in enumerate("abcdef")])
        await pilot.pause()
        await pilot.pause()
        expected = plan([need("stat", "auto")] * 6, size[0], size[1] - 1)
        assert page.plan == expected
        regions = [view.region for view in page.views()]
        assert len({r.x for r in regions}) == columns, "smalls: 1 a row under 70, 3 from 70, 4 from 140"
        grid = page.query_one(PanelGrid)
        for row in rows_of(page):
            assert sum(row) == grid.scrollable_content_region.width, "every row is full"
        assert sorted({r.height for r in regions}) == sorted(set(expected.heights))
        if sum(expected.heights) <= grid.size.height:
            assert grid.max_scroll_y == 0, "everything fits: no scrolling"


async def test_one_panel_fills_the_page_and_sizes_share_rows() -> None:
    app = Host(FakeRunner())
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        page.set_panels([spec("a", kind="timeseries")])
        await pilot.pause()
        await pilot.pause()
        assert page.views()[0].region == (0, 1, 90, 27), "one panel: the whole page under the board row"
        page.set_panels([spec("a", kind="stat"), spec("b", kind="gauge", order=1), spec("c", kind="text", order=2)])
        await pilot.pause()
        await pilot.pause()
        assert rows_of(page) == [[45, 45], [90]], "two smalls, then a medium alone: each row stretched full"
        top, _, text = (view.region for view in page.views())
        assert top.height == need("gauge", "auto").height, "the smalls' row keeps its minimum"
        assert text.height == 27 - top.height, "the medium gets the spare height"


async def test_more_panels_than_fit_scroll_to_the_focused_one() -> None:
    app = Host(FakeRunner())
    async with app.run_test(size=(90, 24)) as pilot:
        page = page_of(app)
        page.set_panels([spec(f"{k:08x}", order=k) for k in range(15)])
        await until(pilot, lambda: isinstance(app.focused, PanelView))
        grid = page.query_one(PanelGrid)
        await until(pilot, lambda: grid.max_scroll_y > 0)
        for _ in range(4):
            await pilot.press("down")
        await pilot.pause()
        focused = cast(PanelView, app.focused)
        assert focused is page.views()[12], "down: the panel below, row by row"
        assert grid.scroll_y > 0, "scrolled to the focused panel"


# -- panels come and go ----------------------------------------------------------------------------------------


async def test_panels_appear_run_and_disappear() -> None:
    runner = FakeRunner()
    app = Host(runner)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        a, b = spec("a", kind="gauge"), spec("b", kind="table", order=1)
        page.set_panels([a, b])
        await until(pilot, lambda: all(page.runs[p.id].frame is not None for p in (a, b)))
        assert ids(page) == [a.id, b.id]
        assert runner.calls.count(a.id) == 1 and runner.calls.count(b.id) == 1, "each runs once when it appears"
        view = page.views()[0]
        assert view.frame is not None and view.body.display
        timer = page.runs[b.id].timer
        assert timer is not None, "a cmd panel runs again on its schedule"
        page.set_panels([a])
        await pilot.pause()
        assert ids(page) == [a.id] and b.id not in page.runs, "a removed panel loses its widget and its timer"
        page.set_panels([a])
        await pilot.pause()
        assert runner.calls.count(a.id) == 1, "the same panel again: no new run"
        assert list(app.errors) == []


async def test_a_replaced_panel_runs_again_at_once_and_its_old_result_is_dropped() -> None:
    runner = FakeRunner()
    app = Host(runner)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        old = spec("a", kind="stat")
        runner.gates[old.id] = threading.Event()
        runner.values[old.id] = 1.0
        page.set_panels([old])
        await until(pilot, lambda: runner.count(old.id) == 1)
        new = replace(old, updated_at=T0 + timedelta(minutes=5), title="panel a, again")
        page.set_panels([new])
        await pilot.pause()
        assert page.runs[old.id].spec.title == "panel a, again"
        runner.values[old.id] = 2.0
        runner.gates[old.id].set()  # both runs finish: the first one's result is for the old panel
        await until(pilot, lambda: runner.count(old.id) == 2 and page.runs[old.id].frame is not None)
        await until(pilot, lambda: not page.runs[old.id].running)
        frame = page.runs[old.id].frame
        assert frame is not None and [v for _, v in frame.series[0].points] == [2.0], "only the new panel's run"
        assert page.views()[0].spec.title == "panel a, again"


async def test_a_replaced_panel_of_another_kind_gets_a_new_widget() -> None:
    app = Host(FakeRunner())
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        page.set_panels([spec("a", kind="stat")])
        await pilot.pause()
        first = page.views()[0]
        page.set_panels([replace(spec("a", kind="gauge"), updated_at=T0 + timedelta(minutes=1))])
        await pilot.pause()
        assert page.views()[0] is not first and page.views()[0].kind == "gauge"
        await pilot.pause()
        assert widths(page) == [90], "laid out like the old one: alone, it fills the page"


async def test_results_for_removed_panels_are_dropped() -> None:
    runner = FakeRunner()
    app = Host(runner)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        gone = spec("a")
        runner.gates[gone.id] = threading.Event()
        page.set_panels([gone, spec("b", order=1)])
        await until(pilot, lambda: runner.count(gone.id) == 1)
        page.set_panels([spec("b", order=1)])
        runner.gates[gone.id].set()
        await asyncio.sleep(0.2)
        await pilot.pause()
        assert gone.id not in page.runs and ids(page) == [spec("b").id]
        assert list(app.errors) == [], "dropped quietly"


async def test_single_readings_accumulate_into_history() -> None:
    runner = FakeRunner()
    app = Host(runner)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        panel = spec("a", kind="stat", unit="ms")
        page.set_panels([panel])
        await until(pilot, lambda: page.runs[panel.id].frame is not None)
        await until(pilot, lambda: isinstance(app.focused, PanelView))
        for value in (10.0, 20.0, 30.0):
            runner.values[panel.id] = value
            await pilot.press("r")
            await until(pilot, partial(lambda v: newest(page, panel.id) == v, value))
        frame = page.runs[panel.id].frame
        assert frame is not None
        assert [v for _, v in frame.series[0].points] == [42.0, 10.0, 20.0, 30.0], "accumulate keeps the history"
        body = page.views()[0].body
        assert isinstance(body, StatBody) and body.spark.display, "a stat with history draws its sparkline"


async def test_every_panel_runs_on_its_schedule_also_on_another_board() -> None:
    runner = FakeRunner()
    app = Host(runner)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        here, there = spec("a", every=0.15), spec("b", board="ops", every=0.15)
        data = spec("c", kind="text", data="hello", order=2)
        page.set_panels([here, there, data])
        await until(pilot, lambda: runner.count(here.id) >= 3 and runner.count(there.id) >= 3, timeout=8)
        assert ids(page) == [here.id, data.id], "only the current board has widgets"
        assert runner.count(data.id) == 1, "a data panel runs once"
        assert page.runs[data.id].timer is None


async def test_a_failing_run_shows_and_the_board_row_counts_it() -> None:
    runner = FakeRunner()
    app = Host(runner)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        bad = spec("a", board="ops")
        runner.fail[bad.id] = "exited with status 2\nmore detail"
        page.set_panels([spec("b"), bad])
        await until(pilot, lambda: page.runs[bad.id].error is not None)
        assert page.runs[bad.id].error == "exited with status 2", "one line"
        row = page.query_one(BoardRow).render_line(0).text
        assert "◆ main 1" in row and "ops 1 ✕1" in row, row
        await pilot.press("right_square_bracket")
        await pilot.pause()
        view = page.views()[0]
        assert view.error == "exited with status 2" and view.message.display, "no good data: the error takes the body"


# -- boards ----------------------------------------------------------------------------------------------------


async def test_boards_switch_with_brackets_and_a_vanished_board_goes_to_the_nearest() -> None:
    app = Host(FakeRunner())
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        panels = [spec("a", board="alpha"), spec("b", board="beta"), spec("c", board="beta", order=1),
                  spec("d", board="gamma")]
        page.set_panels(panels)
        await pilot.pause()
        assert page.current == "alpha" and ids(page) == [spec("a").id]
        row = page.query_one(BoardRow).render_line(0).text
        assert row.startswith("◆ alpha 1") and "beta 2" in row and "gamma 1" in row
        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert page.current == "beta" and ids(page) == [spec("b").id, spec("c").id]
        assert "◆ beta 2" in page.query_one(BoardRow).render_line(0).text
        assert isinstance(app.focused, PanelView) and app.focused.spec.board == "beta", "focus moves to the board"
        await pilot.press("left_square_bracket", "left_square_bracket")
        await pilot.pause()
        assert page.current == "gamma", "wraps around"
        page.set_panels([p for p in panels if p.board != "gamma"])
        await pilot.pause()
        assert page.current == "beta", "the last board vanished: the one before it"
        page.set_panels([p for p in panels if p.board != "beta"])
        await pilot.pause()
        assert page.current == "gamma", "a middle board vanished: the next one"
        assert list(app.errors) == []


async def test_the_default_board_shows_first() -> None:
    app = Host(FakeRunner())
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        page.set_panels([spec("a", board="incident-db"), spec("b", board="main"), spec("c", board="zz")])
        await pilot.pause()
        assert page.current == "main", "main, the default board, though another sorts first"
        assert page.query_one(BoardRow).render_line(0).text.startswith("  incident-db 1   ◆ main 1")


async def test_arrows_and_tab_move_between_panels() -> None:
    app = Host(FakeRunner())
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        page.set_panels([spec(c, order=k) for k, c in enumerate("abcd")])  # three smalls, then one stretched
        await until(pilot, lambda: isinstance(app.focused, PanelView))
        views = page.views()
        assert app.focused is views[0]
        await pilot.press("right")
        assert app.focused is views[1]
        await pilot.press("down")
        assert app.focused is views[3], "down: the panel below"
        await pilot.press("up")
        assert app.focused is views[0], "up: the one it overlaps most, the first on a tie"
        await pilot.press("up", "left")
        assert app.focused is views[0], "nothing above, nothing before"
        await pilot.press("tab", "tab")
        assert app.focused is views[2]
        await pilot.press("shift+tab")
        assert app.focused is views[1]


async def test_update_snapshot_reads_the_payload() -> None:
    runner = FakeRunner()
    app = Host(runner)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        panels = (spec("a"), spec("b", order=1))
        page.update_snapshot(Snapshot(taken_at=T0, payloads={"BD": panels}))
        await pilot.pause()
        assert ids(page) == [p.id for p in panels]
        page.update_snapshot(Snapshot(taken_at=T0, payloads={}))
        page.update_snapshot(Snapshot(taken_at=T0, payloads={"BD": panels}))
        await pilot.pause()
        assert ids(page) == [p.id for p in panels], "no payload: nothing changes"


# -- keys ------------------------------------------------------------------------------------------------------


async def test_x_then_y_removes_and_n_keeps() -> None:
    removed: list[PanelSpec] = []
    app = Host(FakeRunner(), remove=removed.append)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        a, b = spec("a"), spec("b", order=1)
        page.set_panels([a, b])
        await until(pilot, lambda: isinstance(app.focused, PanelView))
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmRemove)
        assert 'Remove "panel a"? y / n' in str(app.screen.query_one("#confirm-text").render())
        await pilot.press("n")
        await pilot.pause()
        assert removed == [] and not isinstance(app.screen, ConfirmRemove) and ids(page) == [a.id, b.id]
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert removed == [], "esc keeps it too"
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert removed == [a], "y removes the focused panel"
        assert ids(page) == [b.id], "it leaves the page at once"
        page.set_panels([a, b])  # a fetch that read the folder before the removal
        await pilot.pause()
        assert ids(page) == [b.id], "and doesn't come back from a stale fetch"
        page.set_panels([b])
        await pilot.pause()
        assert a.id not in page._removed, "once the store agrees, the page forgets it"
        assert app.focused is page.views()[0]
        assert list(app.errors) == []


async def test_remove_that_fails_is_reported_and_the_panel_stays() -> None:
    def broken(panel: PanelSpec) -> None:
        raise PermissionError("read-only folder")

    app = Host(FakeRunner(), remove=broken)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        page.set_panels([spec("a")])
        await until(pilot, lambda: isinstance(app.focused, PanelView))
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert ids(page) == [spec("a").id]
        assert any("read-only folder" in report.message for report in app.errors)


async def test_enter_opens_the_full_view_and_esc_closes_it() -> None:
    runner = FakeRunner()
    app = Host(runner)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        panel = spec("a", kind="gauge", unit="%", added_by="infra", expires_at=T0 + timedelta(hours=2))
        page.set_panels([panel])
        await until(pilot, lambda: page.runs[panel.id].frame is not None and isinstance(app.focused, PanelView))
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PanelScreen) and page.full is screen
        info = screen.query_one(PanelInfo)
        assert info.render_line(0).text.startswith("$ echo a "), "its command"
        assert "last run" in info.render_line(1).text and "every 1h" in info.render_line(1).text
        assert screen.view.frame is not None and screen.view.size.width > 80, "a big version"
        runner.values[panel.id] = 77.0
        await pilot.press("r")  # run it now, from the full view
        await until(pilot, lambda: screen.view.frame is not None and screen.view.frame.series[0].last == 77.0)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, PanelScreen) and page.full is None


async def test_the_full_view_closes_when_its_panel_goes() -> None:
    app = Host(FakeRunner())
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        page.set_panels([spec("a"), spec("b", order=1)])
        await until(pilot, lambda: isinstance(app.focused, PanelView))
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PanelScreen)
        page.set_panels([spec("b", order=1)])
        await pilot.pause()
        assert not isinstance(app.screen, PanelScreen)


# -- arranging -------------------------------------------------------------------------------------------------


async def test_plus_and_minus_resize_the_focused_panel_at_once_and_save_it() -> None:
    runner, arranged = FakeRunner(), Arranged()
    app = Host(runner, arrange=arranged)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        a, b, c = spec("a"), spec("b", order=1), spec("c", kind="timeseries", order=2)
        page.set_panels([a, b, c])
        await until(pilot, lambda: all(page.runs[p.id].frame is not None for p in (a, b, c)))
        await until(pilot, lambda: isinstance(app.focused, PanelView))
        assert rows_of(page) == [[45, 45], [90]]
        first = page.views()
        await pilot.press("plus")
        await pilot.pause()
        assert arranged.calls == [(a.id, "medium", None)], "auto stat: small, so one up is medium"
        assert rows_of(page) == [[52, 38], [90]], "laid out again at once: medium 6 + small 4, stretched to 7 + 5"
        await pilot.press("plus")
        await pilot.pause()
        assert arranged.calls[-1] == (a.id, "large", None)
        assert rows_of(page) == [[90], [37, 53]], "large: a row of its own (then small 4 + medium 6 stretched to 5 + 7)"
        await pilot.press("plus")
        await pilot.pause()
        assert len(arranged.calls) == 2, "large is the largest: nothing to save"
        await pilot.press("minus", "minus", "minus")
        await pilot.pause()
        assert [call[1] for call in arranged.calls[2:]] == ["medium", "small"], "and back down to small"
        assert rows_of(page) == [[45, 45], [90]]
        assert page.views() == first and app.focused is first[0], "the same widgets, the focus kept"
        assert page.runs[a.id].spec.size == "small" and page.runs[a.id].spec.changed_at == a.changed_at
        assert [runner.count(p.id) for p in (a, b, c)] == [1, 1, 1], "arranging never runs a panel"
        assert list(app.errors) == []


async def test_less_and_greater_move_the_focused_panel_and_save_its_position() -> None:
    runner, arranged = FakeRunner(), Arranged()
    app = Host(runner, arrange=arranged)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        a, b, c = spec("a"), spec("b", order=1), spec("c", order=2)
        page.set_panels([a, b, c])
        await until(pilot, lambda: isinstance(app.focused, PanelView))
        await pilot.press("right")
        await pilot.press("less_than_sign")
        await pilot.pause()
        assert arranged.calls == [(b.id, None, T1 - 1.0)], "before the first: a little earlier than it"
        assert ids(page) == [b.id, a.id, c.id], "moved at once"
        assert cast(PanelView, app.focused).spec.id == b.id, "the focus moves with it"
        await pilot.press("less_than_sign")
        assert len(arranged.calls) == 1, "already first"
        await pilot.press("greater_than_sign", "greater_than_sign")
        await pilot.pause()
        assert arranged.calls[1:] == [(b.id, None, T1 + 1.0), (b.id, None, T1 + 3.0)], "between, then after the last"
        assert ids(page) == [a.id, c.id, b.id]
        assert page.board_panels() == sorted(page.board_panels(), key=lambda p: p.order_key)
        assert [runner.count(p.id) for p in (a, b, c)] == [1, 1, 1], "arranging never runs a panel"


async def test_a_folder_read_that_only_arranges_keeps_the_runs_and_the_history() -> None:
    runner = FakeRunner()
    app = Host(runner)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        a, b = spec("a"), spec("b", order=1)
        page.set_panels([a, b])
        await until(pilot, lambda: all(newest(page, p.id) == 42.0 for p in (a, b)))
        await until(pilot, lambda: isinstance(app.focused, PanelView))
        runner.values[a.id] = 7.0
        await pilot.press("r")
        await until(pilot, lambda: newest(page, a.id) == 7.0)
        history = page.runs[a.id].frame
        views = page.views()
        assert rows_of(page) == [[45, 45]]
        moved = replace(a, size="large", position=T1 + 10)  # another viewer arranged it: same changed_at
        page.set_panels([b, moved])
        await pilot.pause()
        await pilot.pause()
        assert ids(page) == [b.id, a.id] and rows_of(page) == [[90], [90]], "the new order and size, at once"
        assert set(page.views()) == set(views), "the same widgets, moved"
        assert page.runs[a.id].frame is history, "the history stays"
        assert runner.count(a.id) == 2 and runner.count(b.id) == 1, "and nothing ran again"
        assert page.views()[1].spec.size == "large"


async def test_a_folder_read_from_before_the_write_does_not_undo_an_arrangement(
        monkeypatch: pytest.MonkeyPatch) -> None:
    arranged = Arranged()
    app = Host(FakeRunner(), arrange=arranged)
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        a, b = spec("a"), spec("b", order=1)
        page.set_panels([a, b])
        await until(pilot, lambda: isinstance(app.focused, PanelView))
        await pilot.press("plus")
        await pilot.pause()
        page.set_panels([a, b])  # read before the arrange landed
        await pilot.pause()
        assert page.runs[a.id].spec.size == "medium", "the page keeps what it saved"
        page.set_panels([replace(a, size="medium"), b])  # the store agrees
        await pilot.pause()
        assert page._arranged == {}
        page.set_panels([replace(a, size="large"), b])  # another viewer, later
        await pilot.pause()
        assert page.runs[a.id].spec.size == "large", "once the store agreed, it wins again"
        monkeypatch.setattr(BoardsPage, "ARRANGE_GRACE", 0.0)
        await pilot.press("minus")
        page.set_panels([replace(a, size="large"), b])
        await pilot.pause()
        assert page.runs[a.id].spec.size == "large", "an arrangement the store never shows doesn't stick"


async def test_arranging_that_fails_is_reported_and_nothing_moves() -> None:
    app = Host(FakeRunner(), arrange=Arranged(fail="read-only folder"))
    async with app.run_test(size=(90, 28)) as pilot:
        page = page_of(app)
        page.set_panels([spec("a"), spec("b", order=1)])
        await until(pilot, lambda: isinstance(app.focused, PanelView))
        await pilot.press("plus", "greater_than_sign")
        await pilot.pause()
        assert rows_of(page) == [[45, 45]] and ids(page) == [spec("a").id, spec("b").id]
        assert any("read-only folder" in report.message for report in app.errors)
    plain = Host(FakeRunner())  # no arrange: the keys say so and change nothing
    async with plain.run_test(size=(90, 28)) as pilot:
        page = page_of(plain)
        page.set_panels([spec("a"), spec("b", order=1)])
        await until(pilot, lambda: isinstance(plain.focused, PanelView))
        await pilot.press("plus", "less_than_sign")
        await pilot.pause()
        assert rows_of(page) == [[45, 45]] and list(plain.errors) == []


# -- with glass ------------------------------------------------------------------------------------------------


async def test_with_glass_every_kind_draws_and_an_idle_page_sends_nothing(output: Output) -> None:
    runner = FakeRunner()
    app = Host(runner)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        page = page_of(app)
        series = Frame(series=(Series("api", tuple((T1 - 60 * k, 100.0 + k) for k in range(15, -1, -1))),), at=T1)
        panels = [spec("a", kind="timeseries", data={"x": 1}), spec("b", kind="gauge", order=1),
                  spec("c", kind="bars", order=2), spec("d", board="ops")]

        def run(panel: PanelSpec) -> Frame:
            if panel.kind == "timeseries":
                return series
            if panel.kind == "bars":
                return Frame(series=(Series("x", ((T1, 3.0),)), Series("y", ((T1, 5.0),))), at=T1)
            return runner(panel)

        page.runner = run
        page.set_panels(panels)
        await until(pilot, lambda: all(page.runs[p.id].frame is not None for p in panels))
        await settle(app, pilot)
        layer = app.glass.layers[MAIN]
        items = [*layer.dynamic, *layer.static]
        assert any(isinstance(i, Lines) for i in items), "the chart's lines"
        assert any(isinstance(i, Ring) for i in items), "the gauge's ring"
        assert sum(isinstance(i, Meter) for i in items) == 2, "a meter per bar"
        assert any(isinstance(i, Pill) and i.ident.startswith("board:") for i in items), "the current board's pill"
        mark = output.mark()
        for _ in range(3):  # runs that bring the same data
            page.runs[panels[1].id].running = False
            page._ran(panels[1].id, panels[1].changed_at, page.runs[panels[1].id].frame, None)
            page._ran(panels[0].id, panels[0].changed_at,
                      Frame(series=tuple(Series(s.name, tuple((t, float(v)) for t, v in s.points))
                                         for s in series.series), at=T1 + 99), None)
            await settle(app, pilot)
        assert output.since(mark) == "", "equal data: not a byte"
        await pilot.press("right_square_bracket")
        await settle(app, pilot)
        assert not any(isinstance(i, Lines) for i in app.glass.layers[MAIN].dynamic), "the other board's panels"
    model = output.model()
    assert model.errors == [] and list(app.errors) == []
