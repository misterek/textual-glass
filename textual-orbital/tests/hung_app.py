"""A dashboard whose only source never returns, like an HTTP call with no
timeout whose socket died while the laptop slept. ``test_pty.py`` runs it in a
real pty and presses ``q``: the process must exit at once (rule 25)."""

from __future__ import annotations

import threading
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType

from textual_orbital import HeaderStrip, OrbitalApp, SourceData, StaticProvider


class Stuck:
    key, name, interval = "ST", "Stuck", 1.0

    def fetch(self) -> SourceData:
        threading.Event().wait()  # forever
        return SourceData()


class StuckBoard(OrbitalApp):
    TITLE = "Stuck"
    BINDINGS: ClassVar[list[BindingType]] = [("q", "quit", "quit")]

    def compose(self) -> ComposeResult:
        yield HeaderStrip("STUCK", ["one"])


if __name__ == "__main__":
    StuckBoard(StaticProvider([Stuck()]), notify="off").run()
