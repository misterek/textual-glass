"""``OrbitalApp`` headless, without glass, over fake sources: fetch deadlines and
hung threads, the poll schedule and backoff, guarded publishing and error
reports, closing resolved alerts, re-stamped snapshots, the minute tick and
opening links. Real timers and threads, with intervals of a quarter second."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from textual.app import ComposeResult

import textual_orbital.app as orbital_app
from textual_orbital import (
    AlertScreen,
    AttentionItem,
    Condition,
    Entity,
    EntityState,
    ErrorReport,
    HeaderStrip,
    Health,
    OrbitalApp,
    Page,
    Snapshot,
    SourceData,
    SourcesScreen,
    SourceStatus,
    StaticProvider,
    WaitingRule,
    now,
)

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


async def until(condition: Callable[[], object], timeout: float = 5.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or condition}")
        await asyncio.sleep(0.02)


class Gate:
    """A fetch that blocks until ``release`` is set: a socket that died while the
    laptop slept. Fetch ``n`` returns the attention item ``HG:n``."""

    def __init__(self, key: str = "HG", timeout: float = 0.3) -> None:
        self.key, self.name, self.interval, self.timeout = key, key, 0.25, timeout
        self.release = threading.Event()
        self.calls = 0
        self.threads: list[threading.Thread] = []

    def fetch(self) -> SourceData:
        self.calls += 1
        n = self.calls
        self.threads.append(threading.current_thread())
        self.release.wait(10)  # bounded, so a failing test leaves no thread behind for long
        return SourceData(attention=[AttentionItem(f"{self.key}:{n}", self.key, 5, "review", f"fetch {n}")])


class Quick:
    """A source that answers at once, with ``data`` or by raising ``error``; it
    records when each fetch started and ended."""

    def __init__(self, key: str, data: SourceData | None = None, interval: float = 0.25,
                 delay: float = 0.0) -> None:
        self.key, self.name, self.interval = key, key, interval
        self.data = data or SourceData(attention=[AttentionItem(f"{key}:1", key, 5, "review", key)])
        self.error: BaseException | None = None
        self.delay = delay
        self.starts: list[float] = []
        self.ends: list[float] = []

    @property
    def calls(self) -> int:
        return len(self.starts)

    def fetch(self) -> SourceData:
        self.starts.append(time.monotonic())
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return self.data
        finally:
            self.ends.append(time.monotonic())


class Board(OrbitalApp):
    """Records what the pages would get."""

    def __init__(self, *sources: Any, **kwargs: Any) -> None:
        kwargs.setdefault("glass", False)
        kwargs.setdefault("notify", "off")
        kwargs.setdefault("rules", ())
        super().__init__(StaticProvider(list(sources)), **kwargs)
        self.delivered: list[Snapshot] = []

    def snapshot_changed(self, snapshot: Snapshot) -> None:
        self.delivered.append(snapshot)
        super().snapshot_changed(snapshot)


# -- deadlines and hung fetches (O1, O2, O6) -----------------------------------------------


async def test_a_hung_fetch_times_out_and_never_stops_the_other_sources() -> None:
    gate = Gate()
    quick = [Quick(f"Q{i}") for i in range(5)]  # more than the old pool's four workers
    app = Board(gate, *quick)
    async with app.run_test():
        await until(lambda: app.store.status("HG").error, what="the deadline")
        status = app.store.status("HG")
        assert status.error == "timed out after 0.3 s" and status.health(now()) is Health.FAILING
        assert status.failures == 1 and status.next_poll is not None
        assert gate.threads and all(t.daemon for t in gate.threads), "quitting never waits for a fetch"
        await until(lambda: all(q.calls >= 3 for q in quick), what="the other sources")
        assert all(app.store.status(q.key).health(now()) is Health.FRESH for q in quick)
        gate.release.set()  # the hung fetch returns, late; the next one returns at once
        await until(lambda: app.store.status("HG").error is None, what="a good fetch")
        delivered = {i.key for s in app.delivered for i in s.attention}
        assert "HG:1" not in delivered, "the late result was ignored"
        assert app.snapshot is not None and app.snapshot.item(f"HG:{gate.calls}") is not None
        assert app.store.status("HG").failures == 0
        assert not app.errors


async def test_at_the_hung_limit_a_source_waits_for_a_fetch_to_return() -> None:
    gate = Gate(timeout=0.2)
    app = Board(gate)
    async with app.run_test():
        await until(lambda: "paused" in (app.store.status("HG").error or ""), what="two timeouts")
        status = app.store.status("HG")
        assert status.error == "timed out after 0.2 s; 2 fetches hung, paused until one returns"
        assert status.next_poll is None and gate.calls == app.HUNG_LIMIT == 2
        await asyncio.sleep(0.8)
        app.poll_all()
        await asyncio.sleep(0.2)
        assert gate.calls == 2, "no third thread, not even for poll_all"
        gate.release.set()
        await until(lambda: app.store.status("HG").error is None, what="polling again")
        assert gate.calls == 3 and app.snapshot is not None and app.snapshot.item("HG:3") is not None


async def test_a_fetch_that_raises_system_exit_fails_and_polls_again() -> None:
    exits = Quick("EX")
    exits.error = SystemExit(2)
    app = Board(exits)
    async with app.run_test():
        await until(lambda: exits.calls >= 2, what="a second fetch")
        status = app.store.status("EX")
        assert status.error == "SystemExit: 2" and status.failures >= 1


async def test_the_fetch_thread_reports_back_even_if_it_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    def dies(source: Any) -> Any:
        raise KeyboardInterrupt  # what timed_fetch raises again on the main thread; here, anything that escapes

    escaped: list[type[BaseException] | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: escaped.append(args.exc_type))
    monkeypatch.setattr(orbital_app, "timed_fetch", dies)
    app = Board(Quick("QK", interval=60))
    async with app.run_test():
        await until(lambda: app.store.status("QK").error, what="a report")
        assert app.store.status("QK").error == "the fetch thread stopped"
        assert app.store.status("QK").next_poll is not None, "and the source is polled again"
        await until(lambda: escaped, what="the thread to end")
        assert escaped == [KeyboardInterrupt]


# -- the poll schedule and backoff (O8) ------------------------------------------------------


async def test_the_next_poll_starts_an_interval_after_the_fetch_ends() -> None:
    slow = Quick("SL", delay=0.3)  # longer than its interval
    app = Board(slow)
    async with app.run_test():
        await until(lambda: slow.calls >= 3, what="three fetches")
        gaps = [start - end for start, end in zip(slow.starts[1:], slow.ends, strict=False)]
        assert all(gap >= 0.2 for gap in gaps), gaps


async def test_a_failing_source_backs_off_and_poll_all_skips_the_wait() -> None:
    flaky = Quick("FL")
    flaky.error = RuntimeError("HTTP 503")
    app = Board(flaky)
    async with app.run_test():
        await until(lambda: flaky.calls >= 3, what="three failures")
        gaps = [b - a for a, b in zip(flaky.starts, flaky.starts[1:], strict=False)]
        assert gaps[0] >= 0.45 and gaps[1] >= 0.95, f"0.25 s doubled for each failure: {gaps}"
        status = app.store.status("FL")
        assert status.failures == 3 and status.error == "HTTP 503"
        assert status.last_attempt is not None and status.next_poll == status.last_attempt + timedelta(seconds=2)
        app.poll_all()  # r: now, whatever the backoff
        await until(lambda: app.store.status("FL").failures == 4, timeout=0.5, what="poll_all")
        flaky.error = None
        app.poll_all()
        await until(lambda: app.store.status("FL").error is None, timeout=1.0, what="a good fetch")
        status = app.store.status("FL")
        assert status.failures == 0 and status.last_ok is not None
        assert status.next_poll == status.last_ok + timedelta(seconds=0.25), "back to its interval"


# -- errors are reported, and one step doesn't skip the others (O3, rule 22) ----------------


class Broken(Page):
    def update_snapshot(self, snapshot: Snapshot) -> None:
        raise ValueError("page bug [not markup]")


class Fine(Page):
    def __init__(self) -> None:
        super().__init__()
        self.got: list[Snapshot] = []

    def update_snapshot(self, snapshot: Snapshot) -> None:
        self.got.append(snapshot)


class BadRule:
    def conditions(self, snapshot: Snapshot) -> Iterator[Condition]:
        raise TypeError("rule bug")


class Pages(Board):
    ERROR_TOAST_SPACING = 0.3

    def __init__(self, *sources: Any, **kwargs: Any) -> None:
        super().__init__(*sources, **kwargs)
        self.badges: list[str | None] = []
        self.toasts: list[str] = []

    def compose(self) -> ComposeResult:
        yield HeaderStrip("BOARD", ["one"])
        yield Broken()
        yield Fine()

    def badge_changed(self, text: str | None, waiting: int) -> None:
        self.badges.append(text)

    def notify(self, message: str, **kwargs: Any) -> None:  # type: ignore[override]
        self.toasts.append(message)


WAITING = SourceData(entities=[Entity("s1", "deploy", state=EntityState.WAITING, since=T0)])


async def test_a_failing_step_is_reported_and_the_others_still_run() -> None:
    app = Pages(Quick("CC", WAITING, interval=60), rules=(BadRule(), WaitingRule()), alerts=True)
    async with app.run_test():
        await until(lambda: app.snapshot is not None, what="a fetch")
        await until(lambda: len(app.toasts) >= 2, timeout=2, what="the second toast")
        fine = app.query_one(Fine)
        assert fine.got and fine.got[-1] is app.snapshot, "the next page still got the snapshot"
        assert app.badges[-1] == "◉ 1 waiting", "the badge still updated"
        assert app.query_one(HeaderStrip).health.statuses, "health still went to the header"
        where = {e.where: e for e in app.errors}
        assert where["Broken.update_snapshot"].message == "ValueError: page bug [not markup]"
        assert where["rules"].message == "TypeError: rule bug"
        assert app.toasts[0] == "Broken.update_snapshot: ValueError: page bug [not markup]", "shown at once"
        assert app.toasts[1] == "rules: TypeError: rule bug", "the next one after ERROR_TOAST_SPACING"
        app.poll_all()  # the same content: no page update, but the rules fail again
        await until(lambda: where["rules"] is not app.errors[-1], what="a second report")
        assert app.errors[-1].where == "rules" and app.errors[-1].count == 2, "the same error once, counted"
        assert len(app.errors) == 2


async def test_errors_from_thread_callbacks_and_the_snapshot_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    app = Pages(Quick("CC", WAITING, interval=60))
    async with app.run_test():
        await until(lambda: app.snapshot is not None, what="a fetch")

        def boom() -> None:
            raise RuntimeError("callback bug")

        threading.Thread(target=app.call_from_worker, args=(boom,)).start()
        await until(lambda: any(e.message == "RuntimeError: callback bug" for e in app.errors), what="a report")
        assert "boom" in app.errors[-1].where

        def no_snapshot(at: datetime | None = None) -> Snapshot:
            raise TypeError("can't compare offset-naive and offset-aware datetimes")

        monkeypatch.setattr(app.store, "snapshot", no_snapshot)
        badges = len(app.badges)
        app.poll_all()
        await until(lambda: app.errors[-1].where == "Store.snapshot", what="a report")
        await until(lambda: len(app.badges) > badges, what="the badge")


# -- closing resolved alerts (O10) -----------------------------------------------------------


def sessions(*waiting: str) -> SourceData:
    return SourceData(entities=[Entity(n, n, state=EntityState.WAITING if n in waiting else EntityState.WORKING,
                                       since=T0) for n in ("a", "b")])


async def test_a_resolved_alert_under_another_popup_closes_when_it_is_on_top_again() -> None:
    cc = Quick("CC", sessions("a"), interval=60)
    app = Board(cc, rules=(WaitingRule(),))
    async with app.run_test() as pilot:
        await until(lambda: isinstance(app.screen, AlertScreen), what="the alert")
        old = app.screen
        assert isinstance(old, AlertScreen)
        app.push_screen(SourcesScreen(app.store.statuses()))  # h, over the alert
        await pilot.pause()
        cc.data = sessions("b")  # a was answered elsewhere, and b waits now: in one fetch
        app.poll_all()
        await until(lambda: isinstance(app.screen, AlertScreen) and app.screen is not old, what="b's alert")
        assert isinstance(app.screen, AlertScreen) and app.screen.alert.title == "b", "this cycle's alert shows"
        stack = app.screen_stack
        assert [type(s) for s in stack[1:]] == [AlertScreen, SourcesScreen, AlertScreen], "nothing else was popped"
        assert old.resolved and app.alert_queue.active is not None and app.alert_queue.active.title == "b"
        await pilot.press("escape")  # b's alert
        await pilot.pause()
        assert isinstance(app.screen, SourcesScreen)
        await pilot.press("escape")  # the sources: a's resolved alert is on top again, and closes itself
        await until(lambda: len(app.screen_stack) == 1, what="the resolved alert closing")
        assert app.alert_queue.active is None and not app.errors


async def test_a_second_publish_never_closes_a_resolved_alert_twice() -> None:
    cc = Quick("CC", sessions("a"), interval=60)
    app = Board(cc, rules=(WaitingRule(),))
    async with app.run_test() as pilot:
        await until(lambda: isinstance(app.screen, AlertScreen), what="the alert")
        assert app.store.apply("CC", sessions(), now())
        app._publish(now())
        app._publish(now())  # before the dismiss callback has run
        await pilot.pause()
        await pilot.pause()
        assert len(app.screen_stack) == 1 and app.alert_queue.active is None and not app.errors


# -- snoozes that survive a restart (O7) -----------------------------------------------------


WAITS = SourceData(entities=[Entity("s1", "deploy", state=EntityState.WAITING, since=T0)],
                   attention=[AttentionItem("CC:s1", "CC", 10, "respond", "deploy", since=T0),
                              AttentionItem("CC:pr", "CC", 5, "review", "a pull request", since=T0)])


async def test_snoozes_survive_a_restart_and_a_snoozed_wait_stays_quiet_at_launch(tmp_path: Path) -> None:
    path = tmp_path / "state" / "snoozes.json"  # its directory is made on the first write
    app = Board(Quick("CC", WAITS, interval=60), rules=(WaitingRule(),), snooze_file=path)
    async with app.run_test() as pilot:
        await until(lambda: isinstance(app.screen, AlertScreen), what="the waiting alert")
        assert not path.exists(), "nothing is written until something is snoozed"
        await pilot.press("s")  # snooze the alert for ALERT_SNOOZE
        await until(lambda: app.alert_queue.snoozed, what="the alert snooze")
        app.snooze_item("CC:pr")
        saved = json.loads(path.read_text())
        (alert_key,) = saved["alerts"]
        assert alert_key.startswith("waiting:s1:") and list(saved["items"]) == ["CC:pr"]
        assert datetime.fromisoformat(saved["items"]["CC:pr"]) == app.snoozed["CC:pr"]
        assert [p.name for p in path.parent.iterdir()] == ["snoozes.json"], "no temporary file left"

    again = Board(Quick("CC", WAITS, interval=60), rules=(WaitingRule(),), snooze_file=path)
    async with again.run_test() as pilot:
        await until(lambda: again.snapshot is not None, what="a fetch")
        await pilot.pause()
        assert not isinstance(again.screen, AlertScreen), "a snoozed wait doesn't pop up again at launch"
        assert set(again.snoozed) == {"CC:pr"} and set(again.alert_queue.snoozed) == {alert_key}
        assert again.unsnooze("CC:pr") and not again.unsnooze("CC:pr")
        assert json.loads(path.read_text())["items"] == {}, "saved after unsnooze"
        again._wake_alerts(now() + again.ALERT_SNOOZE + timedelta(minutes=1))  # the snooze ends
        await until(lambda: isinstance(again.screen, AlertScreen), what="the alert, back")
        await pilot.pause()
        assert again.screen.alert.key == alert_key and json.loads(path.read_text())["alerts"] == {}
        assert not again.errors


async def test_snoozes_that_ended_while_closed_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "snoozes.json"
    at = now()
    path.write_text(json.dumps({"items": {"CC:s1": (at - timedelta(minutes=1)).isoformat(),
                                          "CC:pr": (at + timedelta(minutes=30)).isoformat()},
                                "alerts": {"gone": (at - timedelta(hours=1)).isoformat()}}))
    app = Board(Quick("CC", WAITS, interval=60), snooze_file=path)
    async with app.run_test():
        await until(lambda: app.snapshot is not None, what="a fetch")
        assert list(app.snoozed) == ["CC:pr"] and not app.alert_queue.snoozed
        assert [i.key for i in app.visible_items(app.snapshot.attention)] == ["CC:s1"]
        saved = json.loads(path.read_text())
        assert list(saved["items"]) == ["CC:pr"] and saved["alerts"] == {}, "rewritten without them"
        assert not app.errors


@pytest.mark.parametrize("content, message", [
    ("{not json", "JSONDecodeError"),
    ('["a list"]', "ValueError: not a JSON object"),
    ('{"items": {"CC:pr": "tomorrow"}}', "ValueError: Invalid isoformat string"),
    ('{"items": {"CC:pr": "2026-10-28T12:00:00"}}', "ValueError: items 'CC:pr': a time without an offset"),
    ('{"alerts": {"k": 5}}', "TypeError"),
    (None, "IsADirectoryError"),  # a directory where the file should be
])
async def test_a_broken_snooze_file_is_reported_and_never_stops_startup(tmp_path: Path, content: str | None,
                                                                        message: str) -> None:
    path = tmp_path / "snoozes.json"
    if content is None:
        path.mkdir()
    else:
        path.write_text(content)
    app = Board(Quick("CC", WAITS, interval=60), rules=(WaitingRule(),), snooze_file=path)
    async with app.run_test() as pilot:
        await until(lambda: isinstance(app.screen, AlertScreen), what="startup, with its alert")
        await pilot.pause()
        (report,) = app.errors
        assert report.where == f"snooze file {path}" and report.message.startswith(message), report
        assert not app.snoozed and not app.alert_queue.snoozed, "treated as empty"
        if content is not None:
            assert path.read_text() == content, "a broken file is left alone until something changes"
            app.snooze_item("CC:pr")
            assert list(json.loads(path.read_text())["items"]) == ["CC:pr"]


# -- the snapshot's time (O5) and the minute tick --------------------------------------------


async def test_snoozing_republishes_the_snapshot_taken_now(monkeypatch: pytest.MonkeyPatch) -> None:
    app = Board(Quick("QK", interval=60))
    async with app.run_test():
        await until(lambda: app.snapshot is not None, what="a fetch")
        first = app.snapshot
        assert first is not None
        clock = [first.taken_at + timedelta(minutes=30)]
        monkeypatch.setattr(orbital_app, "now", lambda: clock[0])
        app.snooze_item("QK:1")
        assert app.delivered[-1].taken_at == clock[0] == app.snapshot.taken_at, "ages from now, not the last change"
        clock[0] += timedelta(minutes=5)
        app.wake_items()
        assert app.delivered[-1].taken_at == clock[0]
        app.snooze_item("QK:1", timedelta(minutes=1))
        clock[0] += timedelta(minutes=2)
        app.snooze_expire(clock[0])
        assert app.delivered[-1].taken_at == clock[0] and not app.snoozed
        assert app.store.content_version == 1, "snoozing is not a content change"


async def test_the_minute_tick_republishes_on_a_new_day(monkeypatch: pytest.MonkeyPatch) -> None:
    sunday = datetime(2026, 10, 18, 23, 59, 30).astimezone()  # local time, in a week without a DST change
    monday = datetime(2026, 10, 19, 0, 0, 30).astimezone()
    clock = [sunday]
    monkeypatch.setattr(orbital_app, "now", lambda: clock[0])
    app = Board(Quick("QK", interval=60))
    async with app.run_test():
        await until(lambda: app.snapshot is not None, what="a fetch")
        assert app.delivered[-1].week().start == sunday.date() - timedelta(days=6)
        count = len(app.delivered)
        app._on_minute()
        assert len(app.delivered) == count, "the same day: nothing to deliver"
        clock[0] = monday
        app._on_minute()
        assert len(app.delivered) == count + 1 and app.delivered[-1].taken_at == monday
        assert app.delivered[-1].week().start == monday.date(), "the week rolled over without new data"


async def test_a_raising_minute_tick_still_arms_the_next_minute() -> None:
    class Ticks(Board):
        def minute_tick(self, at: datetime) -> None:
            raise RuntimeError("tick bug")

    app = Ticks(Quick("QK", interval=60))
    async with app.run_test():
        armed = app._orbital_minute
        app._on_minute()
        assert app._orbital_minute is not None and app._orbital_minute is not armed, "re-armed"
        assert app.errors[-1].where == "minute_tick" and app.errors[-1].message == "RuntimeError: tick bug"


# -- links (O14) -------------------------------------------------------------------------------


async def test_open_link_runs_off_the_ui_thread_and_honours_open_hosts(
        no_browser: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The one test of the opening path itself: ``webbrowser.open`` is a recorder
    (the autouse ``no_browser`` fixture, and again here), so ``open_urls=True`` is safe."""
    import webbrowser

    threads: list[str] = []

    def record(url: str, *args: object, **kwargs: object) -> bool:
        threads.append(threading.current_thread().name)
        no_browser.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", record)
    app = Board(Quick("QK", interval=60), open_urls=True, open_hosts={"GitHub.com"})
    async with app.run_test():
        app.open_link("https://github.com/example-org/web/pull/1")
        await until(lambda: no_browser, what="the browser")
        assert threads == ["orbital-browser"], "not on the UI thread"
        app.open_link("https://evil.example.com/x")
        app.open_link("claude://session/a")
        await asyncio.sleep(0.2)
        assert no_browser == ["https://github.com/example-org/web/pull/1"], "only allowed http(s) hosts"
    anywhere = Board(Quick("QK", interval=60), open_urls=True)
    async with anywhere.run_test():
        anywhere.open_link("https://example.com/y")
        await until(lambda: len(no_browser) == 2, what="the browser")


# -- the health screen shows backoff and errors -------------------------------------------


async def test_the_sources_screen_shows_the_next_try_and_the_errors() -> None:
    at = now()
    failing = SourceStatus("GH", "GitHub", 60, last_ok=at - timedelta(hours=1), last_attempt=at, error="HTTP 503",
                           failures=3, next_poll=at + timedelta(minutes=4, seconds=10))
    errors = [ErrorReport(at, "rules", "TypeError: rule bug", 3), ErrorReport(at, "Page.update_snapshot", "x")]
    app = Board(Quick("QK", interval=60))
    async with app.run_test(size=(90, 28)) as pilot:
        app.push_screen(SourcesScreen([failing], at, errors=errors))
        await pilot.pause()
        text = " ".join(str(w.render()) for w in app.screen.query("Static"))
        assert "HTTP 503  · 3 failures, next try in 4m" in text
        assert "APP ERRORS (2)" in text and "rules  TypeError: rule bug  ×3" in text
        assert text.index("Page.update_snapshot") < text.index("rules"), "newest first"
