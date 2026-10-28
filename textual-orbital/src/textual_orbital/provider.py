"""The data seam: sources, providers, and the store that merges them.

To wire in real data, write one :class:`Source` per system and hand them to
the app in a :class:`Provider`:

.. code-block:: python

    class GitHubSource:
        key, name, interval = "GH", "GitHub", 60.0

        def fetch(self) -> SourceData:          # runs in a worker thread
            prs = [...]                          # e.g. from `gh pr list --json ...`
            return SourceData(track=[to_track_item(p) for p in prs], attention=[...], events=[...])

    provider = StaticProvider([ClaudeCodeSource(), GitHubSource(), ...])

``fetch`` may block (subprocesses, HTTP); each fetch runs in a thread of its
own, off the UI thread. If it raises, the source turns red in the header (the
reason shows on ``h``) and its last good data stays on screen. A fetch that
takes longer than its deadline (:func:`fetch_timeout`) turns the source red
too, and its late result is ignored. A field left as ``None`` in
:class:`SourceData` means "this source doesn't report that"; an empty list
(or ``{}`` for ``metrics``) means "it reports none".

:meth:`Store.apply` rejects data that would break the merged snapshot, and
fails that source instead: a naive ``datetime`` in any record, or a key (an
attention item's or track item's ``key``, an entity's ``id``) that another
source already uses.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime
from typing import Protocol, runtime_checkable

from .model import AttentionItem, DoneTask, Entity, Event, Goal, Metrics, Snapshot, SourceStatus, Task, TrackItem
from .theme import SOURCE_NAMES, now


@dataclass
class SourceData:
    """What one fetch returns. Each field replaces that source's previous value.

    ``metrics`` are named numbers for gauges (``{"disk": 0.42}``). Round each
    to what the screen shows: a change in any value is a content change, so
    noise in the last digits would redraw the dashboard on every fetch.
    """

    entities: Sequence[Entity] | None = None
    attention: Sequence[AttentionItem] | None = None
    track: Sequence[TrackItem] | None = None
    events: Sequence[Event] | None = None
    goals: Sequence[Goal] | None = None
    tasks: Sequence[Task] | None = None
    done: Sequence[DoneTask] | None = None
    metrics: Mapping[str, float] | None = None

    def count(self) -> int:
        """How many records this fetch returned (every field but ``metrics``)."""
        return sum(len(getattr(self, name) or ()) for name in RECORD_FIELDS)


#: The fields of :class:`SourceData` that hold records (all but ``metrics``).
RECORD_FIELDS = tuple(name for name in SourceData.__dataclass_fields__ if name != "metrics")
#: The fields whose records must have keys unique across sources, and the key attribute.
KEYED_FIELDS = (("entities", "id"), ("attention", "key"), ("track", "key"))


@runtime_checkable
class Source(Protocol):
    """One system to poll. ``interval`` is in seconds (Claude Code 10, others 60).

    ``fetch()`` runs in a thread of its own and may block, but it must set its
    own timeouts on every network call and subprocess (``timeout=`` on
    ``subprocess.run`` and on HTTP requests). A thread can't be stopped from
    outside: a call with no timeout that hangs (the laptop slept mid-request and
    the socket died without an error) keeps its thread forever.

    A source may also have a ``timeout`` attribute: the fetch's deadline in
    seconds (by default ``max(2 * interval, 10)``, see :func:`fetch_timeout`).
    When a fetch passes it, the source turns red ("timed out after 20 s") and
    is polled again later; the late result is ignored.

    Record keys must be unique across sources (attention and track ``key``,
    entity ``id``): prefix them with the source key (``"GH:412"``).
    """

    key: str
    name: str
    interval: float

    def fetch(self) -> SourceData: ...


#: The longest wait between fetches of a failing source, in seconds (unless its
#: ``interval`` is longer).
BACKOFF_CAP = 15 * 60.0


def fetch_timeout(source: Source) -> float:
    """A fetch's deadline in seconds: ``source.timeout`` if it has one, else
    ``max(2 * interval, 10)``."""
    timeout = getattr(source, "timeout", None)
    return float(timeout) if timeout is not None else max(2.0 * float(source.interval), 10.0)


def poll_delay(interval: float, failures: int) -> float:
    """Seconds until the next fetch, counted from the end of the last one:
    ``interval`` after a good fetch, doubled for each failure in a row (timeouts
    included), up to ``max(interval, BACKOFF_CAP)``."""
    return min(interval * 2.0 ** min(failures, 32), max(interval, BACKOFF_CAP))


@runtime_checkable
class Provider(Protocol):
    """What the app polls: a set of sources."""

    def sources(self) -> Sequence[Source]: ...


class StaticProvider:
    """A provider that is just a list of sources."""

    def __init__(self, sources: Sequence[Source]) -> None:
        self._sources = list(sources)

    def sources(self) -> Sequence[Source]:
        return self._sources


@dataclass
class _Entry:
    source: Source
    data: SourceData = field(default_factory=SourceData)
    status: SourceStatus | None = None
    fetch_ms: float = 0.0


class Store:
    """Keeps each source's latest data and merges it into one :class:`Snapshot`.

    ``content_version`` changes only when the merged content changes, so a
    poll that returns the same data costs nothing downstream.
    """

    def __init__(self, sources: Sequence[Source]) -> None:
        self._entries: dict[str, _Entry] = {}
        for source in sources:
            status = SourceStatus(source.key, source.name or SOURCE_NAMES.get(source.key, source.key),
                                  float(source.interval))
            self._entries[source.key] = _Entry(source, status=status)
        self.content_version = 0
        self._content: tuple[object, ...] | None = None

    @property
    def sources(self) -> list[Source]:
        return [e.source for e in self._entries.values()]

    def apply(self, key: str, data: SourceData, at: datetime | None = None, fetch_ms: float = 0.0) -> bool:
        """Merge one good fetch of source ``key``. Returns True if it was merged.

        Data that would break the merged snapshot is not merged: the source
        fails instead (as with :meth:`fail`, its last good data stays), and this
        returns False. That is a naive ``datetime`` in any record ("naive
        datetime in AttentionItem.since"), or a key that another source's
        current data already has ("duplicate key 'X' (also from GH): prefix
        keys with the source").
        """
        entry = self._entries[key]
        at = at or now()
        problem = naive_datetime(data) or self._duplicate_key(key, data)
        if problem is not None:
            self.fail(key, problem, at)
            return False
        merged = SourceData(**{name: (getattr(data, name) if getattr(data, name) is not None
                                      else getattr(entry.data, name))
                               for name in SourceData.__dataclass_fields__})
        entry.data = merged
        entry.fetch_ms = fetch_ms
        entry.status = replace(self.status(key), last_ok=at, last_attempt=at, error=None, items=data.count(),
                               failures=0)
        self._recompute()
        return True

    def fail(self, key: str, error: str, at: datetime | None = None) -> None:
        """Source ``key`` failed (or timed out): it turns red with ``error`` and
        keeps its last good data."""
        status = self.status(key)
        self._entries[key].status = replace(status, last_attempt=at or now(), error=error,
                                            failures=status.failures + 1)

    def schedule(self, key: str, at: datetime | None) -> None:
        """Record when source ``key`` is fetched next (None: nothing is scheduled)."""
        self._entries[key].status = replace(self.status(key), next_poll=at)

    def status(self, key: str) -> SourceStatus:
        status = self._entries[key].status
        assert status is not None
        return status

    def _duplicate_key(self, key: str, data: SourceData) -> str | None:
        """A record key in ``data`` that another source's current data already has."""
        for name, attr in KEYED_FIELDS:
            records = getattr(data, name)
            if not records:
                continue
            mine = {getattr(record, attr) for record in records}
            for other_key, other in self._entries.items():
                if other_key == key:
                    continue
                for record in getattr(other.data, name) or ():
                    if getattr(record, attr) in mine:
                        return (f"duplicate key '{getattr(record, attr)}' (also from {other_key}): "
                                "prefix keys with the source")
        return None

    def statuses(self) -> tuple[SourceStatus, ...]:
        """Every source's status, in source order."""
        return tuple(e.status for e in self._entries.values() if e.status is not None)

    def _gather(self) -> SourceData:
        """Every source's data, concatenated field by field; metrics merged in source order."""
        merged: dict[str, list[object]] = {name: [] for name in RECORD_FIELDS}
        metrics: dict[str, float] = {}
        for entry in self._entries.values():
            for name, values in merged.items():
                value = getattr(entry.data, name)
                if value:
                    values.extend(value)
            metrics.update(entry.data.metrics or {})
        return SourceData(**merged, metrics=metrics)  # type: ignore[arg-type]

    @staticmethod
    def _content_key(data: SourceData) -> tuple[object, ...]:
        return (*(tuple(getattr(data, name) or ()) for name in RECORD_FIELDS),
                tuple(sorted((data.metrics or {}).items())))

    def _recompute(self) -> None:
        content = self._content_key(self._gather())
        if content != self._content:
            self._content = content
            self.content_version += 1

    def snapshot(self, at: datetime | None = None) -> Snapshot:
        data = self._gather()
        attention = sorted(data.attention or (), key=lambda i: (-i.score, i.since or now()))
        return Snapshot(
            taken_at=at or now(),
            entities=tuple(data.entities or ()),
            attention=tuple(attention),
            track=tuple(data.track or ()),
            events=tuple(data.events or ()),
            goals=tuple(data.goals or ()),
            tasks=tuple(data.tasks or ()),
            done=tuple(data.done or ()),
            sources=self.statuses(),
            metrics=Metrics(data.metrics or {}),
        )


_DATETIME_FIELDS: dict[type, tuple[str, ...]] = {}


def naive_datetime(data: SourceData) -> str | None:
    """The first naive ``datetime`` in any record of ``data``, as a reason
    ("naive datetime in AttentionItem.since"), or None. Every dataclass field of
    every record is checked; ``data`` (the app's own record) is not."""
    for name in RECORD_FIELDS:
        for record in getattr(data, name) or ():
            kind = type(record)
            names = _DATETIME_FIELDS.get(kind)
            if names is None:
                names = _DATETIME_FIELDS[kind] = (
                    tuple(f.name for f in fields(record) if f.name != "data") if is_dataclass(record) else ())
            for attr in names:
                value = getattr(record, attr)
                if isinstance(value, datetime) and value.utcoffset() is None:
                    return f"naive datetime in {kind.__name__}.{attr}"
    return None


def timed_fetch(source: Source) -> tuple[SourceData | None, str | None, float]:
    """Run one fetch, catching errors: (data, error, milliseconds).

    Anything ``fetch()`` raises becomes the error, ``BaseException`` included
    (reused script code that calls ``sys.exit()``): the first line of an
    ``Exception``'s text, or ``"SystemExit: 2"`` for the others. Only
    ``KeyboardInterrupt`` on the main thread is raised again.
    """
    t0 = time.perf_counter()
    try:
        data = source.fetch()
        if not isinstance(data, SourceData):
            raise TypeError(f"{type(source).__name__}.fetch() returned {type(data).__name__}, not SourceData")
        return data, None, (time.perf_counter() - t0) * 1000
    except BaseException as error:  # noqa: BLE001 - any failure turns the source red
        if isinstance(error, KeyboardInterrupt) and threading.current_thread() is threading.main_thread():
            raise
        text = str(error).strip()
        message = text.splitlines()[0] if text else type(error).__name__
        if not isinstance(error, Exception):
            message = f"{type(error).__name__}: {message}" if text else message
        return None, message[:200], (time.perf_counter() - t0) * 1000
