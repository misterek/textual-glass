"""Incoming transmissions: the alert queue and the rules that raise alerts.

* :class:`AlertQueue` holds alerts by priority, drops duplicates (same key),
  snoozes ("remind me in 10 minutes") and resolves rule alerts whose condition
  went away (you answered Claude in its own window, so the popup closes itself).
* :class:`AlertRules` runs a list of :class:`Rule` objects over each snapshot
  and raises an alert when a condition *starts*. Conditions that already hold
  when a source is first seen are a baseline, not news, except for the kinds in
  ``startup_kinds`` (by default only ``"waiting"``).

A rule is any object with ``conditions(snapshot)``, yielding a
:class:`Condition` for every alert-worthy situation that holds right now. The
library ships :class:`WaitingRule`; write your own for your records (read them
typed from ``data``). For a one-off, call ``app.raise_alert(Alert(...))``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol

from .model import Alert, EntityState, Snapshot
from .theme import PALETTE, now


def alert_color(alert: Alert) -> str:
    return alert.color or PALETTE["accent_2"]


class AlertQueue:
    """Alerts waiting to be shown, highest priority first.

    ``active`` is the one on screen. Raising an alert whose key is active,
    queued or snoozed does nothing (the queued copy is refreshed instead).
    """

    def __init__(self) -> None:
        self.active: Alert | None = None
        self._queue: list[Alert] = []
        self._snoozed: dict[str, tuple[datetime, Alert]] = {}

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def pending(self) -> list[Alert]:
        return list(self._queue)

    @property
    def snoozed(self) -> dict[str, datetime]:
        return {k: until for k, (until, _) in self._snoozed.items()}

    def push(self, alert: Alert) -> bool:
        """Queue an alert. False if it is a duplicate."""
        if alert.raised_at is None:
            alert = replace(alert, raised_at=now())
        if self.active is not None and self.active.key == alert.key:
            return False
        if alert.key in self._snoozed:
            return False
        for i, queued in enumerate(self._queue):
            if queued.key == alert.key:
                self._queue[i] = replace(alert, raised_at=queued.raised_at)
                return False
        self._queue.append(alert)
        self._queue.sort(key=lambda a: (-a.priority, a.raised_at or now()))
        return True

    def pop(self) -> Alert | None:
        """Take the next alert to show (it becomes ``active``)."""
        if self.active is not None or not self._queue:
            return None
        self.active = self._queue.pop(0)
        return self.active

    def dismiss(self) -> Alert | None:
        """The active alert was handled or closed."""
        alert, self.active = self.active, None
        return alert

    def snooze(self, duration: timedelta, at: datetime | None = None) -> Alert | None:
        """Hide the active alert for ``duration``; it comes back if still relevant."""
        alert, self.active = self.active, None
        if alert is not None:
            self._snoozed[alert.key] = ((at or now()) + duration, alert)
        return alert

    def resolve(self, live_keys: Iterable[str]) -> bool:
        """Drop queued and snoozed rule alerts whose condition has gone (one-off
        alerts stay). Returns True if the active alert's condition went away
        (the caller closes it)."""
        live = set(live_keys)
        self._queue = [a for a in self._queue if a.key in live or not a.from_rule]
        self._snoozed = {k: v for k, v in self._snoozed.items() if k in live or not v[1].from_rule}
        active = self.active
        return active is not None and active.from_rule and active.key not in live

    def wake(self, at: datetime | None = None) -> int:
        """Move snoozed alerts whose time is up back into the queue."""
        t = at or now()
        due = [k for k, (until, _) in self._snoozed.items() if until <= t]
        for key in due:
            _, alert = self._snoozed.pop(key)
            self.push(replace(alert, raised_at=t))
        return len(due)


@dataclass(frozen=True)
class Condition:
    """An alert-worthy situation that holds right now.

    Attributes:
        key: The alert's key. It must change when the situation is "new again"
            (``WaitingRule`` puts the entity's ``since`` in it).
        source: The source key the situation comes from. It decides the
            startup baseline.
        alert: What to raise when the condition starts.
    """

    key: str
    source: str
    alert: Alert


class Rule(Protocol):
    """Turns a snapshot into the conditions that hold in it."""

    def conditions(self, snapshot: Snapshot) -> Iterable[Condition]: ...


class WaitingRule:
    """An entity entered ``WAITING``: kind ``"waiting"``, headline "NEEDS YOU",
    priority 100, amber. A new wait (a new ``since``) is a new alert.

    Args:
        source: The key of the source that reports the entities (its first
            fetch is the baseline when ``"waiting"`` is not a startup kind).
    """

    kind = "waiting"

    def __init__(self, source: str = "CC") -> None:
        self.source = source

    def conditions(self, snapshot: Snapshot) -> Iterable[Condition]:
        for e in snapshot.entities:
            if e.state is not EntityState.WAITING:
                continue
            key = f"waiting:{e.id}:{e.since.isoformat() if e.since else ''}"
            first = " · ".join(x for x in (e.where, e.reason) if x)
            yield Condition(key, self.source, Alert(
                key=key, kind=self.kind, title=e.name, body=tuple(x for x in (first, e.detail) if x),
                priority=100, color=PALETTE["amber"], url=e.url, target=("entity", e.id), headline="NEEDS YOU"))


class AlertRules:
    """Turns snapshot changes into alerts.

    Args:
        rules: The rules to run on every snapshot.
        images: A picture (path) per alert kind, e.g. ``{"waiting": "~/art.gif"}``,
            for alerts that don't name one.
        startup_kinds: Kinds that alert for conditions already true at startup.
    """

    def __init__(self, rules: Sequence[Rule], images: dict[str, str] | None = None,
                 startup_kinds: frozenset[str] = frozenset({"waiting"})) -> None:
        self.rules = tuple(rules)
        self.images = dict(images or {})
        self.startup_kinds = startup_kinds
        self.known: set[str] = set()
        self.sources_seen: set[str] = set()

    def conditions(self, snapshot: Snapshot) -> list[Condition]:
        """Every alert-worthy condition that holds right now, from every rule."""
        return [cond for rule in self.rules for cond in rule.conditions(snapshot)]

    def evaluate(self, snapshot: Snapshot) -> tuple[list[Alert], set[str]]:
        """New alerts since the last call, and the keys of every live condition."""
        seen_before = set(self.sources_seen)
        fresh: list[Alert] = []
        live: set[str] = set()
        for cond in self.conditions(snapshot):
            live.add(cond.key)
            if cond.key in self.known:
                continue
            self.known.add(cond.key)
            if cond.source in seen_before or cond.alert.kind in self.startup_kinds:
                fresh.append(replace(cond.alert, key=cond.key, from_rule=True,
                                     image=cond.alert.image or self.images.get(cond.alert.kind)))
        self.known &= live  # a condition that goes away may alert again when it comes back
        self.sources_seen |= {s.key for s in snapshot.sources if s.last_ok is not None}
        return fresh, live
