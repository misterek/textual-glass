import os
import sys
import webbrowser
from collections.abc import Iterator

# The console example's modules (app, demo, pages, rules) import each other by
# plain name. Drop same-named modules from elsewhere (another package's example
# in the same pytest run), then put the console first on the path.
CONSOLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "console")
for _name in ("app", "demo", "pages", "rules"):
    _module = sys.modules.get(_name)
    if _module is not None and os.path.dirname(getattr(_module, "__file__", None) or "") != CONSOLE:
        del sys.modules[_name]
sys.path.insert(0, CONSOLE)


import pytest  # noqa: E402

import textual_glass._textual_hooks as hooks  # noqa: E402
from textual_glass import hard_exit  # noqa: E402
from textual_glass.testing.pilot_helpers import Output  # noqa: E402


@pytest.fixture(autouse=True)
def no_browser(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """No test may open the user's real browser: record the URLs instead."""
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
    """Run as in a plain Ghostty window, even from a shell inside tmux or screen
    (the notifier wraps its escapes when ``TMUX`` is set)."""
    for name in ("TMUX", "STY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def output(monkeypatch: pytest.MonkeyPatch) -> Iterator[Output]:
    """Capture what glass and the notifier write instead of sending it to a terminal.

    Afterwards, every test that used it checks rule 1: each graphics command
    carries ``q=2`` and nothing queries the terminal while Textual runs."""
    out = Output()
    monkeypatch.setattr(hooks, "driver_write", out)
    yield out
    model = out.model()
    assert model.no_quiet == 0 and model.queries == 0, "every command is quiet and nothing is queried"


@pytest.fixture(autouse=True)
def no_hard_exit_leftovers() -> Iterator[None]:
    """Whatever is registered with ``on_hard_exit`` is written to the terminal when the
    interpreter exits: a test must not leave anything for pytest's own exit to write (rule 24)."""
    yield
    left = sorted(hard_exit._entries)
    hard_exit._entries.clear()
    assert not left, f"on_hard_exit entries left registered: {left}"
