"""The data layer: plain, typed, immutable records.

Sources (see :mod:`textual_orbital.provider`) produce these; widgets only read
them. Every timestamp is a timezone-aware ``datetime``; ages are computed at
render time, so a record never has to change just because time passed.

The records are generic. An app keeps its own typed record in ``data`` (on
:class:`Entity`, :class:`AttentionItem` and :class:`TrackItem`): it takes part
in equality, so a change in it is a content change, and the library never looks
inside it. Read it back typed in your rules and views (``cast(MyRecord, item.data)``).
Plain numbers for gauges (a disk's fill, a queue depth) travel as named
:class:`Metrics` instead (``snapshot.metric("disk")``).

Source keys are short strings: ``CC`` Claude Code and ``GH`` GitHub have names
and colours by default; any other key works too.
"""

from __future__ import annotations

from collections.abc import Container, Hashable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum

# --------------------------------------------------------------------- sources


class Health(str, Enum):
    """How fresh a source's data is."""

    PENDING = "pending"  # not fetched yet
    FRESH = "fresh"
    STALE = "stale"      # the last good fetch is older than ``stale_after``
    FAILING = "failing"  # the last attempt raised


@dataclass(frozen=True)
class SourceStatus:
    """The state of one data source, as shown by the header's health dots.

    Attributes:
        key: The source's key.
        name: Its name.
        interval: Seconds between fetches while it works.
        last_ok: When the last good fetch ended.
        last_attempt: When the last fetch ended, good or not (or timed out).
        error: Why the last attempt failed, or None.
        items: How many records the last good fetch returned (events included).
        failures: Failed attempts in a row (timeouts included); 0 after a good one.
            The app waits ``interval * 2 ** failures`` before the next one, up to a cap.
        next_poll: When the next fetch is due, or None when none is scheduled
            (a fetch is hung; see ``OrbitalApp.HUNG_LIMIT``).
    """

    key: str
    name: str
    interval: float
    last_ok: datetime | None = None
    last_attempt: datetime | None = None
    error: str | None = None
    items: int = 0
    failures: int = 0
    next_poll: datetime | None = None

    @property
    def stale_after(self) -> timedelta:
        return timedelta(seconds=max(30.0, self.interval * 3))

    def health(self, now: datetime) -> Health:
        if self.error is not None:
            return Health.FAILING
        if self.last_ok is None:
            return Health.PENDING
        if now - self.last_ok > self.stale_after:
            return Health.STALE
        return Health.FRESH


def describe_error(error: BaseException) -> str:
    """An exception as one line, as :class:`ErrorReport` keeps it: its type and
    the first line of its text."""
    text = str(error).strip()
    return f"{type(error).__name__}: {text.splitlines()[0]}"[:200] if text else type(error).__name__


@dataclass(frozen=True)
class ErrorReport:
    """An exception the app caught on its own thread (in a hook, a rule, a page,
    a callback from a thread) and reported with ``OrbitalApp.report_error``.

    Attributes:
        at: When it last happened.
        where: What raised (``"rules"``, ``"OverviewPage.update_snapshot"``...).
        message: The exception's type and the first line of its text.
        count: How many times it has happened (the same ``where`` and ``message``).
    """

    at: datetime
    where: str
    message: str
    count: int = 1


# -------------------------------------------------------------------- entities


class EntityState(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    WAITING = "waiting"  # needs you: a permission prompt, a question, an approval
    ERROR = "error"
    DONE = "done"


#: Order entities by urgency: waiting first, then errors, working, idle, done.
ENTITY_URGENCY = {EntityState.WAITING: 0, EntityState.ERROR: 1, EntityState.WORKING: 2,
                  EntityState.IDLE: 3, EntityState.DONE: 4}


@dataclass(frozen=True)
class Entity:
    """Something that works on its own and sometimes needs you (a Claude Code
    session, a build agent, a job). Shown as a beacon.

    Attributes:
        id: Stable id.
        name: Short name (the branch, task or host).
        where: Repo, directory or cluster.
        state: What it is doing.
        since: When it entered ``state`` (drives "waiting 3m"; a new wait gets a new ``since``).
        reason: Why it is waiting or failing ("permission prompt").
        detail: Longer text for the detail view.
        url: Something ``o`` can open.
        data: The app's own frozen record; compared, never read by the library.
    """

    id: str
    name: str
    where: str = ""
    state: EntityState = EntityState.IDLE
    since: datetime | None = None
    reason: str = ""
    detail: str = ""
    url: str | None = None
    data: Hashable | None = None


# ------------------------------------------------------------------- attention


@dataclass(frozen=True)
class AttentionItem:
    """One row of the needs-attention queue (or the be-aware list).

    Attributes:
        key: Stable id (used for selection, snooze and dedupe).
        source: Source key (``GH``...).
        score: 0-10, higher is more urgent. Drives order and colour.
        why: Short verb phrase: "review", "respond", "unblock"...
        title: What it is about.
        where: Repo, list or project.
        detail: One line of context ("jdoe requested changes").
        since: When it started needing attention (age is computed from it).
        section: ``"attention"`` or ``"aware"``.
        url: Opened by ``o``.
        ref: The record this item is about: ``("entity", id)`` or ``("track", key)``.
        data: The app's own frozen record; compared, never read by the library.
    """

    key: str
    source: str
    score: int
    why: str
    title: str
    where: str = ""
    detail: str = ""
    since: datetime | None = None
    section: str = "attention"
    url: str | None = None
    ref: tuple[str, str] | None = None
    data: Hashable | None = None


# ----------------------------------------------------------------------- track


class TrackHealth(str, Enum):
    """The colour of a track item's dot."""

    IDLE = "idle"        # parked (a draft): never stuck
    PENDING = "pending"  # waiting on something outside (checks running, a review outstanding)
    OK = "ok"            # nothing is blocking it
    WARN = "warn"        # needs work (changes requested)
    FAIL = "fail"        # broken (checks failed): always stuck


@dataclass(frozen=True)
class TrackItem:
    """Something moving through stages (a pull request, a release, a deploy).

    ``stage`` is an index into the stage names the ``Track`` widget is given;
    the app derives ``stage`` and ``health`` from its own record.

    Attributes:
        key: Stable id.
        source: Source key.
        title: What it is.
        label: A short handle ("repo#412", "v2.3.1").
        stage: Index of its current stage.
        health: Its dot's colour.
        stage_since: When it entered ``stage`` (drives the trail and "stuck").
        opened_at: When it started (used when ``stage_since`` is unknown).
        url: Opened by ``o``.
        detail: One line of status for tables and captions.
        data: The app's own frozen record; compared, never read by the library.
    """

    key: str
    source: str
    title: str
    label: str = ""
    stage: int = 0
    health: TrackHealth = TrackHealth.OK
    stage_since: datetime | None = None
    opened_at: datetime | None = None
    url: str | None = None
    detail: str = ""
    data: Hashable | None = None

    def stuck(self, now: datetime, after: timedelta = timedelta(days=2)) -> bool:
        """Failing, or in one stage for longer than ``after`` (``IDLE`` never counts)."""
        if self.health is TrackHealth.IDLE:
            return False
        if self.health is TrackHealth.FAIL:
            return True
        since = self.stage_since or self.opened_at
        return since is not None and now - since > after


# ------------------------------------------------------------------------ week


@dataclass(frozen=True)
class Event:
    """Something that happened. Week stats are counted from these by ``kind``."""

    when: datetime
    kind: str  # your own vocabulary: "merged", "deployed", "ticket closed"...
    source: str
    title: str
    where: str = ""
    url: str | None = None
    data: Hashable | None = None  # the app's own record (line counts, say); the library never looks inside


@dataclass(frozen=True)
class DayCount:
    """One day of :class:`WeekStats`: the date and its event counts by kind."""

    day: date
    counts: dict[str, int]

    def __hash__(self) -> int:
        return hash((self.day, tuple(sorted(self.counts.items()))))


@dataclass(frozen=True)
class WeekStats:
    """This week (Monday to today) against the same days of last week."""

    start: date                     # this week's Monday
    days: tuple[DayCount, ...]      # Monday .. Friday, or Monday .. today at the weekend
    previous: tuple[DayCount, ...]  # the same weekdays, last week
    events: tuple[Event, ...]       # this week's, newest first
    elapsed: int = 5                # days of this week so far (today included)

    def total(self, kind: str) -> int:
        return sum(d.counts.get(kind, 0) for d in self.days)

    def previous_total(self, kind: str) -> int:
        """Last week's count over the same weekdays as this week so far."""
        return sum(d.counts.get(kind, 0) for d in self.previous[:self.elapsed])

    def delta(self, kind: str) -> int:
        return self.total(kind) - self.previous_total(kind)

    def series(self, kind: str) -> tuple[int, ...]:
        return tuple(d.counts.get(kind, 0) for d in self.days)

    def previous_series(self, kind: str) -> tuple[int, ...]:
        return tuple(d.counts.get(kind, 0) for d in self.previous)


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_stats(events: Sequence[Event], today: date) -> WeekStats:
    """Count ``events`` into this week's and last week's per-day totals.

    The week runs Monday to Friday (to today at the weekend); days after
    ``today`` are empty slots. ``elapsed`` days are compared with last week.
    """
    start = monday_of(today)
    days_shown = max(5, today.weekday() + 1)
    this_week: list[dict[str, int]] = [{} for _ in range(days_shown)]
    last_week: list[dict[str, int]] = [{} for _ in range(days_shown)]
    mine = []
    for event in events:
        d = event.when.astimezone().date() if event.when.tzinfo else event.when.date()
        offset = (d - start).days
        if 0 <= offset < days_shown:
            slot = this_week[offset]
            mine.append(event)
        elif -7 <= offset < -7 + days_shown:
            slot = last_week[offset + 7]
        else:
            continue
        slot[event.kind] = slot.get(event.kind, 0) + 1
    mine.sort(key=lambda e: e.when, reverse=True)
    return WeekStats(
        start=start,
        days=tuple(DayCount(start + timedelta(days=i), c) for i, c in enumerate(this_week)),
        previous=tuple(DayCount(start - timedelta(days=7 - i), c) for i, c in enumerate(last_week)),
        events=tuple(mine),
        elapsed=min(days_shown, today.weekday() + 1),
    )


# ----------------------------------------------------------------------- goals


@dataclass(frozen=True)
class Goal:
    """A larger piece of work that tasks belong to (an epic, a milestone)."""

    id: str
    name: str
    owner: str = ""
    progress: float = 0.0  # 0..1
    url: str | None = None
    due: date | None = None


@dataclass(frozen=True)
class Task:
    """A task in progress. ``group`` is the list it lives in ("Big Work"...)."""

    id: str
    title: str
    assignee: str = ""
    status: str = "in progress"  # in progress | in review | blocked | ready
    group: str = ""
    status_since: datetime | None = None
    url: str | None = None
    goal_id: str | None = None


@dataclass(frozen=True)
class DoneTask:
    id: str
    title: str
    done_at: datetime
    status: str = "done"
    url: str | None = None


# ----------------------------------------------------------------------- alerts

#: How an alert shows: a popup that takes the keyboard, or a toast in a corner that goes away by itself.
ALERT_STYLES = ("modal", "toast")


@dataclass(frozen=True)
class Alert:
    """An "incoming transmission": a big popup that wants you now.

    Attributes:
        key: Dedupe key: an alert with the same key is not raised again while
            it is showing, queued or snoozed.
        kind: Your own name for the kind ("waiting", "release failed"...).
            ``AlertRules`` picks its picture by it.
        title: The big line.
        body: Lines under it.
        priority: Higher shows first (``WaitingRule`` uses 100).
        color: Accent colour (hex), or None for the default.
        image: A picture or animated GIF to show (a path), or None for the
            drawn emblem.
        url: Opened by ``o`` from the popup.
        target: What ``enter`` jumps to: ``("entity", id)``, ``("track", key)``,
            ``("item", key)`` or None.
        notify: Also send a desktop notification and badge the window title.
        raised_at: Set when it is queued.
        headline: The small line over the title; see :attr:`heading`.
        from_rule: Raised by a rule, so it is dropped when the rule's condition
            goes away. ``AlertRules`` sets it; one-off alerts leave it False.
        style: ``"modal"`` (a popup) or ``"toast"`` (a corner toast that goes
            away by itself), or None for the app's ``alert_style``.
    """

    key: str
    kind: str
    title: str
    body: tuple[str, ...] = ()
    priority: int = 50
    color: str | None = None
    image: str | None = None
    url: str | None = None
    target: tuple[str, str] | None = None
    notify: bool = True
    raised_at: datetime | None = None
    headline: str = ""
    from_rule: bool = False
    style: str | None = None

    def __post_init__(self) -> None:
        if self.style is not None and self.style not in ALERT_STYLES:
            raise ValueError(f"Alert.style must be one of {ALERT_STYLES} or None, not {self.style!r}")

    @property
    def heading(self) -> str:
        return self.headline or "INCOMING TRANSMISSION"


# --------------------------------------------------------------------- snapshot


class Metrics(Mapping[str, float]):
    """Named numbers from the sources (``{"disk": 0.42}``): a read-only,
    hashable mapping, so a snapshot that holds one stays comparable."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, float] | Iterable[tuple[str, float]] = ()) -> None:
        self._values = dict(values)

    def __getitem__(self, name: str) -> float:
        return self._values[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __hash__(self) -> int:
        return hash(frozenset(self._values.items()))

    def __repr__(self) -> str:
        return f"Metrics({self._values!r})"


@dataclass(frozen=True)
class Snapshot:
    """Everything the dashboard shows, merged from every source.

    ``taken_at`` is when it was taken: the end of the fetch it was merged after,
    or the moment items were snoozed or woken or the date changed. Widgets take
    their "now" from it.

    ``metrics`` merges every source's metrics in source order (a later source's
    value wins for a name two sources share, so give them distinct names).
    """

    taken_at: datetime
    entities: tuple[Entity, ...] = ()
    attention: tuple[AttentionItem, ...] = ()
    track: tuple[TrackItem, ...] = ()
    events: tuple[Event, ...] = ()
    goals: tuple[Goal, ...] = ()
    tasks: tuple[Task, ...] = ()
    done: tuple[DoneTask, ...] = ()
    sources: tuple[SourceStatus, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=Metrics)

    def __post_init__(self) -> None:
        if not isinstance(self.metrics, Metrics):  # a plain dict is copied into a read-only, hashable one
            object.__setattr__(self, "metrics", Metrics(self.metrics))

    def metric(self, name: str, default: float = 0.0) -> float:
        """A metric's value, or ``default`` when no source reports it."""
        return self.metrics.get(name, default)

    @property
    def needs_attention(self) -> tuple[AttentionItem, ...]:
        return tuple(i for i in self.attention if i.section == "attention")

    @property
    def be_aware(self) -> tuple[AttentionItem, ...]:
        return tuple(i for i in self.attention if i.section != "attention")

    def entity(self, entity_id: str) -> Entity | None:
        return next((e for e in self.entities if e.id == entity_id), None)

    def track_item(self, key: str) -> TrackItem | None:
        return next((t for t in self.track if t.key == key), None)

    def item(self, key: str) -> AttentionItem | None:
        return next((i for i in self.attention if i.key == key), None)

    def week(self, on: date | None = None) -> WeekStats:
        """This week (``taken_at``'s, Monday to today), or the week that holds
        ``on``: another week is counted whole, Monday to Sunday, against the
        week before it. Counted from ``events``, so the snapshot must hold
        both weeks; for older ones, fetch their events on demand
        (``OrbitalApp.fetch``) and count them with ``replace(snapshot, events=...).week(on)``."""
        today = self.taken_at.astimezone().date()
        if on is None or monday_of(on) == monday_of(today):
            return week_stats(self.events, today)
        return week_stats(self.events, monday_of(on) + timedelta(days=6))


#: The queue load that fills the load gauge.
QUEUE_CAPACITY = 60


def queue_load(snapshot: Snapshot, snoozed: Container[str] = frozenset()) -> tuple[int, int]:
    """The queue's load (the sum of the scores of unsnoozed needs-attention
    items) and the load that fills the gauge."""
    load = sum(i.score for i in snapshot.needs_attention if i.key not in snoozed)
    return load, QUEUE_CAPACITY
