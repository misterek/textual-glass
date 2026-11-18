import sys
import webbrowser
from collections.abc import Iterator
from pathlib import Path

import pytest

import textual_glass._textual_hooks as hooks
import textual_orbital.sounds as sounds
from textual_glass import hard_exit
from textual_glass.testing.pilot_helpers import Output

# The tests import the example's package (boards) from the folder above. Appended, so a module of the same name
# from another suite run with this one keeps its place on the path.
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
def no_sound(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test may play a sound on the user's speakers (rule 20): ``TEXTUAL_ORBITAL_SOUND=off`` silences every
    ``Sounds`` made here and every app a pty test starts, and a real player started anyway fails the test."""
    started: list[list[str]] = []

    def refuse(argv: list[str]) -> sounds.Process:
        started.append(argv)
        raise RuntimeError("tests never start a real sound player")

    monkeypatch.setattr(sounds, "_popen", refuse)
    monkeypatch.setenv("TEXTUAL_ORBITAL_SOUND", "off")
    yield
    assert not started, f"a test tried to play a sound: {started}"


@pytest.fixture(autouse=True)
def outside_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as in a plain Ghostty window, even from a shell inside tmux or screen."""
    for name in ("TMUX", "STY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def boards_dir(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Never touch the user's boards folder: ``$BOARDS_DIR`` points at a fresh temporary one in every test (a
    module-scoped run passes its own, see ``test_pty.py``)."""
    folder = tmp_path_factory.mktemp("boards")
    monkeypatch.setenv("BOARDS_DIR", str(folder))
    return folder


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
