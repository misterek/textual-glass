"""``Page``: a container for one page of a dashboard."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import ClassVar

from textual.containers import Vertical

from ..model import Snapshot


class Page(Vertical):
    """One page: it gets every new snapshot and the minute tick, and says
    which keys the key bar shows and which widget takes focus.

    Subclass it, compose the page from the widgets, and fill them in
    :meth:`update_snapshot` (``OrbitalApp`` calls it on every page whenever the
    data changes, and when items are snoozed or woken).
    """

    #: The key hints for the key bar while this page shows.
    KEYS: ClassVar[Sequence[tuple[str, str]]] = ()
    #: A selector for the widget to focus when the page shows ("#queue").
    DEFAULT_FOCUS: ClassVar[str] = ""

    def focus_default(self) -> None:
        if self.DEFAULT_FOCUS:
            self.query_one(self.DEFAULT_FOCUS).focus()

    def snoozed(self) -> Mapping[str, datetime]:
        """The keys of the attention items snoozed in the app, with when they wake."""
        found = getattr(self.app, "snoozed", None)
        return found if isinstance(found, Mapping) else {}

    def update_snapshot(self, snapshot: Snapshot) -> None:
        """New data (or snoozing changed): fill the widgets."""

    def minute_tick(self, at: datetime) -> None:
        """A minute passed: every widget with ``tick(at)`` re-renders its ages."""
        for widget in self.query("*"):
            tick = getattr(widget, "tick", None)
            if callable(tick):
                tick(at)
