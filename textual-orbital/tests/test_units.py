"""Unit tests: the generic model, the store, alerts, notifications and the theme.
Fast; only the theme test starts a (headless) Textual app."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from textual.app import App
from textual.theme import Theme

from textual_orbital import notify
from textual_orbital.alerts import AlertQueue, AlertRules, Condition, WaitingRule
from textual_orbital.model import (
    Alert,
    AttentionItem,
    Entity,
    EntityState,
    Event,
    Health,
    Snapshot,
    SourceStatus,
    TrackHealth,
    TrackItem,
    queue_load,
    week_stats,
)
from textual_orbital.notify import Notifier, notification_mode, osc9, osc777, set_title, tmux_passthrough
from textual_orbital.provider import SourceData, StaticProvider, Store, timed_fetch
from textual_orbital.theme import (
    ORBITAL,
    ORBITAL_NAVY,
    PALETTE,
    fmt_age,
    fmt_interval,
    source_color,
    urgency_color,
    why_color,
)

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class PullRequest:
    """An app's own record, carried in ``data``."""

    number: int
    checks: str


# ------------------------------------------------------------------- model --


def test_source_health() -> None:
    s = SourceStatus("GH", "GitHub", 60)
    assert s.health(T0) is Health.PENDING
    assert replace(s, last_ok=T0).health(T0 + timedelta(seconds=30)) is Health.FRESH
    assert replace(s, last_ok=T0).health(T0 + timedelta(minutes=10)) is Health.STALE
    assert replace(s, last_ok=T0, error="boom").health(T0) is Health.FAILING


def test_week_stats_counts_this_week_against_the_same_days_last_week() -> None:
    today = date(2026, 10, 27)  # a Tuesday
    tz = timezone.utc

    def ev(d: date, kind: str = "merged") -> Event:
        return Event(datetime(d.year, d.month, d.day, 12, tzinfo=tz).astimezone(), kind, "GH", "x")

    events = [ev(date(2026, 10, 26)), ev(date(2026, 10, 27)), ev(date(2026, 10, 19)), ev(date(2026, 10, 22)),
              ev(date(2026, 10, 27), "deployed"), ev(date(2026, 10, 1))]
    week = week_stats(events, today)
    assert week.start == date(2026, 10, 26) and len(week.days) == 5 and week.elapsed == 2
    assert week.series("merged") == (1, 1, 0, 0, 0)
    assert week.previous_series("merged") == (1, 0, 0, 1, 0)
    assert week.delta("merged") == 1  # 2 this week vs 1 by Tuesday last week
    assert week.total("deployed") == 1 and week.total("nothing") == 0
    assert len(week.events) == 3 and week.events[0].when >= week.events[-1].when, "this week's, newest first"
    assert hash(week) == hash(week_stats(events, today))


def test_track_item_stuck() -> None:
    item = TrackItem("r", "GH", "release", stage=2, stage_since=T0 - timedelta(hours=5))
    assert not item.stuck(T0)
    assert replace(item, health=TrackHealth.FAIL).stuck(T0), "failing is always stuck"
    assert replace(item, stage_since=T0 - timedelta(days=3)).stuck(T0)
    assert replace(item, stage_since=T0 - timedelta(days=3)).stuck(T0, after=timedelta(days=5)) is False
    assert not replace(item, health=TrackHealth.IDLE, stage_since=T0 - timedelta(days=30)).stuck(T0), "parked"
    assert replace(item, stage_since=None, opened_at=T0 - timedelta(days=3)).stuck(T0), "opened_at as fallback"
    assert not replace(item, stage_since=None).stuck(T0), "no timestamps: not stuck"


def test_data_takes_part_in_equality() -> None:
    running, passing = PullRequest(412, "running"), PullRequest(412, "pass")
    records: list[Any] = [Entity("e", "deploy", data=running), AttentionItem("a", "GH", 5, "review", "x", data=running),
                          TrackItem("t", "GH", "x", data=running), Event(T0, "merged", "GH", "x", data=running)]
    for record in records:
        assert record == replace(record, data=PullRequest(412, "running"))
        assert record != replace(record, data=passing), type(record).__name__
        assert len({record, replace(record, data=passing)}) == 2, "still hashable"

    item = TrackItem("t", "GH", "x", data=running)
    src = Src("GH", SourceData(track=[item]))
    store = Store([src])
    store.apply("GH", src.fetch(), T0)
    v = store.content_version
    src.data = SourceData(track=[replace(item, data=passing)])
    store.apply("GH", src.fetch(), T0)
    assert store.content_version == v + 1, "a change only in data is a content change"
    t = store.snapshot(T0).track_item("t")
    assert t is not None and cast(PullRequest, t.data).checks == "pass"


def test_snapshot_lookups() -> None:
    snap = Snapshot(T0, entities=(Entity("e1", "a"), Entity("e2", "b")),
                    attention=(AttentionItem("q", "GH", 5, "review", "x", ref=("track", "t1")),
                               AttentionItem("w", "GH", 2, "fyi", "y", section="aware")),
                    track=(TrackItem("t1", "GH", "x"),))
    assert snap.entity("e2") == Entity("e2", "b") and snap.entity("nope") is None
    assert snap.track_item("t1") is not None and snap.item("w") is not None and snap.item("nope") is None
    assert [i.key for i in snap.needs_attention] == ["q"] and [i.key for i in snap.be_aware] == ["w"]
    assert snap.week().start == date(2026, 10, 26)


def test_queue_load() -> None:
    snap = Snapshot(T0, attention=(AttentionItem("a", "GH", 8, "review", "x"), AttentionItem("b", "CC", 10, "w", "y"),
                                   AttentionItem("c", "GH", 9, "fyi", "z", section="aware")))
    assert queue_load(snap) == (18, 60), "be-aware items don't load the queue"
    assert queue_load(snap, frozenset({"b"})) == (8, 60)
    assert queue_load(snap, {"b": T0 + timedelta(hours=1)}) == (8, 60), "the app's snooze dict works as it is"
    assert queue_load(Snapshot(T0)) == (0, 60)


# ------------------------------------------------------------------- store --


class Src:
    def __init__(self, key: str, data: SourceData | Exception) -> None:
        self.key, self.name, self.interval = key, key, 60.0
        self.data = data

    def fetch(self) -> SourceData:
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


def test_store_merges_sources_and_only_versions_real_changes() -> None:
    gh = Src("GH", SourceData(attention=[AttentionItem("g", "GH", 5, "review", "x")]))
    cc = Src("CC", SourceData(entities=[Entity("s", "s")], attention=[AttentionItem("c", "CC", 10, "w", "y")]))
    store = Store(StaticProvider([gh, cc]).sources())
    store.apply("GH", gh.fetch(), T0)
    store.apply("CC", cc.fetch(), T0)
    v = store.content_version
    snap = store.snapshot(T0)
    assert [i.key for i in snap.attention] == ["c", "g"]  # by score
    assert [e.id for e in snap.entities] == ["s"] and snap.track == ()
    store.apply("CC", cc.fetch(), T0 + timedelta(seconds=10))
    assert store.content_version == v, "the same data again is not a change"
    store.apply("CC", SourceData(entities=[]), T0 + timedelta(seconds=20))
    assert store.snapshot(T0).entities == () and len(store.snapshot(T0).attention) == 2, "None keeps, [] replaces"
    store.fail("GH", "HTTP 502", T0)
    assert store.snapshot(T0).attention, "a failing source keeps its last good data"
    assert store.statuses()[0].health(T0) is Health.FAILING


def test_store_merges_metrics_and_versions_their_changes() -> None:
    disk = Src("DK", SourceData(metrics={"disk": 0.42, "load": 1.5}))
    queue = Src("QU", SourceData(metrics={"queue": 3.0}))
    store = Store([disk, queue])
    store.apply("DK", disk.fetch(), T0)
    store.apply("QU", queue.fetch(), T0)
    snap = store.snapshot(T0)
    assert dict(snap.metrics) == {"disk": 0.42, "load": 1.5, "queue": 3.0}
    assert snap.metric("disk") == 0.42 and snap.metric("gone") == 0.0 and snap.metric("gone", -1.0) == -1.0
    assert snap == store.snapshot(T0) and hash(snap) == hash(store.snapshot(T0)), "comparable and hashable"
    with pytest.raises(TypeError):
        snap.metrics["disk"] = 1.0  # type: ignore[index]
    v = store.content_version
    store.apply("DK", SourceData(metrics={"load": 1.5, "disk": 0.42}), T0)
    store.apply("DK", SourceData(track=[]), T0)
    assert store.content_version == v and store.snapshot(T0).metric("disk") == 0.42, "same values, or None, keep"
    store.apply("DK", SourceData(metrics={"disk": 0.43}), T0)
    assert store.content_version == v + 1, "a changed value is a content change"
    assert dict(store.snapshot(T0).metrics) == {"disk": 0.43, "queue": 3.0}, "a set field replaces the source's"
    store.apply("QU", SourceData(metrics={"disk": 0.9}), T0)
    assert store.snapshot(T0).metric("disk") == 0.9, "merged in source order: the later source wins a shared name"
    store.apply("QU", SourceData(metrics={}), T0)
    assert dict(store.snapshot(T0).metrics) == {"disk": 0.43}, "{} reports none"
    assert Snapshot(T0, metrics={"x": 1.0}) == Snapshot(T0, metrics={"x": 1.0}) and hash(Snapshot(T0, metrics={}))


async def test_rules_run_after_every_fetch_and_pages_keep_the_last_change() -> None:
    """Rules see a fresh snapshot after every fetch, changed or not, taken at that fetch;
    ``app.snapshot`` (what pages get) keeps the time of the last content change."""
    import asyncio

    from textual_orbital import OrbitalApp

    seen: list[Snapshot] = []

    class Record:
        def conditions(self, snapshot: Snapshot) -> Iterator[Condition]:
            seen.append(snapshot)
            return iter(())

    async def until(count: int) -> None:
        for _ in range(100):
            if len(seen) >= count:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"{len(seen)} rule runs, not {count}")

    app = OrbitalApp(StaticProvider([Src("DK", SourceData(metrics={"disk": 0.5}))]), rules=(Record(),),
                     glass=False, notify="off")
    async with app.run_test(size=(40, 10)):
        await until(1)
        first = app.snapshot
        assert first is not None and first.metric("disk") == 0.5 and seen[0].taken_at == first.taken_at
        await asyncio.sleep(0.01)
        app.poll_all()  # the same data again
        await until(2)
        assert seen[1].taken_at > first.taken_at, "the rules ran again, on a snapshot taken at this fetch"
        assert app.snapshot is not None and app.snapshot.taken_at == first.taken_at, "no change: pages keep theirs"


def test_store_names_sources_without_one() -> None:
    nameless = Src("GH", SourceData())
    nameless.name = ""
    other = Src("TT", SourceData())
    other.name = ""
    assert [s.name for s in Store([nameless, other]).statuses()] == ["GitHub", "TT"]


def test_timed_fetch_reports_errors() -> None:
    data, error, _ = timed_fetch(Src("X", RuntimeError("gh: not logged in\nmore")))
    assert data is None and error == "gh: not logged in"
    data, error, _ = timed_fetch(Src("X", SourceData()))
    assert data is not None and error is None


class Raises:
    """A source whose fetch raises something that is not an ``Exception``."""

    key, name, interval = "X", "X", 60.0

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def fetch(self) -> SourceData:
        raise self.error


def test_timed_fetch_turns_base_exceptions_into_failures() -> None:
    """O6: reused script code that calls sys.exit() fails the source instead of
    killing the fetch before it reports back."""
    assert timed_fetch(Raises(SystemExit(3)))[:2] == (None, "SystemExit: 3")
    assert timed_fetch(Raises(SystemExit()))[:2] == (None, "SystemExit")
    assert timed_fetch(Raises(GeneratorExit()))[:2] == (None, "GeneratorExit")
    with pytest.raises(KeyboardInterrupt):
        timed_fetch(Raises(KeyboardInterrupt()))  # ctrl+c on the main thread still stops the program
    results: list[tuple[SourceData | None, str | None, float]] = []
    worker = threading.Thread(target=lambda: results.append(timed_fetch(Raises(KeyboardInterrupt()))))
    worker.start()
    worker.join(5)
    assert results and results[0][:2] == (None, "KeyboardInterrupt"), "on a worker thread it is a failure"


def test_store_counts_every_record_and_failures_in_a_row() -> None:
    src = Src("GH", SourceData(attention=[AttentionItem("g", "GH", 5, "review", "x")],
                               events=[Event(T0, "merged", "GH", "a"), Event(T0, "merged", "GH", "b")],
                               metrics={"disk": 0.5}))
    store = Store([src])
    store.apply("GH", src.fetch(), T0)
    assert store.status("GH").items == 3, "events count too; metrics are not records"
    store.fail("GH", "HTTP 502", T0)
    store.fail("GH", "HTTP 502", T0)
    assert store.status("GH").failures == 2
    store.schedule("GH", T0 + timedelta(minutes=1))
    assert store.status("GH").next_poll == T0 + timedelta(minutes=1)
    store.apply("GH", src.fetch(), T0)
    status = store.status("GH")
    assert (status.failures, status.error) == (0, None), "a good fetch resets the count"
    assert status.next_poll == T0 + timedelta(minutes=1), "only the app schedules"


def test_store_fails_a_source_with_a_naive_datetime() -> None:
    """O4: one naive timestamp used to break the attention sort for every source."""
    naive = datetime(2026, 10, 27, 12, 0)
    gh = Src("GH", SourceData(attention=[AttentionItem("g", "GH", 5, "review", "x", since=T0)]))
    store = Store([gh, Src("TT", SourceData()), Src("EV", SourceData())])
    assert store.apply("GH", gh.fetch(), T0)
    v = store.content_version
    bad = SourceData(attention=[AttentionItem("t", "TT", 5, "review", "y", since=naive)])
    assert not store.apply("TT", bad, T0)
    status = store.status("TT")
    assert status.error == "naive datetime in AttentionItem.since" and status.health(T0) is Health.FAILING
    assert store.content_version == v and [i.key for i in store.snapshot(T0).attention] == ["g"]
    assert not store.apply("EV", SourceData(events=[Event(naive, "merged", "EV", "z")]), T0)
    assert store.status("EV").error == "naive datetime in Event.when"
    assert not store.apply("EV", SourceData(track=[TrackItem("k", "EV", "z", opened_at=naive)]), T0)
    assert store.status("EV").error == "naive datetime in TrackItem.opened_at"
    from textual_orbital.model import DoneTask, Goal

    fine = SourceData(goals=[Goal("g", "goal", due=date(2026, 11, 1))], done=[DoneTask("d", "done", T0)],
                      entities=[Entity("e", "e", data=PullRequest(1, "naive is fine in your own record"))])
    assert store.apply("EV", fine, T0), "a date is not a datetime, and data is the app's own"
    assert store.apply("TT", SourceData(attention=[AttentionItem("t", "TT", 5, "review", "y", since=T0)]), T0)
    assert store.status("TT").error is None, "the next good fetch clears it"


def test_store_rejects_keys_another_source_already_uses() -> None:
    """O9: keys stay bare, so two sources emitting "412" would snooze and look up together."""
    gh = Src("GH", SourceData(attention=[AttentionItem("412", "GH", 5, "review", "x")],
                              track=[TrackItem("web", "GH", "web")], entities=[Entity("s1", "s1")]))
    store = Store([gh, Src("TT", SourceData())])
    assert store.apply("GH", gh.fetch(), T0)
    assert store.apply("GH", gh.fetch(), T0), "a source may send its own keys again"
    clashes = [("412", SourceData(attention=[AttentionItem("412", "TT", 9, "respond", "y")])),
               ("web", SourceData(track=[TrackItem("web", "TT", "y")])),
               ("s1", SourceData(entities=[Entity("s1", "other")]))]
    for key, data in clashes:
        assert not store.apply("TT", data, T0), key
        assert store.status("TT").error == f"duplicate key '{key}' (also from GH): prefix keys with the source"
    snap = store.snapshot(T0)
    item = snap.item("412")
    assert [i.title for i in snap.attention] == ["x"] and item is not None and item.title == "x"
    assert store.apply("TT", SourceData(attention=[AttentionItem("TT:412", "TT", 9, "respond", "y")],
                                        entities=[Entity("TT:s1", "s1")]), T0), "prefixed keys are fine"
    assert store.apply("TT", SourceData(attention=[AttentionItem("s1", "TT", 9, "respond", "y")]), T0), \
        "an attention key may equal an entity id: separate lookups"


def test_poll_delay_backs_off_and_fetch_timeout_defaults() -> None:
    from textual_orbital.provider import BACKOFF_CAP, fetch_timeout, poll_delay

    assert [poll_delay(10, n) for n in range(4)] == [10, 20, 40, 80]
    assert poll_delay(10, 7) == BACKOFF_CAP == 900 and poll_delay(10, 10_000) == BACKOFF_CAP
    assert poll_delay(3600, 3) == 3600, "an interval past the cap is the cap"
    assert fetch_timeout(Src("X", SourceData())) == 120, "2 x a 60 s interval"
    fast = Src("CC", SourceData())
    fast.interval = 1.0
    assert fetch_timeout(fast) == 10, "at least 10 s"
    fast.timeout = 2.5  # type: ignore[attr-defined]
    assert fetch_timeout(fast) == 2.5


# ------------------------------------------------------------------ alerts --


def alert(key: str, priority: int = 50, from_rule: bool = False) -> Alert:
    return Alert(key, "test", key, priority=priority, from_rule=from_rule)


def test_queue_orders_dedupes_snoozes_and_resolves() -> None:
    q = AlertQueue()
    assert q.push(alert("low", 10)) and q.push(alert("high", 80, from_rule=True))
    assert not q.push(alert("low", 10)), "duplicate"
    assert q.pop().key == "high"  # type: ignore[union-attr]
    assert not q.push(alert("high", 80, from_rule=True)), "showing already"
    q.snooze(timedelta(minutes=10), at=T0)
    assert not q.push(alert("high", 80, from_rule=True)), "snoozed"
    assert q.wake(T0 + timedelta(minutes=11)) == 1 and len(q) == 2
    assert q.pop().key == "high"  # type: ignore[union-attr]
    assert q.resolve({"low"}), "the active alert's condition went away"


def test_resolve_drops_only_rule_alerts() -> None:
    q = AlertQueue()
    q.push(alert("snoozed", 99, from_rule=True))
    q.pop()
    q.snooze(timedelta(minutes=10), at=T0)
    for a in (alert("one-off", 90), alert("rule", 50, from_rule=True), alert("kept", 40, from_rule=True)):
        q.push(a)
    assert not q.resolve({"kept"}), "nothing is showing"
    assert [a.key for a in q.pending] == ["one-off", "kept"] and q.snoozed == {}
    assert q.pop().key == "one-off"  # type: ignore[union-attr]
    assert not q.resolve(set()), "a one-off alert is never resolved"


class FailedRule:
    """An app's own rule over track items (a release failed)."""

    def conditions(self, snapshot: Snapshot) -> Iterator[Condition]:
        for t in snapshot.track:
            if t.health is TrackHealth.FAIL:
                key = f"failed:{t.key}"
                yield Condition(key, t.source, Alert(key, "failed", t.title, priority=80, target=("track", t.key)))


SEEN = (SourceStatus("CC", "", 10, last_ok=T0), SourceStatus("GH", "", 60, last_ok=T0))


def test_rules_alert_on_transitions_not_on_baseline() -> None:
    rules = AlertRules([WaitingRule(), FailedRule()])
    waiting = Entity("s1", "deploy", "infra", EntityState.WAITING, T0, "permission prompt", url="https://x/s1")
    failing = TrackItem("GH:a#1", "GH", "a", health=TrackHealth.FAIL)
    fresh, live = rules.evaluate(Snapshot(T0, entities=(waiting,), track=(failing,), sources=SEEN))
    assert [a.kind for a in fresh] == ["waiting"], "a failure already there at startup is a baseline"
    assert live == {f"waiting:s1:{T0.isoformat()}", "failed:GH:a#1"}
    other = TrackItem("GH:b#2", "GH", "b", health=TrackHealth.FAIL)
    fresh, _ = rules.evaluate(Snapshot(T0, entities=(waiting,), track=(failing, other), sources=SEEN))
    assert [a.key for a in fresh] == ["failed:GH:b#2"]
    fresh, _ = rules.evaluate(Snapshot(T0, entities=(waiting,), track=(failing, other), sources=SEEN))
    assert fresh == [], "nothing new"
    again = replace(waiting, since=T0 + timedelta(minutes=5))
    fresh, _ = rules.evaluate(Snapshot(T0, entities=(again,), sources=SEEN))
    assert [a.kind for a in fresh] == ["waiting"], "a new wait is a new alert"
    fresh, _ = rules.evaluate(Snapshot(T0, entities=(again,), track=(failing,), sources=SEEN))
    assert [a.key for a in fresh] == ["failed:GH:a#1"], "a condition that went away alerts again when it comes back"


def test_waiting_rule_alert() -> None:
    waiting = Entity("s1", "deploy", "infra", EntityState.WAITING, T0, "permission prompt", "wants to run make",
                     url="https://x/s1")
    snap = Snapshot(T0, entities=(waiting, Entity("s2", "idle")), sources=SEEN)
    (a,) = AlertRules([WaitingRule()]).evaluate(snap)[0]
    assert (a.kind, a.title, a.body) == ("waiting", "deploy", ("infra · permission prompt", "wants to run make"))
    assert (a.heading, a.priority, a.color, a.target) == ("NEEDS YOU", 100, PALETTE["amber"], ("entity", "s1"))
    assert a.url == "https://x/s1" and a.from_rule
    assert Alert("k", "custom", "t").heading == "INCOMING TRANSMISSION"


def test_custom_rules_plug_in_and_alerts_are_completed() -> None:
    class Sloppy:  # an alert key that disagrees with its condition, and a picture of its own
        def conditions(self, snapshot: Snapshot) -> list[Condition]:
            return [Condition("deploy:1", "GH", Alert("other", "deploy", "deploy 1", image="/own.png"))]

    rules = AlertRules([WaitingRule(source="JOBS"), FailedRule(), Sloppy()],
                       images={"waiting": "/w.gif", "failed": "/f.png", "deploy": "/d.png"},
                       startup_kinds=frozenset({"failed"}))
    waiting = Entity("j1", "nightly", state=EntityState.WAITING, since=T0)
    failing = TrackItem("r1", "GH", "release", health=TrackHealth.FAIL)
    jobs = (*SEEN, SourceStatus("JOBS", "", 60, last_ok=T0))
    fresh, live = rules.evaluate(Snapshot(T0, entities=(waiting,), track=(failing,), sources=jobs))
    assert [(a.kind, a.image) for a in fresh] == [("failed", "/f.png")], "only startup kinds alert at startup"
    assert all(a.from_rule for a in fresh) and "deploy:1" in live
    fresh, _ = rules.evaluate(Snapshot(T0, entities=(replace(waiting, since=T0 + timedelta(minutes=1)),),
                                       track=(failing,), sources=jobs))
    assert [(a.kind, a.image) for a in fresh] == [("waiting", "/w.gif")], "JOBS has been seen now"

    sloppy = AlertRules([Sloppy()], startup_kinds=frozenset({"deploy"}))
    (a,), live = sloppy.evaluate(Snapshot(T0, sources=SEEN))
    assert a.key == "deploy:1" and a.image == "/own.png", "the condition's key wins, so resolve() can match it"
    q = AlertQueue()
    q.push(a)
    q.pop()
    assert not q.resolve(live)


# ------------------------------------------------------------------ notify --


def test_notification_escapes() -> None:
    assert osc777("Console: x;y", "body;more") == "\x1b]777;notify;Console: x,y;body;more\x07"
    assert osc9("", "3 waiting").startswith("\x1b]9;· 3"), "a digit then ; would read as ConEmu"
    assert set_title("a\x1bb") == "\x1b]2;a b\x07"
    assert notification_mode({"TERM_PROGRAM": "ghostty"}) == "osc777"
    assert notification_mode({"TERM": "xterm-256color"}) == "off"
    assert notification_mode({"TEXTUAL_ORBITAL_NOTIFY": "osc9"}) == "osc9"
    assert notification_mode({"TERM_PROGRAM": "ghostty", "STY": "1"}) == "off", "GNU screen: no passthrough"
    tmux = {"TMUX": "/tmp/tmux-501/default,1,0", "TERM": "tmux-256color", "TERM_PROGRAM": "tmux"}
    assert notification_mode(tmux) == "off", "an outer terminal we can't name"
    assert notification_mode({**tmux, "GHOSTTY_RESOURCES_DIR": "/Applications/Ghostty.app/x"}) == "osc777"
    assert notification_mode({**tmux, "KITTY_WINDOW_ID": "1"}) == "osc9"
    assert notification_mode({**tmux, "TERM": "xterm-ghostty", "TERM_PROGRAM": ""}) == "off", "TERM is tmux's"


def test_notifications_pass_through_tmux() -> None:
    """O14: under tmux, OSC 777/9 go through DCS passthrough (allow-passthrough on); the bell
    and the title don't, tmux handles those."""
    assert tmux_passthrough("\x1b]9;hi\x07") == "\x1bPtmux;\x1b\x1b]9;hi\x07\x1b\\"
    sent: list[str] = []
    app = cast(App[Any], SimpleNamespace(title="Console", set_timer=lambda wait, cb: None))
    n = Notifier(app, lambda data: sent.append(data) is None, mode="osc777")
    n.passthrough = True
    n.notify(Alert("a", "waiting", "deploy", headline="NEEDS YOU"))
    n.badge("◉ 1 waiting")
    assert sent == ["\x1bPtmux;\x1b\x1b]777;notify;Console: Needs you;deploy\x07\x1b\\\x07",
                    set_title("◉ 1 waiting · Console")]


def test_notifier_keeps_only_the_last_writes() -> None:
    app = cast(App[Any], SimpleNamespace(title="Console", set_timer=lambda wait, cb: None))
    n = Notifier(app, lambda data: True, mode="off")
    for i in range(Notifier.SENT_KEPT + 10):
        n.badge(f"◉ {i} waiting")
    last = Notifier.SENT_KEPT + 9
    assert len(n.sent) == Notifier.SENT_KEPT and n.sent[-1] == set_title(f"◉ {last} waiting · Console")


def test_notifier_spaces_notifications_and_badges_the_title(monkeypatch: pytest.MonkeyPatch) -> None:
    timers: list[tuple[float, Callable[[], None]]] = []
    app = cast(App[Any], SimpleNamespace(title="Console", set_timer=lambda wait, cb: timers.append((wait, cb))))
    written: list[str] = []
    connected = True

    def write(data: str) -> bool:
        written.append(data)
        return connected

    clock = [1000.0]
    monkeypatch.setattr(notify.time, "monotonic", lambda: clock[0])
    n = Notifier(app, write, mode="osc777")
    first = Alert("a", "waiting", "deploy", body=("infra · permission prompt",), headline="NEEDS YOU")
    n.notify(first)
    n.notify(replace(first, key="b", title="docs"))
    assert list(n.sent) == ["\x1b]777;notify;Console: Needs you;deploy · infra · permission prompt\x07\x07"]
    assert len(timers) == 1 and timers[0][0] == pytest.approx(notify.MIN_SPACING), "the second waits its turn"
    clock[0] += notify.MIN_SPACING
    timers[0][1]()
    assert n.sent[-1].startswith("\x1b]777;notify;Console: Needs you;docs")
    n.notify(replace(first, key="c", notify=False))
    assert len(n.sent) == 2, "notify=False sends nothing"

    n.badge("◉ 1 waiting")
    n.badge("◉ 1 waiting")
    n.badge(None)
    n.restore()
    assert list(n.sent)[2:] == [set_title("◉ 1 waiting · Console"), set_title("Console"), set_title("")]
    connected = False
    n.badge("◉ 2 waiting")
    assert len(n.sent) == 5 and written[-1] == set_title("◉ 2 waiting · Console"), "unsent writes aren't recorded"


# ------------------------------------------------------------------- theme --


def test_theme_helpers() -> None:
    assert fmt_age(30) == "<1m" and fmt_age(3 * 3600) == "3h" and fmt_age(timedelta(days=20)) == "2w"
    assert urgency_color(10) != urgency_color(2)
    assert source_color("CC") != source_color("GH") != source_color("ZZ") == PALETTE["text_soft"]
    assert why_color("respond") == PALETTE["red"] and why_color("anything else") == PALETTE["text_dim"]


def test_ages_are_minute_resolution() -> None:
    assert fmt_age(19) == "<1m" and fmt_age(59.9) == "<1m" and fmt_age(61) == "1m"
    assert fmt_age(timedelta(seconds=-5)) == "<1m", "clock skew never shows seconds or negatives"
    assert fmt_interval(10) == "10s" and fmt_interval(60) == "1m"


GLASS_VARIABLES = ("glass-backdrop", "glass-scrim-alpha", "glass-nebula-1", "glass-nebula-2", "glass-nebula-3",
                   "glass-nebula-strength")


@pytest.mark.parametrize("theme", [ORBITAL, ORBITAL_NAVY], ids=lambda t: t.name)
async def test_theme_presets_expose_the_glass_variables(theme: Theme) -> None:
    app: App[None] = App()
    async with app.run_test() as pilot:
        app.register_theme(theme)
        app.theme = theme.name
        await pilot.pause()
        variables = app.get_css_variables()
    assert all(name in variables for name in GLASS_VARIABLES)
    assert variables["glass-backdrop"] == "nebula" and float(variables["glass-scrim-alpha"]) == 0.62
    assert all(variables[f"glass-nebula-{i}"].startswith("#") for i in (1, 2, 3))
    assert float(variables["glass-nebula-strength"]) == (1.0 if theme is ORBITAL else 0.0)
    assert variables["background"].lower() == PALETTE["background"] and theme.primary == PALETTE["accent"]


def test_presets_differ_only_in_the_nebula() -> None:
    assert ORBITAL.name == "orbital" and ORBITAL_NAVY.name == "orbital-navy"
    assert ORBITAL.variables is not ORBITAL_NAVY.variables
    assert {k for k, v in ORBITAL.variables.items() if ORBITAL_NAVY.variables[k] != v} == {"glass-nebula-strength"}
    assert replace(ORBITAL_NAVY, name="orbital", variables=ORBITAL.variables) == ORBITAL


# -------------------------------------------------------------- demo world --


def test_demo_world_script_produces_every_alert_kind() -> None:
    from demo import DemoProvider
    from rules import INCIDENT, RELEASE_FAILED, IncidentRule, ReleaseFailedRule

    provider = DemoProvider(speed=1.0)
    world = provider.world
    rules = AlertRules([WaitingRule(), ReleaseFailedRule(), IncidentRule()])
    store = Store(provider.sources())

    def poll() -> list[Alert]:
        for source in provider.sources():
            data, error, _ = timed_fetch(source)
            if data is not None:
                store.apply(source.key, data)
            else:
                store.fail(source.key, error or "")
        return rules.evaluate(store.snapshot())[0]

    first = poll()
    assert {a.kind for a in first} == {"waiting"}, "a failing release at startup is a baseline, not news"
    kinds = {a.kind for a in first}
    for elapsed in (30, 60, 80, 110, 125, 160, 175):
        world.t0 = world.t0 - (elapsed - world.elapsed())  # jump the world's clock forward
        fresh = poll()
        kinds |= {a.kind for a in fresh}
        if elapsed == 30:
            assert [a.title for a in fresh] == ["rate-limit-middleware"], "a second session waits at 25 s"
    assert kinds == {"waiting", RELEASE_FAILED, INCIDENT}
    snap = store.snapshot()
    assert snap.item("TT-2231") is not None, "a ticket is assigned at 100 s"
    landed = snap.track_item("GH:storefront@4.12.0")
    assert landed is not None and (landed.stage, landed.health) == (4, TrackHealth.OK), "it lands at 150 s"
    assert any(e.kind == "shipped" and e.title.startswith("storefront:") for e in snap.events), "and is counted"
    assert any(s.key == "SS" and s.error for s in store.statuses()), "the scanner fails at 170 s"


def test_static_demo_world_never_changes() -> None:
    from demo import DemoProvider

    provider = DemoProvider(speed=1000.0, dynamics=False)
    first = [s.fetch() for s in provider.sources()]
    provider.world.t0 -= 1000
    second = [s.fetch() for s in provider.sources()]
    assert first == second


def test_demo_churn_keeps_the_world_sane() -> None:
    """Long after the script, random churn keeps every record valid."""
    from demo import STAGES, DemoProvider

    provider = DemoProvider(speed=1.0, seed=3)
    world = provider.world
    world.t0 -= 3600  # an hour of churn at once
    data = [timed_fetch(s) for s in provider.sources()]
    assert all(d is not None or e == "scanner API: HTTP 503 (maintenance)" for d, e, _ in data)
    assert world.releases and all(0 <= t.stage < len(STAGES) for t in world.releases.values())
    assert len({e.id for e in world.sessions.values()}) == 5


# ---------------------------------------------------------------- rule 14 --


def test_app_and_widgets_add_no_attribute_textual_has() -> None:
    """Rule 14: what ``OrbitalApp``, the console and the widgets add must not
    shadow Textual (``App._context`` is a method, ``Widget.colors`` a
    property). Only Textual's hooks and settings are overridden on purpose."""
    import re

    import app as console
    from demo import DemoProvider
    from textual.screen import ModalScreen
    from textual.widget import Widget

    from textual_glass import Glass
    from textual_orbital import OrbitalApp
    from textual_orbital import widgets as W

    dunder = re.compile(r"^__.*__$")
    settings = {"BINDINGS", "DEFAULT_CSS", "SCOPED_CSS", "CSS_PATH", "TITLE", "HORIZONTAL_BREAKPOINTS",
                "VERTICAL_BREAKPOINTS", "__init__", "exit", "compose", "on_mount", "on_resize", "on_click",
                "on_focus", "on_blur", "render_line", "get_content_width", "can_focus"}
    app_made = set(vars(type("Plain", (App,), {})))
    for cls in (OrbitalApp, console.MissionConsole):
        added = {n for n in vars(cls) if not dunder.match(n) or n == "__init__"} - app_made
        assert not {n for n in added - settings if hasattr(App, n)}, cls

    class Bare(Glass, App[None]):
        pass

    instance = set(vars(console.MissionConsole(DemoProvider(dynamics=False)))) - set(vars(Bare()))
    assert instance and not {n for n in instance if hasattr(App, n)}, instance

    widgets = [W.BeaconRow(), W.PriorityList(), W.Track(("A", "B")), W.TrackTable(("A",)), W.TaskBoard(),
               W.EventLog(), W.DoneList(), W.HeaderStrip(), W.Brand(), W.Clock(), W.KeyBar(), W.SourceHealth(),
               W.Page()]
    screens = [W.AlertScreen(Alert("k", "waiting", "t")), W.DetailScreen("t"), W.HelpScreen([]),
               W.SourcesScreen([])]
    plain_widget, plain_screen = set(vars(Widget())), set(vars(ModalScreen()))
    for obj, base, plain in [*((w, Widget, plain_widget) for w in widgets),
                             *((s, ModalScreen, plain_screen) for s in screens)]:
        made = set(vars(type("Plain", (base,), {}, can_focus=True)))
        methods = {n for c in type(obj).__mro__ if c.__module__.startswith("textual_orbital") for n in vars(c)
                   if not dunder.match(n)} - made - settings
        attrs = set(vars(obj)) - plain
        clashes = {n for n in methods | attrs if hasattr(base, n) and not n.startswith(("action_", "_"))}
        assert not clashes, (type(obj).__name__, clashes)


async def test_the_header_clock_ticks_outside_the_pages() -> None:
    """The minute tick reaches a Clock wherever it sits, not only the widgets inside a Page."""
    from textual.app import ComposeResult

    from textual_orbital import OrbitalApp, StaticProvider
    from textual_orbital.widgets import HeaderStrip

    class Board(OrbitalApp):
        def compose(self) -> ComposeResult:
            yield HeaderStrip("BOARD", ["one"], id="header")

    app = Board(StaticProvider([]), alerts=False, glass=False)
    async with app.run_test(size=(60, 10)):
        clock = app.query_one(HeaderStrip).clock
        clock.text = "--:--"
        app.minute_tick(T0)
        assert clock.text != "--:--", "OrbitalApp.minute_tick ticked the header's clock"
