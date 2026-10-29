"""``WeekReplay``, the week in review: with glass forced on and its output captured,
the show plays when its page shows, its caption follows the cues, the tally ends it
and it holds in silence; a replay, a resize, a new theme, a hidden page and glass
going off each do what they should. Without glass it is a text rundown. Plus the
console's Replay page, and the conversion of event times to hours of the local week.

The scenes' clock is a fake one the tests move, so a show is stepped cue by cue.
What the widget asks glass for, and what glass tells it back, is recorded as it
happens (``Plays``): a whole scene can start and end inside one ``pilot.pause()``,
which waits for the CPU to go idle while glass's worker draws, so polling the
widget's state between pauses can miss it.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock_time
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import app as console
import pytest
from demo import DemoProvider
from pages import SHIPS as CONSOLE_SHIPS
from pages import ReplayPage
from textual.app import App, ComposeResult
from textual.widgets import ContentSwitcher, Static

from textual_glass import Glass, GlassPanel
from textual_glass import engine as engine_module
from textual_glass.palette import luminance
from textual_glass.player import Showing
from textual_glass.scenes import space
from textual_glass.testing.pilot_helpers import Output, idle, settle, start
from textual_orbital import Event, KeyBar, WeekReplay, WeekStats, now
from textual_orbital.model import monday_of
from textual_orbital.widgets.replay import SHIPS, ReplayWeek, week_hours

SIZE = (90, 28)
CELL = (9.0, 19.0)
ZONE = "America/New_York"
#: The test week's kinds: the app's own words, mapped to the five ships ("incident" flies none).
MAPPING = {"merged": "merged", "reviewed": "review", "shipped": "deploy", "ticket closed": "ticket",
           "released": "release"}


# -- the setting -----------------------------------------------------------------------------


@contextmanager
def local_zone(name: str) -> Iterator[ZoneInfo]:
    """Run with ``name`` as the process's local time zone (``datetime.astimezone()``)."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield ZoneInfo(name)
    finally:
        if old is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = old
        time.tzset()


@pytest.fixture(autouse=True)
def zone() -> Iterator[ZoneInfo]:
    with local_zone(ZONE) as found:
        yield found


@pytest.fixture(autouse=True)
def cell(monkeypatch: pytest.MonkeyPatch) -> None:
    """9x19 px cells, whatever terminal runs the tests."""
    monkeypatch.setattr(engine_module, "terminal_cell_pixels", lambda: (*CELL, True))


class Clock:
    """The scenes' clock, moved by the test."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Plays:
    """Every scene the widget asked glass for, with its start and what its callbacks heard,
    recorded as it happened."""

    def __init__(self, app: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        real = app.glass.play_scene

        def play(make: Any, at: Any = None, start: float = 0.0, on_cue: Any = None, on_end: Any = None) -> Any:
            entry: dict[str, Any] = {"make": make, "at": at, "start": start, "cues": [], "ends": []}

            def cue(data: Hashable) -> None:
                entry["cues"].append(data)
                on_cue(data)

            def end(reason: str) -> None:
                entry["ends"].append(reason)
                on_end(reason)

            entry["show"] = real(make, at=at, start=start, on_cue=cue, on_end=end)
            self.calls.append(entry)
            return entry["show"]

        app.glass.play_scene = play

    @property
    def starts(self) -> list[float]:
        return [c["start"] for c in self.calls]

    def show(self, n: int = -1) -> Showing:
        found = self.calls[n]["show"]
        assert isinstance(found, Showing), self.calls[n]
        return found


async def until(condition: Callable[[], bool], timeout: float = 10.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out: {what}")
        await asyncio.sleep(0.02)


async def landed(show: Showing, timeout: float = 20.0) -> None:
    """Wait until the scene's pictures are in the terminal (its clock starts), or it ended."""
    await until(lambda: show._t0 is not None or show.ended is not None, timeout, f"the scene landed: {show!r}")
    await asyncio.sleep(0)


async def at(app: Any, clock: Clock, show: Showing, t: float) -> None:
    """Move the scenes' clock to ``t`` seconds into the show, tick, and let the callbacks run."""
    assert show._t0 is not None
    clock.now = show._t0 + t
    app.glass.tick()
    for _ in range(3):
        await asyncio.sleep(0)


def this_monday() -> date:
    return monday_of(now().astimezone().date())


def ev(monday: date, day: int, hour: int, minute: int, kind: str, title: str, source: str = "GH") -> Event:
    when = datetime.combine(monday + timedelta(days=day), clock_time(hour, minute), tzinfo=ZoneInfo(ZONE))
    return Event(when, kind, source, title)


def sample(monday: date) -> list[Event]:
    """Six pieces of work and an incident, oldest first."""
    return [ev(monday, 0, 9, 30, "merged", "Add a retry budget"),
            ev(monday, 0, 15, 10, "reviewed", "Cache product images"),
            ev(monday, 1, 14, 2, "merged", "fix the flaky test"),
            ev(monday, 2, 11, 0, "shipped", "storefront 4.12"),
            ev(monday, 2, 16, 45, "ticket closed", "Export times out", "TT"),
            ev(monday, 3, 10, 5, "released", "v2.0"),
            ev(monday, 3, 12, 0, "incident", "Checkout errors", "IM")]


def week_of(events: list[Event], monday: date | None = None, elapsed: int = 4) -> WeekStats:
    """The ``WeekStats`` a page would pass (``snapshot.week()``): only its start, events and days so far matter."""
    return WeekStats(start=monday or this_monday(), days=(), previous=(),
                     events=tuple(sorted(events, key=lambda e: e.when, reverse=True)), elapsed=elapsed)


TALLY = "this week: 2 merged · 1 review · 1 deploy · 1 ticket · 1 release · 1 other"


class Board(Glass, App[None]):
    """Two pages: something else, and the replay in a panel."""

    CSS = """
    ContentSwitcher { height: 1fr; }
    #other { height: 1fr; border: round $primary; }
    #replay-page { height: 1fr; }
    """

    def __init__(self, ships: dict[str, str] | None = MAPPING, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ships = ships

    def compose(self) -> ComposeResult:
        with ContentSwitcher(initial="other", id="pages"):
            yield Static("another page", id="other")
            with GlassPanel(title="WEEK IN REVIEW", id="replay-page"):
                yield WeekReplay(ships=self.ships, id="replay")

    @property
    def replay(self) -> WeekReplay:
        return self.query_one(WeekReplay)

    async def page(self, pilot: Any, name: str) -> None:
        self.query_one(ContentSwitcher).current = name
        await settle(self, pilot)


async def playing(app: Board, pilot: Any, events: list[Event] | None = None,
                  ) -> tuple[Plays, Clock, Showing]:
    """Glass on, the scenes on a fake clock, the week set, its page shown, the first scene in the terminal."""
    await start(app, pilot)
    clock = app.glass.player.clock = Clock()
    plays = Plays(app)
    app.replay.set_week(week_of(sample(this_monday()) if events is None else events))
    await settle(app, pilot)
    assert plays.calls == [], "nothing plays while its page is hidden"
    await app.page(pilot, "replay-page")
    await until(lambda: len(plays.calls) == 1, what="it plays when its page shows")
    show = plays.show()
    await landed(show)
    return plays, clock, show


async def to_the_end(app: Board, clock: Clock, show: Showing) -> None:
    assert show.scene is not None
    await at(app, clock, show, show.scene.duration + 1.0)  # past the end (the clock is a float)
    assert show.holding


def bottom(replay: WeekReplay) -> str:
    return replay.render_line(replay.size.height - 1).text.strip()


def clean(output: Output) -> None:
    model = output.model(CELL)
    assert model.errors == [] and model.no_quiet == 0 and model.queries == 0, model.errors


# -- playing -----------------------------------------------------------------------------------


async def test_it_plays_when_its_page_shows(output: Output) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        plays, _, show = await playing(app, pilot)
        replay = app.replay
        call = plays.calls[0]
        make = call["make"]
        assert call["at"] is replay and call["start"] == 0.0 and replay.showing is show
        assert isinstance(make, partial) and make.func is space.make, "glass's week in review"
        assert make.keywords["look"] == "halo" and make.keywords["length"] == 20.0
        works = make.keywords["week"]
        assert [w.kind for w in works] == ["merged", "review", "merged", "deploy", "ticket", "release"], \
            "oldest first, the incident left out"
        assert works[2].hour == pytest.approx(24 + 14 + 2 / 60), "Tue 14:02"
        assert show.ended is None and show._casts, "its pictures went up"
        assert {c.image_id for c in show._casts if not c.gone} <= set(output.model(CELL).images)
        assert bottom(replay) == "this week · 0 of 6 landed" and replay.caption == "", "before the first lands"
        assert app.glass.errors == 0
    clean(output)


async def test_captions_follow_the_cues_and_the_tally_ends_the_show(output: Output) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        plays, clock, show = await playing(app, pilot)
        replay = app.replay
        assert show.scene is not None
        cues = sorted(show.scene.cues, key=lambda c: c.t)
        landings = [c for c in cues if isinstance(c.data, tuple) and c.data[0] == "landed"]
        assert sorted(c.data[1] for c in landings) == list(range(6)), "one landing per piece of work"
        events = sample(this_monday())[:6]
        height = replay.size.height
        for n, cue in enumerate(landings):
            await at(app, clock, show, cue.t + 1e-3)  # just past it: the clock is a float
            _, i, kind = cue.data
            event = events[i]
            assert kind == MAPPING[event.kind]
            if n == len(landings) - 1:
                break
            caption = replay.render_line(height - 2).text.strip()
            assert caption == replay.caption == f"{event.when:%a %H:%M} · {event.kind} · {event.title} ({event.source})"
            assert bottom(replay) == replay.status == f"this week · {n + 1} of 6 landed" and not replay.done
            if i == 2:
                assert caption == "Tue 14:02 · merged · fix the flaky test (GH)"
        assert cues[-1].data == space.END
        await at(app, clock, show, cues[-1].t + 1e-3)
        assert plays.calls[0]["cues"] == [c.data for c in cues], "every cue, once, in order"
        assert replay.done and bottom(replay) == TALLY == replay.status == replay.tally(), "the tally, once all landed"
        assert replay.caption == "latest: Thu 10:05 · released · v2.0 (GH)", "and the newest piece of work"
        await to_the_end(app, clock, show)
        assert bottom(replay) == TALLY and app.glass.errors == 0
    clean(output)


async def test_it_holds_in_silence(output: Output) -> None:
    """Rule 9: once the show is over, nothing is sent, the same data again changes nothing,
    and neither does the minute tick."""
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        plays, clock, show = await playing(app, pilot)
        await to_the_end(app, clock, show)
        replay = app.replay
        await idle(app, pilot)
        refreshes: list[int] = []
        real_refresh = replay.refresh
        replay.refresh = lambda *a, **k: refreshes.append(1) or real_refresh(*a, **k)  # type: ignore[method-assign]
        quiet = output.mark()
        for k in range(5):
            await at(app, clock, show, 100.0 + k)
            await pilot.pause()
            app.glass.on_frame()
            await asyncio.sleep(0.02)
        replay.set_week(week_of(sample(this_monday())))  # equal data: costs nothing
        for widget in app.query("*"):
            tick = getattr(widget, "tick", None)
            if callable(tick):
                tick(now())
        await settle(app, pilot)
        assert output.since(quiet) == "", "a holding summary sends nothing"
        assert refreshes == [] and len(plays.calls) == 1 and show.holding
    clean(output)


async def test_new_data_while_it_holds_updates_the_summary_quietly(output: Output) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        plays, clock, show = await playing(app, pilot)
        await to_the_end(app, clock, show)
        replay = app.replay
        monday = this_monday()
        events = sample(monday)
        replay.set_week(week_of([*events, ev(monday, 3, 13, 0, "incident", "Search latency", "IM")]))
        await settle(app, pilot)
        assert len(plays.calls) == 1 and show.holding, "the same ships: no new scene"
        assert bottom(replay).endswith("1 release · 2 other"), "only the tally changes"
        replay.set_week(week_of([*events, ev(monday, 3, 14, 0, "merged", "Batch the writes")]))
        await until(lambda: len(plays.calls) == 2, what="the new summary")
        assert plays.starts == [0.0, math.inf], "its end at once, no replay"
        assert plays.calls[0]["ends"] == ["replaced"] and replay.done
        assert bottom(replay) == "this week: 3 merged · 1 review · 1 deploy · 1 ticket · 1 release · 1 other"
        await landed(plays.show())
        assert plays.show().holding and plays.calls[1]["cues"] == []
    clean(output)


async def test_new_data_during_the_show_waits_for_its_end(output: Output) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        plays, clock, show = await playing(app, pilot)
        monday = this_monday()
        app.replay.set_week(week_of([*sample(monday), ev(monday, 3, 14, 0, "merged", "Batch the writes")]))
        await settle(app, pilot)
        assert len(plays.calls) == 1, "the show plays on"
        await to_the_end(app, clock, show)
        await until(lambda: len(plays.calls) == 2, what="then the new summary")
        assert plays.starts == [0.0, math.inf] and app.replay.tally().startswith("this week: 3 merged")
    clean(output)


async def test_replay_plays_it_again_from_the_start(output: Output) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        plays, clock, show = await playing(app, pilot)
        await to_the_end(app, clock, show)
        app.replay.replay()
        await until(lambda: len(plays.calls) == 2, what="a replay")
        assert plays.starts == [0.0, 0.0] and plays.calls[0]["ends"] == ["replaced"]
        assert not app.replay.done and bottom(app.replay) == "this week · 0 of 6 landed"
        again = plays.show()
        await landed(again)
        assert again.scene is not None
        await at(app, clock, again, again.scene.duration + 1.0)
        assert app.replay.done and bottom(app.replay) == TALLY
        app.replay.set_look("nightfall")
        await until(lambda: len(plays.calls) == 3, what="the next look")
        assert plays.calls[2]["make"].keywords["look"] == "nightfall" and plays.starts[2] == 0.0
        assert app.replay.next_look() == space.LOOKS[(space.LOOKS.index("nightfall") + 1) % len(space.LOOKS)]
        with pytest.raises(ValueError):
            app.replay.set_look("sunrise")
    clean(output)


@pytest.mark.parametrize("change", ["resize", "theme"])
async def test_a_resize_or_a_new_theme_puts_the_end_back(output: Output, change: str) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        plays, clock, show = await playing(app, pilot)
        await at(app, clock, show, 2.0)  # mid-show
        if change == "resize":
            await pilot.resize_terminal(110, 32)
        else:
            app.theme = "textual-light"
        await settle(app, pilot)
        replay = app.replay

        def back() -> bool:  # the end, put back where the widget is now
            last = plays.calls[-1]["show"]
            return len(plays.calls) >= 2 and last is not None and last.holding and last._cells[2:] == tuple(replay.size)

        await until(back, what="the end, put back")
        assert plays.calls[0]["ends"] == ["resized" if change == "resize" else "theme"]
        assert plays.starts[0] == 0.0 and set(plays.starts[1:]) == {math.inf}, plays.starts
        assert all(c["ends"] == ["moved"] for c in plays.calls[1:-1]), "a put-back made before the layout settled"
        assert replay.done and bottom(replay) == TALLY
        if change == "theme":  # light on dark: the show paints its night sky over the widget
            for y in (replay.size.height - 2, replay.size.height - 1):  # the caption and the tally
                line = list(replay.render_line(y))
                words = {seg.style.color.triplet for seg in line
                         if any(ch.isalnum() for ch in seg.text) and seg.style and seg.style.color}
                assert words and all(luminance(tuple(c)) > 0.55 for c in words), (y, words)
                assert all(seg.style is None or seg.style.bgcolor is None for seg in line), "no cell backgrounds"
        assert app.glass.errors == 0
    clean(output)


async def test_hidden_then_shown_plays_again(output: Output) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        plays, clock, show = await playing(app, pilot)
        await to_the_end(app, clock, show)
        ids = {c.image_id for c in show._casts or ()}
        await app.page(pilot, "other")
        assert plays.calls[0]["ends"] == ["hidden"] and app.replay.showing is None
        assert not ids & set(output.model(CELL).images), "its pictures are deleted"
        await asyncio.sleep(0.1)
        await settle(app, pilot)
        assert len(plays.calls) == 1, "nothing plays while it is hidden"
        await app.page(pilot, "replay-page")
        await until(lambda: len(plays.calls) == 2, what="it plays again")
        assert plays.starts == [0.0, 0.0]
    clean(output)


async def test_glass_going_off_shows_the_rundown_and_coming_back_puts_the_summary_up(output: Output) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        plays, clock, show = await playing(app, pilot)
        await to_the_end(app, clock, show)
        app.glass.deactivate()
        await settle(app, pilot)
        replay = app.replay
        monday = this_monday()
        assert plays.calls[0]["ends"] == ["off"] and replay.caption == ""
        assert replay.render_line(1).text.split()[:2] == [f"{monday:%a}", str(monday.day)], "the rundown"
        app.glass.activate(force=True)
        await until(lambda: len(plays.calls) == 2, what="the summary, back")
        assert plays.starts == [0.0, math.inf], "it already played: its end at once"
    clean(output)


async def test_at_minimal_motion_the_summary_is_a_still_and_the_tally_shows_at_once(output: Output) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        app.glass.set_motion("minimal")
        app.glass.player.clock = Clock()
        plays = Plays(app)
        app.replay.set_week(week_of(sample(this_monday())))
        await app.page(pilot, "replay-page")
        await until(lambda: len(plays.calls) == 1, what="a still")
        show = plays.show()
        assert show.stage.still and app.replay.done and bottom(app.replay) == TALLY, "the tally at once"
        await landed(show)
        assert show.holding and plays.calls[0]["cues"] == [space.END], "no landings, only the end"
    clean(output)


async def test_an_empty_week_is_quiet(output: Output) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        plays, _, show = await playing(app, pilot, events=[])
        replay = app.replay
        assert replay.done and bottom(replay) == "nothing landed yet this week", "over the planet"
        assert replay.caption == "" and replay.render_line(replay.size.height - 2).text.strip() == ""
        assert show.ended is None and plays.calls[0]["make"].keywords["week"] == []
        replay.set_week(week_of([ev(this_monday(), 0, 9, 0, "incident", "Search latency", "IM")]))
        assert bottom(replay) == "nothing landed yet this week · 1 other"
    clean(output)


# -- without glass -------------------------------------------------------------------------------


class Plain(App[None]):
    """A stock Textual app: no glass at all."""

    def compose(self) -> ComposeResult:
        yield WeekReplay(ships=MAPPING, id="replay")


@pytest.mark.parametrize("glass", [False, True], ids=["stock Textual", "glass off"])
async def test_without_glass_it_is_a_text_rundown_by_day(output: Output, glass: bool) -> None:
    app: App[None] = Board() if glass else Plain()
    async with app.run_test(size=SIZE) as pilot:
        replay = app.query_one(WeekReplay)
        if glass:
            await app.page(pilot, "replay-page")  # type: ignore[attr-defined]
        assert "waiting for the first fetch" in replay.render_line(0).text
        monday = this_monday()
        replay.set_week(week_of(sample(monday)))
        await pilot.pause()
        lines = [replay.render_line(y).text.rstrip() for y in range(replay.size.height)]
        header = lines[0].split()
        assert header == ["merged", "reviews", "deploys", "tickets", "releases", "other"], lines[0]
        assert lines[1].split() == [f"{monday:%a}", str(monday.day), "1", "1", "·", "·", "·", "·"], lines[1]
        assert lines[3].split()[2:] == ["·", "·", "1", "1", "·", "·"], "Wednesday: a deploy and a ticket"
        assert lines[4].split()[2:] == ["·", "·", "·", "·", "1", "1"], "Thursday: the release and the incident"
        assert lines[5] == "", "four days so far, then a blank line"
        assert lines[6].strip() == TALLY == replay.tally()
        assert lines[7].strip() == "other: 1 incident"
        assert replay.caption == "" and replay.showing is None
        replay.styles.height = 4  # short: the latest days and the tally
        await pilot.pause()
        short = [replay.render_line(y).text.strip() for y in range(4)]
        assert short[0].startswith("merged") and short[1].startswith(f"{monday + timedelta(days=3):%a}")
        assert short[2:] == [TALLY, "other: 1 incident"]
        replay.set_week(week_of([]))
        await pilot.pause()
        assert replay.render_line(0).text.strip() == "nothing landed yet this week"
    assert "\x1b_G" not in output.text, "no graphics at all"


# -- the week ---------------------------------------------------------------------------------------


def test_kinds_map_to_ships_and_the_others_only_count() -> None:
    monday = date(2026, 10, 26)
    events = sample(monday)
    week = ReplayWeek.read(week_of(events, monday, 7), MAPPING, this_monday=monday)
    assert [e.title for e in week.items] == [e.title for e in events[:6]], "oldest first"
    assert [w.kind for w in week.works] == ["merged", "review", "merged", "deploy", "ticket", "release"]
    assert [e.kind for e in week.others] == ["incident"] and week.label == "this week" and week.days == 7
    past = ReplayWeek.read(week_of(events, monday, 7), MAPPING, this_monday=monday + timedelta(weeks=2))
    assert past.label == "the week of Oct 26" and past.empty_text() == "nothing landed in the week of Oct 26"
    default = ReplayWeek.read(week_of(events, monday, 7), SHIPS, this_monday=monday)
    assert [w.kind for w in default.works] == ["merged", "review", "merged", "deploy", "ticket", "release"], \
        "the default map knows the common words"
    console_week = ReplayWeek.read(week_of(events, monday, 7), CONSOLE_SHIPS, this_monday=monday)
    assert [e.kind for e in console_week.others] == ["released", "incident"]
    assert set(CONSOLE_SHIPS.values()) <= set(space.KINDS) and set(SHIPS.values()) <= set(space.KINDS)
    with pytest.raises(ValueError, match="unknown ship"):
        WeekReplay(ships={"merged": "submarine"})


def test_hours_count_from_the_local_monday_by_the_wall_clock() -> None:
    ny = ZoneInfo(ZONE)
    monday = date(2026, 10, 26)
    assert week_hours(datetime(2026, 10, 27, 14, 2, tzinfo=ny), monday) == pytest.approx(38 + 2 / 60)
    late = datetime(2026, 10, 25, 23, 30, tzinfo=ny)  # Sunday night: already Monday in UTC
    assert late.astimezone(timezone.utc).date() == date(2026, 10, 26)
    assert week_hours(late, monday - timedelta(weeks=1)) == pytest.approx(167.5), "the end of last week"
    assert week_hours(datetime(2026, 10, 26, 3, 30, tzinfo=timezone.utc), monday) == pytest.approx(-0.5), \
        "UTC early on Monday is Sunday night here"
    # The clocks go back at 02:00 on Sunday Nov 1: that week lasts 169 hours, the wall clock 168.
    sunday = datetime(2026, 11, 1, 23, 30, tzinfo=ny)
    start = datetime(2026, 10, 26, tzinfo=ny)
    assert (sunday.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 3600 == 168.5
    assert week_hours(sunday, monday) == pytest.approx(167.5), "Sunday 23:30 by the wall clock"
    events = [Event(late, "merged", "GH", "last week"), Event(sunday, "merged", "GH", "Sunday night"),
              Event(datetime(2026, 11, 2, 0, 10, tzinfo=ny), "merged", "GH", "next week")]
    week = ReplayWeek.read(week_of(events, monday, 7), MAPPING, this_monday=monday)
    assert [e.title for e in week.items] == ["Sunday night"] and week.works[0].day == 6
    # Spring forward (Sun Mar 8, 02:00): 03:30 that Sunday is 146.5 real hours in, 147.5 on the wall clock.
    assert week_hours(datetime(2026, 3, 8, 3, 30, tzinfo=ny), date(2026, 3, 2)) == pytest.approx(147.5)


def test_hours_follow_the_local_zone_not_the_events() -> None:
    when = datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc)  # Sunday 23:30 UTC
    with local_zone("Europe/Berlin"):  # Monday 00:30 there: its clocks went back that morning, to UTC+1
        assert week_hours(when, date(2026, 10, 26)) == pytest.approx(0.5)
    with local_zone("Asia/Tokyo"):
        assert week_hours(when, date(2026, 10, 26)) == pytest.approx(8.5)
    with local_zone("America/Los_Angeles"):  # still Sunday afternoon: the end of the week before
        assert week_hours(when, date(2026, 10, 19)) == pytest.approx(6 * 24 + 16.5)


# -- the console's Replay page ----------------------------------------------------------------------


async def test_the_console_replay_page(output: Output) -> None:
    app = console.MissionConsole(DemoProvider(dynamics=False), alerts=False, notify="off")
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        await until(lambda: app.snapshot is not None, what="the first fetch")
        app.glass.player.clock = Clock()
        plays = Plays(app)
        await pilot.press("5")
        await until(lambda: len(plays.calls) == 1, what="page 5 plays")
        page = app.query_one(ReplayPage)
        replay = app.query_one("#week-replay", WeekReplay)
        assert app.focused is replay and plays.starts == [0.0]
        assert app.query_one("#keys", KeyBar).keys == list(ReplayPage.KEYS)
        assert ("p", "play again") in ReplayPage.KEYS and ("l", "look") in ReplayPage.KEYS
        week = plays.calls[0]["make"].keywords["week"]
        assert len(week) == replay.landed and {w.kind for w in week} <= {"merged", "review", "deploy", "ticket"}
        title = str(app.query_one("#replay-panel", GlassPanel).border_title)
        assert "WEEK IN REVIEW" in title and f"{replay.landed} landed" in title and "halo" in title
        await pilot.press("p")
        await until(lambda: len(plays.calls) == 2, what="p plays it again")
        await pilot.press("l")
        await until(lambda: len(plays.calls) == 3, what="l, the next look")
        assert plays.starts == [0.0, 0.0, 0.0] and replay.look == "harbour"
        assert plays.calls[2]["make"].keywords["look"] == "harbour"
        assert "harbour" in str(app.query_one("#replay-panel", GlassPanel).border_title)
        assert ("1-5", "switch page") in console.HELP_KEYS and len(console.HELP_KEYS) <= 14
        await pilot.press("1")
        await settle(app, pilot)
        assert plays.calls[2]["ends"] == ["hidden"] and page.display is False
        assert not app.errors, list(app.errors)


async def test_the_console_replay_page_without_glass() -> None:
    app = console.MissionConsole(DemoProvider(dynamics=False), alerts=False, notify="off", glass=False)
    async with app.run_test(size=SIZE) as pilot:
        await until(lambda: app.snapshot is not None, what="the first fetch")
        await pilot.press("5")
        await pilot.pause()
        replay = app.query_one("#week-replay", WeekReplay)
        text = "\n".join(replay.render_line(y).text for y in range(replay.size.height))
        assert "this week" in text and replay.showing is None
        assert not app.errors, list(app.errors)
