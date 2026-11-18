"""The boards demo: a folder of panels on one orbital page (``BoardsPage`` over a ``BoardsSource``).

Run it from the example's folder, in Ghostty::

    uv run python app.py --demo           # a fresh temporary folder with the demo's panels (boards/seed.py)
    uv run python app.py                  # the boards in $BOARDS_DIR, or ~/.cache/boards
    uv run python app.py --dir DIR        # the boards in DIR
    uv run python app.py --plain          # stock Textual, no glass

Keys: ``[`` ``]`` board, arrows or ``tab`` panel, ``enter`` the panel full screen, ``+`` ``-`` its size, ``<``
``>`` its place, ``x`` remove it, ``r`` run it now, ``h`` sources, ``?`` help, ``q`` quit. It never opens a browser
and never plays a sound.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType

from boards.seed import python_first, seed
from textual_orbital import PALETTE, HeaderStrip, HelpScreen, KeyBar, OrbitalApp, SourcesScreen, StaticProvider
from textual_orbital.boards import BoardsPage, BoardsSource, PanelSpec, Size, Store
from textual_orbital.widgets import SourceHealth

SOURCE_KEY = "BD"
#: Under "No panels yet" when the folder is empty.
EMPTY_HINT = "ask Claude to add one, or: run the demo with --demo"

HELP_KEYS = [
    ("[ ]", "previous / next board"),
    ("← → ↑ ↓", "move between panels"),
    ("tab", "next panel"),
    ("enter", "the panel full screen"),
    ("+ -", "grow / shrink the panel"),
    ("< >", "move it earlier / later"),
    ("x", "remove the panel"),
    ("r", "run the panel now"),
    ("h", "source health"),
    ("? q", "this help / quit"),
]

LEGEND = [
    ("◆", PALETTE["accent"], "the board on screen; the number: its panels"),
    ("✕1", PALETTE["red"], "a board with a failing panel"),
    ("─●", PALETTE["accent"], "a line; the bright dot: its newest point"),
    ("▒", PALETTE["accent"], "the soft fill: under the first line only"),
    ("✕", PALETTE["red"], "a failing panel: its last good data dimmed"),
    ("2m", PALETTE["text_dim"], "left before the panel expires (its expires_at)"),
]


class BoardsApp(OrbitalApp):
    """The boards in a folder, as one orbital page."""

    TITLE = "Boards"
    CSS = """
    Screen { layout: vertical; }
    #boards { height: 1fr; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "quit", priority=True),
        Binding("ctrl+c", "quit", show=False, priority=True),
        Binding("question_mark", "help", show=False),
        Binding("h,H", "sources", show=False),
    ]

    def __init__(self, folder: Store, *, interval: float = 2.0, **kwargs: Any) -> None:
        self.boards_store = folder  # ``store`` is orbital's own Store of sources
        source = BoardsSource(folder, key=SOURCE_KEY, interval=interval)
        kwargs.setdefault("notify", "off")
        super().__init__(StaticProvider([source]), rules=(), alerts=False, bell=False, title_badge=False, **kwargs)

    def compose(self) -> ComposeResult:
        yield HeaderStrip("BOARDS", id="header")
        yield BoardsPage(source_key=SOURCE_KEY, remove=self.remove_panel, arrange=self.arrange_panel,
                         empty_hint=EMPTY_HINT, id="boards")
        yield KeyBar(BoardsPage.KEYS, id="keys")

    def on_mount(self) -> None:
        self.call_after_refresh(self.query_one(BoardsPage).focus_default)

    def remove_panel(self, spec: PanelSpec) -> None:
        self.boards_store.remove(spec.id)

    def arrange_panel(self, panel_id: str, *, size: Size | None = None, position: float | None = None) -> None:
        self.boards_store.arrange(panel_id, size=size, position=position)  # the page then polls the source

    def snapshot_changed(self, snapshot: Any) -> None:
        super().snapshot_changed(snapshot)
        problems = len(self.boards_store.problems)
        self.query_one("#keys", KeyBar).set_status(
            [(f"{problems} unreadable panel file{'s' if problems != 1 else ''}", PALETTE["amber"])] if problems else [])

    def action_help(self) -> None:
        self.push_screen(HelpScreen(HELP_KEYS, LEGEND))

    def action_sources(self) -> None:
        self.push_screen(SourcesScreen(self.store.statuses(), errors=self.errors))

    def on_source_health_clicked(self, message: SourceHealth.Clicked) -> None:
        self.action_sources()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--dir", metavar="DIR", help="the boards folder (default: $BOARDS_DIR or ~/.cache/boards)")
    parser.add_argument("--demo", action="store_true", help="a fresh temporary folder with the demo's panels")
    parser.add_argument("--plain", action="store_true", help="stock Textual, no glass")
    args = parser.parse_args(argv)
    glass = False if args.plain else None
    if not args.demo:
        BoardsApp(Store(args.dir), glass=glass).run()
        return
    folder = tempfile.mkdtemp(prefix="boards-demo-")
    try:
        store = Store(folder)
        python_first()
        seed(store)
        BoardsApp(store, glass=glass).run()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
