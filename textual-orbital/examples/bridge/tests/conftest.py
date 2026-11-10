import sys
import webbrowser
from collections.abc import Iterator
from pathlib import Path

import pytest

import textual_glass._textual_hooks as hooks
from textual_glass import engine, hard_exit
from textual_glass.testing.pilot_helpers import Output

# The tests import the bridge package from the folder above. Appended, so a module of the same name from another
# suite run with this one (the console example's app.py) keeps its place on the path.
sys.path.append(str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def no_browser(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """No test may open the user's real browser: record the URLs instead (rule 20)."""
    opened: list[str] = []

    def record(url: str, *args: object, **kwargs: object) -> bool:
        opened.append(url)
        return True

    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, record)
    monkeypatch.setenv("BROWSER", "true")
    yield opened


@pytest.fixture(autouse=True)
def outside_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as in a plain Ghostty window, even from a shell inside tmux or screen."""
    for name in ("TMUX", "STY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def harness_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    """Glass draws for the harness's 9 x 19 px cells, whatever terminal runs pytest (the deck picks station
    sizes that stay crisp on the cell size glass draws sprites for)."""
    monkeypatch.setattr(engine, "terminal_cell_pixels", lambda: (9.0, 19.0, True))


@pytest.fixture
def output(monkeypatch: pytest.MonkeyPatch) -> Iterator[Output]:
    """Capture what glass writes instead of sending it to a terminal (rule 1 checked after)."""
    out = Output()
    monkeypatch.setattr(hooks, "driver_write", out)
    yield out
    model = out.model()
    assert model.no_quiet == 0 and model.queries == 0, "every command is quiet and nothing is queried"


@pytest.fixture(autouse=True)
def no_hard_exit_leftovers() -> Iterator[None]:
    """Nothing may stay registered with ``on_hard_exit`` for pytest's own exit to write (rule 24)."""
    yield
    left = sorted(hard_exit._entries)
    hard_exit._entries.clear()
    assert not left, f"on_hard_exit entries left registered: {left}"
