"""The demo's panels: two boards with every kind, ``cmd`` panels on ``feed.py``, one ``data`` panel, one panel
that fails and one that expires in 3 minutes.

The ``cmd`` panels run ``python3 feed.py ...`` in this folder, so what the full-screen view shows is short and the
same on every machine; the demo app puts its own Python's folder first on ``PATH``, so ``python3`` is that Python.

    from textual_orbital.boards import Store
    from boards.seed import seed

    seed(Store(folder))     # the PanelSpecs it wrote, in order
"""

from __future__ import annotations

import os
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from textual_orbital.boards import Kind, PanelSpec

__all__ = ["FEED", "NOTES", "python_first", "seed"]

FEED = Path(__file__).resolve().with_name("feed.py")

#: The incident board's notes: the one ``data`` panel.
NOTES = """14:02  replica-2 lag above 30s, paged db on-call
14:05  failover drill paused
14:09  long vacuum on orders found, cancelled
14:11  lag falling: 12s
14:20  lag back under 5s, watching for an hour"""


def python_first() -> None:
    """Put the folder of the running Python first on ``PATH`` (the panels' commands get this process's
    environment), so the demo's ``python3`` is this Python, with or without an activated virtualenv."""
    folder = os.path.dirname(sys.executable)
    os.environ["PATH"] = os.pathsep.join([folder, os.environ.get("PATH", "")])


class Adds(Protocol):
    """What ``seed`` needs of a store."""

    def new_id(self) -> str: ...

    def add(self, spec: PanelSpec) -> PanelSpec: ...


def seed(store: Adds, *, now: datetime | None = None, python: str = "python3") -> list[PanelSpec]:
    """Add the demo's panels to ``store`` and return them as written. ``python`` is the command that runs
    ``feed.py`` (found on ``PATH``: see :func:`python_first`)."""
    at = now or datetime.now(timezone.utc)
    feed = f"{shlex.quote(python)} {FEED.name}"
    order = iter(range(100))

    def panel(board: str, title: str, kind: Kind, **fields: Any) -> PanelSpec:
        fields.setdefault("cwd", str(FEED.parent))
        return PanelSpec(id=store.new_id(), board=board, title=title, kind=kind,
                         added_at=at + timedelta(milliseconds=next(order)), **fields)

    specs = [
        panel("main", "p95 latency", "timeseries", cmd=f"{feed} series api web db --base 180 --swing 60", unit="ms",
              every=15.0, added_by="checkout-api", expires_at=at + timedelta(hours=2)),
        panel("main", "requests", "stat", cmd=f"{feed} series requests --base 1200 --swing 260", unit="req/s",
              every=15.0, added_by="checkout-api"),
        panel("main", "CPU", "gauge", cmd=f"{feed} value --base 64 --swing 22", unit="%", ymax=100.0, every=5.0,
              added_by="infra"),
        panel("main", "queue depth", "bars", cmd=f"{feed} labels billing search auth email export --swing 45",
              every=10.0, added_by="infra"),
        panel("main", "services", "table", cmd=f"{feed} table", every=20.0, added_by="infra"),
        panel("main", "nightly backup", "stat", cmd="echo 'backup-db:5432: connection refused' >&2; exit 1",
              every=60.0, added_by="db-team"),
        panel("incident-db", "replica lag", "timeseries", cmd=f"{feed} series replica-1 replica-2 --base 6 --swing 5",
              unit="s", every=15.0, added_by="db-team", expires_at=at + timedelta(hours=6)),
        panel("incident-db", "connections", "gauge", cmd=f"{feed} value --base 330 --swing 120", ymax=500.0,
              every=10.0, added_by="db-team"),
        panel("incident-db", "canary errors", "stat", cmd=f"{feed} value --base 0.8 --swing 0.6", unit="%",
              every=5.0, added_by="checkout-api", expires_at=at + timedelta(minutes=3)),
        panel("incident-db", "incident notes", "text", data=NOTES, added_by="claude"),
    ]
    return [store.add(spec) for spec in specs]
