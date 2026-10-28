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

``fetch`` may block (subprocesses, HTTP); it runs off the UI thread. If it
raises, the source turns red in the header (the reason shows on ``h``) and
its last good data stays on screen. A field left as ``None`` in
:class:`SourceData` means "this source doesn't report that"; an empty list
(or ``{}`` for ``metrics``) means "it reports none".
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
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
        return sum(len(v) for v in (self.entities, self.attention, self.track, self.goals, self.tasks, self.done)
                   if v is not None)


@runtime_checkable
class Source(Protocol):
    """One system to poll. ``interval`` is in seconds (Claude Code 10, others 60)."""

    key: str
    name: str
    interval: float

    def fetch(self) -> SourceData: ...


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

    def apply(self, key: str, data: SourceData, at: datetime | None = None, fetch_ms: float = 0.0) -> None:
        entry = self._entries[key]
        at = at or now()
        merged = SourceData(**{name: (getattr(data, name) if getattr(data, name) is not None
                                      else getattr(entry.data, name))
                               for name in SourceData.__dataclass_fields__})
        entry.data = merged
        entry.fetch_ms = fetch_ms
        assert entry.status is not None
        entry.status = replace(entry.status, last_ok=at, last_attempt=at, error=None, items=data.count())
        self._recompute()

    def fail(self, key: str, error: str, at: datetime | None = None) -> None:
        entry = self._entries[key]
        at = at or now()
        assert entry.status is not None
        entry.status = replace(entry.status, last_attempt=at, error=error)

    def statuses(self) -> tuple[SourceStatus, ...]:
        return tuple(e.status for e in self._entries.values() if e.status is not None)

    def _gather(self) -> SourceData:
        """Every source's data, concatenated field by field; metrics merged in source order."""
        merged: dict[str, list[object]] = {name: [] for name in SourceData.__dataclass_fields__ if name != "metrics"}
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
        return (*(tuple(getattr(data, name) or ()) for name in SourceData.__dataclass_fields__ if name != "metrics"),
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


def timed_fetch(source: Source) -> tuple[SourceData | None, str | None, float]:
    """Run one fetch, catching errors: (data, error, milliseconds)."""
    t0 = time.perf_counter()
    try:
        data = source.fetch()
        if not isinstance(data, SourceData):
            raise TypeError(f"{type(source).__name__}.fetch() returned {type(data).__name__}, not SourceData")
        return data, None, (time.perf_counter() - t0) * 1000
    except Exception as error:  # noqa: BLE001 - any failure turns the source red
        message = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
        return None, message[:200], (time.perf_counter() - t0) * 1000
