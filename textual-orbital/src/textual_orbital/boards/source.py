"""``BoardsSource``: an orbital source whose payload is the live panels of a ``Store``.

An idle board costs nothing downstream: while no panel file changed and no panel expired, ``fetch`` hands
back the same tuple object, which the orbital store treats as no change.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from ..provider import SourceData
from .model import PanelSpec
from .store import Store

__all__ = ["BoardsSource"]


class BoardsSource:
    """The live panels of ``store``, as ``snapshot.payload(key)``: a ``tuple[PanelSpec, ...]``.

    Panel files that can't be read are skipped (``store.problems`` says which); ``fetch`` raises only when
    the folder itself can't be read. Safe to run twice at once.
    """

    def __init__(self, store: Store, key: str = "BD", name: str = "Boards", interval: float = 2.0) -> None:
        self.store = store
        self.key = key
        self.name = name
        self.interval = interval
        self._lock = threading.Lock()
        self._signature: tuple[tuple[str, int, int], ...] | None = None
        self._panels: tuple[PanelSpec, ...] = ()
        self._expires: datetime | None = None  # the soonest expiry among the panels

    def fetch(self) -> SourceData:
        now = datetime.now(timezone.utc)
        # Taken before reading, so a write that lands during the read shows on the next fetch.
        signature = self.store.signature()
        with self._lock:
            fresh = self._expires is None or now < self._expires
            if signature == self._signature and fresh:
                return SourceData(payload=self._panels)
        panels = tuple(self.store.panels(at=now))
        expiries = [p.expires_at for p in panels if p.expires_at is not None]
        with self._lock:
            if panels != self._panels:
                self._panels = panels
            self._signature = signature
            self._expires = min(expiries) if expiries else None
            return SourceData(payload=self._panels)
