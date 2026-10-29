"""``WeekReplay``: the week in review as a space show, then its summary.

With glass, the widget plays glass's week-in-review scene over itself
(:mod:`textual_glass.scenes.space`): a ship for each piece of work, in the
order it happened, each leaving a light where it lands. Then the scene holds
its last frame as the week's summary: one light per piece of work, its shape
and colour telling the kind, its place telling when it happened. A caption
follows the ships ("Tue 14:02 · merged · fix the flaky test (GH)") and a
tally ends the show. While it holds, glass sends nothing (rule 9).

Without glass (or after glass turned off), the widget is a text rundown by
day with the same counts and the tally. At ``minimal`` motion glass shows the
summary as a still, and the tally shows at once.

Feed it from ``Page.update_snapshot`` with ``set_week(snapshot.week())``.
``ships`` maps the app's own ``Event.kind`` words to the scene's five ship
classes (``SHIP_CLASSES``); events of other kinds fly no ship but count in the
tally as "other".
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from typing import TYPE_CHECKING

from textual.strip import Strip

from textual_glass.palette import RGB, hex_of, luminance, mix
from textual_glass.scenes import space
from textual_glass.widgets import BRIGHT, FAINT, SOFT, Part, PixelWidget, ellipsize, strip, tone

from ..model import Event, WeekStats, monday_of
from ..theme import PALETTE, now, source_color

if TYPE_CHECKING:
    from textual_glass.player import Showing

#: The scene's ship classes, one per kind of work it draws.
SHIP_CLASSES: tuple[str, ...] = tuple(space.KINDS)

#: Common ``Event.kind`` words and the ship class each one flies as. Other
#: kinds fly no ship and count as "other" in the tally.
SHIPS: dict[str, str] = {
    "merged": "merged", "merge": "merged", "pr merged": "merged",
    "reviewed": "review", "review": "review", "approved": "review",
    "deployed": "deploy", "deploy": "deploy", "shipped": "deploy",
    "ticket closed": "ticket", "ticket": "ticket", "closed": "ticket", "resolved": "ticket",
    "released": "release", "release": "release",
}

#: How the tally names each ship class: (one, many).
NOUNS: dict[str, tuple[str, str]] = {
    "merged": ("merged", "merged"), "review": ("review", "reviews"), "deploy": ("deploy", "deploys"),
    "ticket": ("ticket", "tickets"), "release": ("release", "releases"),
}

#: Why a scene ended (glass's ``on_end`` reasons) that put the summary back at once.
PUT_BACK = ("resized", "theme", "moved")


def week_hours(when: datetime, monday: date) -> float:
    """``when`` as hours since 00:00 on ``monday``, by the local wall clock.

    The day is the local date's offset from ``monday``, and the hour is the local
    time of day, so an event at 23:30 on Sunday is at 167.5 hours even in a week
    whose clocks went back an hour (that week lasts 169 real hours)."""
    local = when.astimezone()
    return (local.date() - monday).days * 24 + local.hour + local.minute / 60 + local.second / 3600


def noun(ship: str, count: int) -> str:
    one, many = NOUNS.get(ship, (ship, ship))
    return one if count == 1 else many


@dataclass(frozen=True)
class ReplayWeek:
    """A week as the replay shows it: the events that fly ships, in order of time
    (a cue's index is a position in ``items``), and the ones that don't."""

    start: date  # the Monday
    label: str  # "this week", or "the week of Oct 19"
    days: int  # days of the week to list in the rundown, from Monday
    items: tuple[Event, ...]
    works: tuple[space.Work, ...]  # one per item: its ship class and hour
    others: tuple[Event, ...]  # events of kinds no ship flies for

    @classmethod
    def read(cls, week: WeekStats, ships: Mapping[str, str], this_monday: date | None = None) -> ReplayWeek:
        """The week's events, mapped to ships and converted to hours since its local Monday 00:00.
        Events outside the week are left out."""
        items: list[Event] = []
        works: list[space.Work] = []
        others: list[Event] = []
        for event in sorted(week.events, key=lambda e: e.when):
            hour = week_hours(event.when, week.start)
            if not 0 <= hour < 168:
                continue
            ship = ships.get(event.kind)
            if ship is None:
                others.append(event)
            else:
                items.append(event)
                works.append(space.Work(ship, hour))
        this = this_monday or monday_of(now().astimezone().date())
        label = "this week" if week.start == this else f"the week of {week.start:%b} {week.start.day}"
        return cls(week.start, label, max(1, min(7, week.elapsed)), tuple(items), tuple(works), tuple(others))

    def counts(self) -> Counter[str]:
        return Counter(work.kind for work in self.works)

    def empty_text(self) -> str:
        return "nothing landed yet this week" if self.label == "this week" else f"nothing landed in {self.label}"


class WeekReplay(PixelWidget, can_focus=True):
    """The week in review: a ship for each piece of work, then the lights they
    left, held as the week's summary. A text rundown without glass.

    Args:
        look: One of ``LOOKS`` (glass's ``space.LOOKS``).
        ships: ``Event.kind`` -> ship class (one of ``SHIP_CLASSES``); default :data:`SHIPS`.
            Kinds not in it fly no ship and count as "other" in the tally.
        length: About how many seconds of ships the show lasts (glass's ``space.make``).

    It plays when it becomes visible (its page shows), through
    ``app.glass.play_scene``, and again on :meth:`replay`. While it holds, new data
    puts the new summary up at once (no replay), and only when the set of items
    changed. A resize, a new theme or a move puts the summary back; hidden, it plays
    again the next time it shows.
    """

    DEFAULT_CSS = """
    WeekReplay { height: 1fr; }
    """

    #: The looks glass draws the week in.
    LOOKS: tuple[str, ...] = tuple(space.LOOKS)

    def __init__(self, look: str = "halo", ships: Mapping[str, str] | None = None, length: float = 20.0, *,
                 name: str | None = None, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.ships = dict(SHIPS if ships is None else ships)
        wrong = sorted({ship for ship in self.ships.values() if ship not in SHIP_CLASSES})
        if wrong:
            raise ValueError(f"unknown ship classes {wrong}: use {SHIP_CLASSES}")
        self.look = self._checked_look(look)
        self.length = float(length)
        self.week: ReplayWeek | None = None  # the latest data
        self._shown: ReplayWeek | None = None  # the data the current scene was made with
        self._showing: Showing | None = None
        self._token = 0  # the current scene's: callbacks of older scenes are ignored
        self._fresh = True  # play from the start next time (it was shown, or asked to replay)
        self._retry = True  # start a scene when it renders with glass on and none is up
        self._scheduled = False  # a play waits for the next refresh
        self._landed: set[int] = set()  # indices into _shown.items heard landing
        self._last: int | None = None  # the item that landed last
        self._done = False  # the show is over: the tally shows

    # -- what the app calls ------------------------------------------------------------------

    def set_week(self, week: WeekStats) -> None:
        """New data (``snapshot.week()``, or any ``WeekStats``). Equal data costs nothing.
        While a show plays, it plays on; once it is over, a changed set of items puts
        the new summary up at once."""
        new = ReplayWeek.read(week, self.ships)
        if new == self.week:
            return
        self.week = new
        if self._showing is not None and self._done and self._shown is not None and new.works != self._shown.works:
            self._start_soon()  # quietly: the new summary, no replay
        self.refresh()

    def replay(self) -> None:
        """Play the show again from the start (with glass; nothing to replay without)."""
        self._fresh = self._retry = True
        if self.glass_on:
            self._start_soon()
        self.refresh()

    def set_look(self, look: str) -> None:
        """Draw the week in another look (``LOOKS``), playing it from the start."""
        self.look = self._checked_look(look)
        self.replay()

    def next_look(self) -> str:
        """The look after this one, in ``LOOKS`` order."""
        return self.LOOKS[(self.LOOKS.index(self.look) + 1) % len(self.LOOKS)]

    @property
    def showing(self) -> Showing | None:
        """The scene over the widget (glass's ``Showing``), or None."""
        return self._showing

    @property
    def done(self) -> bool:
        """The show is over (or shows its end): the tally is up."""
        return self._done

    @property
    def landed(self) -> int:
        """How many pieces of work the week has ships for."""
        return len(self.week.works) if self.week is not None else 0

    def tally(self) -> str:
        """The week's tally as plain text: "this week: 6 merged · 5 reviews · 1 release · 3 other"."""
        return _text(self._tally_parts(self._colours(sky=False)))

    @property
    def caption(self) -> str:
        """The line that follows the ships, as plain text: the latest arrival while the show plays,
        the week's newest piece of work ("latest: ...") once it is over; empty without a show."""
        if not self._show_mode():
            return ""
        return _text(self._caption_parts(self._colours(sky=True), self.size.width or 999))

    @property
    def status(self) -> str:
        """The bottom line under the show, as plain text: how many have landed, then the tally."""
        if not self._show_mode():
            return ""
        return _text(self._status_parts(self._colours(sky=True), self.size.width or 999))

    # -- playing -----------------------------------------------------------------------------

    def on_show(self) -> None:
        """Its page shows (or it scrolled into view): play from the start."""
        self._fresh = self._retry = True
        self._start_soon()

    def _start_soon(self) -> None:
        """Play once the layout has settled: from the start if it was just shown or asked
        to replay, else its end at once (``start=math.inf``)."""
        if self._scheduled:
            return
        self._scheduled = True
        self.call_after_refresh(self._play_now)

    def _play_now(self) -> None:
        self._scheduled = False
        if self.is_attached:
            self._play(0.0 if self._fresh else math.inf)

    def _play(self, start: float) -> None:
        engine = self.glass
        week = self.week
        self._token += 1  # whatever showed before is now stale (glass ends it as "replaced")
        if week is None or engine is None or not engine.active:
            self._showing = None
            self.refresh()
            return
        token = self._token
        make = partial(space.make, week=list(week.works), look=self.look, length=self.length)
        show = engine.play_scene(make, at=self, start=start, on_cue=partial(self._cue, token),
                                 on_end=partial(self._ended, token))
        self._showing = show
        self._landed = set()
        self._last = None
        if show is None:  # no room, or not on screen: the rundown, until it is shown again
            self._retry = False
            self._done = False
            self.refresh()
            return
        self._shown = week
        self._fresh = False
        self._done = start == math.inf or show.stage.still or not week.works
        self.refresh()

    def _cue(self, token: int, data: Hashable) -> None:
        """A ship landed (``("landed", i, kind)``, ``i`` an index into the week it was made
        with), or the show is over (``space.END``)."""
        shown = self._shown
        if token != self._token or shown is None:
            return
        if isinstance(data, tuple) and len(data) >= 2 and data[0] == "landed" and isinstance(data[1], int):
            if 0 <= data[1] < len(shown.items):
                self._landed.add(data[1])
                self._last = data[1]
        elif data != space.END:
            return
        if not self._done and (data == space.END or len(self._landed) >= len(shown.items)):
            self._done = True
            week = self.week
            if week is not None and week.works != shown.works:
                self._start_soon()  # data changed during the show: its summary now
        self.refresh()

    def _ended(self, token: int, reason: str) -> None:
        if token != self._token:
            return  # an older scene, replaced
        self._showing = None
        if reason in PUT_BACK:
            self._start_soon()  # the same summary at the new size or in the new colours
        elif reason == "hidden":
            self._fresh = True  # plays again the next time it shows
        elif reason in ("failed", "stopped"):
            self._retry = False  # the rundown, until it is shown again or replayed
        self.refresh()  # "off": the rundown

    def _show_mode(self) -> bool:
        """A scene is over the widget, or about to be."""
        return self.glass_on and self.week is not None and (self._showing is not None or self._scheduled)

    @staticmethod
    def _checked_look(look: str) -> str:
        if look not in space.LOOKS:
            raise ValueError(f"unknown look {look!r}: use one of {tuple(space.LOOKS)}")
        return look

    # -- text --------------------------------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        width, height = self.size
        if (self._retry and self._showing is None and not self._scheduled and self.week is not None
                and self.glass_on):
            self._start_soon()  # glass came on, or the data came, while it shows
        if self._show_mode():  # the show fills the widget: a caption and a status line at its foot
            if y == height - 1:
                return strip(_centred(self._status_parts(self._colours(sky=True), width), width), width)
            if y == height - 2:
                return strip(_centred(self._caption_parts(self._colours(sky=True), width), width), width)
            return Strip.blank(width)
        lines = self._rundown(self._colours(sky=False), height)
        return strip([(" ", ""), *lines[y]] if y < len(lines) else [], width)

    def _colours(self, sky: bool) -> _Colours:
        """Text colours: light on dark over the show (its night sky covers the widget even on a
        light theme) and on a dark theme; the theme's own text colours on a light one."""
        palette = self.theme_palette
        light = sky or palette.dark

        def ship(kind: str) -> str:
            role = getattr(space, "ROLE", {}).get(kind, "foreground")
            colour: RGB = getattr(palette, role, palette.foreground)
            return hex_of(_lifted(colour) if light and not palette.dark else colour)

        if light:
            return _Colours(PALETTE["text_bright"], PALETTE["text"], PALETTE["text_soft"], PALETTE["text_faint"], ship)
        return _Colours(tone(palette, BRIGHT), tone(palette), tone(palette, SOFT), tone(palette, FAINT), ship)

    def _tally_parts(self, c: _Colours, width: int | None = None) -> list[Part]:
        """The tally; within ``width`` it drops its "this week:" first."""
        week = self.week
        if week is None:
            return [("waiting for the first fetch…", c.soft)]
        counts = week.counts()
        bits: list[Part] = [(f"{counts[k]} {noun(k, counts[k])}", f"bold {c.ship(k)}") for k in SHIP_CLASSES
                            if counts[k]]
        if week.others:
            bits.append((f"{len(week.others)} other", c.soft))
        if not week.works:
            return [(week.empty_text(), c.text), *(p for b in bits for p in ((" · ", c.faint), b))]
        out: list[Part] = []
        for n, bit in enumerate(bits):
            out += [(" · ", c.faint), bit] if n else [bit]
        label = (f"{week.label}: ", c.soft)
        return out if width is not None and _length([label, *out]) > width else [label, *out]

    def _caption_parts(self, c: _Colours, width: int) -> list[Part]:
        """The latest arrival while the show plays; once it is over, the week's newest piece of work."""
        week, shown = self.week, self._shown
        if self._done:
            if week is None or not week.items:
                return []
            return [("latest: ", c.soft), *_arrival(week.items[-1], week.works[-1].kind, c, width - 8)]
        if shown is None or self._last is None:
            return []
        return _arrival(shown.items[self._last], shown.works[self._last].kind, c, width)

    def _status_parts(self, c: _Colours, width: int) -> list[Part]:
        """How many have landed while the show plays, the tally once it is over."""
        shown = self._shown
        if self._done or shown is None:
            return self._tally_parts(c, width)
        return [(f"{shown.label} · {len(self._landed)} of {len(shown.items)} landed", c.soft)]

    def _rundown(self, c: _Colours, height: int) -> list[list[Part]]:
        """Without a show: the week by day, counts by ship class (and "other"), then the tally."""
        week = self.week
        if week is None or (not week.works and not week.others):
            return [self._tally_parts(c)]
        counts = week.counts()
        columns = [k for k in SHIP_CLASSES if counts[k]] + (["other"] if week.others else [])
        words = {k: noun(k, 2) if k != "other" else "other" for k in columns}
        header: list[Part] = [(" " * 7, "")]
        for k in columns:
            header.append((words[k].rjust(len(words[k]) + 2), f"bold {c.ship(k)}" if k != "other" else c.soft))
        per_day: list[Counter[str]] = [Counter() for _ in range(week.days)]
        for work in week.works:
            if work.day < week.days:
                per_day[work.day][work.kind] += 1
        for event in week.others:
            offset = int(week_hours(event.when, week.start) // 24)
            if offset < week.days:
                per_day[offset]["other"] += 1
        rows: list[list[Part]] = []
        for offset, day_counts in enumerate(per_day):
            day = date.fromordinal(week.start.toordinal() + offset)
            row: list[Part] = [(f"{day:%a} {day.day:<2} ", c.soft)]
            for k in columns:
                n = day_counts[k]
                row.append((str(n if n else "·").rjust(len(words[k]) + 2), c.text if n else c.faint))
            rows.append(row)
        tail = [self._tally_parts(c, self.size.width - 1)]
        if week.others:
            kinds = Counter(e.kind for e in week.others).most_common()
            tail.append([("other: ", c.faint), (" · ".join(f"{n} {kind}" for kind, n in kinds), c.faint)])
        if 1 + len(rows) + 1 + len(tail) <= height:
            return [header, *rows, [], *tail]
        room = height - 1 - len(tail)  # short: no blank line, and the latest days
        return [header, *rows[-room:], *tail] if room > 0 else tail[:max(1, height)]


@dataclass(frozen=True)
class _Colours:
    bright: str
    text: str
    soft: str
    faint: str
    ship: Callable[[str], str]  # a ship class's colour


def _lifted(colour: RGB, floor: float = 0.62) -> RGB:
    """A theme colour made light enough to read on a night sky."""
    lum = luminance(colour)
    if lum >= floor:
        return colour
    return mix(colour, (255, 255, 255), (floor - lum) / (1 - lum))


def _length(parts: Sequence[Part]) -> int:
    return sum(len(text) for text, _ in parts)


def _text(parts: Sequence[Part]) -> str:
    return "".join(text for text, _ in parts)


def _arrival(event: Event, ship: str, c: _Colours, width: int) -> list[Part]:
    """"Tue 14:02 · merged · fix the flaky test (GH)", its title cut to fit ``width``."""
    local = event.when.astimezone()
    parts = [(f"{local:%a %H:%M}", c.soft), (" · ", c.faint), (event.kind, f"bold {c.ship(ship)}"),
             (" · ", c.faint), (event.title, c.bright), (f" ({event.source})", source_color(event.source))]
    over = _length(parts) - width
    if over > 0:
        parts[4] = (ellipsize(event.title, max(1, len(event.title) - over)), c.bright)
    return parts


def _centred(parts: Sequence[Part], width: int) -> list[Part]:
    """Parts centred in ``width`` (``strip`` crops a line that is still too long)."""
    return [(" " * max(0, (width - _length(parts)) // 2), ""), *parts]


__all__ = ["NOUNS", "SHIPS", "SHIP_CLASSES", "ReplayWeek", "WeekReplay", "noun", "week_hours"]
