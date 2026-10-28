"""Mission Console's own alert rules, next to the library's ``WaitingRule``.

A rule yields a ``Condition`` for every alert-worthy situation that holds right
now. The alert is raised when a condition first appears (after the source's
first fetch, which is the baseline) and dropped, even from the popup, when it
goes away. Put what makes the situation "new again" in the key. A rule may set
the alert's ``style``: ``NeedsYouRule`` keeps "Claude needs you" a popup when
the console shows other alerts as toasts (``--alert-style toast``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import cast

from demo import STAGES, Release

from textual_orbital import Alert, Condition, Snapshot, TrackHealth, WaitingRule
from textual_orbital.theme import PALETTE
from textual_orbital.widgets import status_word

RELEASE_FAILED = "release failed"
INCIDENT = "incident"


class NeedsYouRule(WaitingRule):
    """The library's ``WaitingRule``, always a popup: Claude waiting for you is
    worth the keyboard, whatever the app's ``alert_style``."""

    def conditions(self, snapshot: Snapshot) -> Iterator[Condition]:
        for cond in super().conditions(snapshot):
            yield replace(cond, alert=replace(cond.alert, style="modal"))


class ReleaseFailedRule:
    """A release failed in some stage: kind ``"release failed"``. A new failure
    (a new ``stage_since``) is a new alert."""

    def conditions(self, snapshot: Snapshot) -> Iterator[Condition]:
        for t in snapshot.track:
            if t.health is not TrackHealth.FAIL:
                continue
            since = t.stage_since.isoformat() if t.stage_since else ""
            key = f"{RELEASE_FAILED}:{t.key}:{since}"
            release = cast(Release, t.data)  # the demo's own record, carried in data
            stage = STAGES[max(0, min(len(STAGES) - 1, t.stage))].lower()
            yield Condition(key, t.source, Alert(
                key=key, kind=RELEASE_FAILED, headline="ABORT · RELEASE FAILED", title=t.title,
                body=(f"{t.label} · {status_word(t)} in {stage}", f"owner {release.owner} · run {release.run}"),
                priority=80, color=PALETTE["red"], url=t.url, target=("track", t.key)))


class IncidentRule:
    """InfraMonitor paged: an attention item whose ``why`` is ``"incident"``."""

    def conditions(self, snapshot: Snapshot) -> Iterator[Condition]:
        for item in snapshot.attention:
            if item.why != INCIDENT:
                continue
            key = f"{INCIDENT}:{item.key}"
            yield Condition(key, item.source, Alert(
                key=key, kind=INCIDENT, headline="INCIDENT · YOU ARE PAGED", title=item.title,
                body=tuple(x for x in (item.where, item.detail) if x), priority=90, color=PALETTE["orange"],
                url=item.url, target=("item", item.key)))
