"""A small stock Textual app used by the tests (and handy to try glass on).

Besides stock widgets it has two widgets with pixels of their own (a
``RingGauge``, and a ``SpriteView`` showing a terminal-played beacon), a still
``GlassImage`` on the main screen, and two "pages" of bordered panels (keys 1
and 2) for the page cache.

    uv run python tests/sample_app.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, Vertical  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widgets import Button, ContentSwitcher, DataTable, Footer, Label, ProgressBar, Static  # noqa: E402

from textual_glass import Glass, GlassImage  # noqa: E402
from textual_glass.sprites import beacon_frames  # noqa: E402
from textual_glass.widgets import RingGauge, SpriteView  # noqa: E402

BADGE = "#b93c5b"
BEACON = "#ffc24b"


def checker(size: int = 16) -> bytes:
    out = bytearray()
    step = max(1, size // 4)
    for y in range(size):
        for x in range(size):
            on = (x // step + y // step) % 2
            out += bytes((240, 120, 60, 255) if on else (40, 60, 200, 255))
    return bytes(out)


def beacon() -> SpriteView:
    """A "waiting" beacon the terminal plays behind placeholder cells (a glyph without glass)."""
    return SpriteView(("beacon", "waiting", BEACON), lambda w, h: beacon_frames(w, h, "waiting", BEACON),
                      fallback_color=BEACON, id="beacon")


class Dialog(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", "Close")]
    DEFAULT_CSS = """
    Dialog { align: center middle; }
    #dialog { width: 50; height: 14; border: round $accent; background: $panel; padding: 1 2; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="dialog"):
            yield GlassImage(checker(), 16, 16, id="image", cache_key="checker")
            yield Label("A dialog", id="dialog-label")


class Sample(App):
    CSS = """
    #cards { height: 5; }
    .card { width: 1fr; border: round $primary; content-align: center middle; }
    .card.hot { border: round $warning; }
    #pages, #page1, #page2 { height: 1fr; }
    #table { border: round $primary; height: 1fr; }
    #table:focus { border: round $accent; }
    #bottom { height: 3; }
    ProgressBar { width: 40; }
    #gauge { width: 9; height: 3; }
    #beacon { width: 4; height: 2; }
    .pane { width: 1fr; height: 1fr; border: round $secondary; padding: 1 2; }
    """
    BINDINGS = [
        Binding("d", "dialog", "Dialog"),
        Binding("q", "quit", "Quit"),
        Binding("1", "page('page1')", "Page 1", show=False),
        Binding("2", "page('page2')", "Page 2", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal(id="cards"):
            yield Static("7\nlanded", classes="card", id="card1")
            yield Static("3.5\nper day", classes="card hot", id="card2")
        with ContentSwitcher(initial="page1", id="pages"):
            with Vertical(id="page1"):
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="bottom"):
                    yield ProgressBar(total=100, show_eta=False, id="progress")
                    yield Button("Go", id="go")
                    yield RingGauge(fraction=0.4, text="40%", id="gauge")
                    yield beacon()
                    yield GlassImage(checker(6), 6, 6, id="still")
            with Horizontal(id="page2"):
                yield Static("The second page", classes="pane", id="pane1")
                yield Static("has other panels", classes="pane", id="pane2")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.border_title = "Needs Attention"
        table.add_columns("Score", "Item", "Where")
        for i in range(60):
            label = f" {10 - i % 10:>2} "
            score = Text(label, style=f"bold white on {BADGE}") if i % 10 == 0 else Text(label)
            table.add_row(score, f"Item number {i}", "infra-live")
        self.query_one(ProgressBar).update(progress=60)
        table.focus()

    def action_dialog(self) -> None:
        self.push_screen(Dialog())

    def action_page(self, page: str) -> None:
        self.query_one(ContentSwitcher).current = page


class GlassSample(Glass, Sample):
    pass


if __name__ == "__main__":
    (Sample if "--plain" in sys.argv else GlassSample)().run()
