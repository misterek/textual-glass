import os
import sys
from collections.abc import Iterator

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


import pytest  # noqa: E402

import textual_glass._textual_hooks as hooks  # noqa: E402
from textual_glass.testing.pilot_helpers import Output  # noqa: E402


@pytest.fixture
def output(monkeypatch: pytest.MonkeyPatch) -> Iterator[Output]:
    """Capture the engine's writes instead of sending them to a terminal.

    Afterwards, every test that used it checks rule 1: each graphics command
    carries ``q=2`` and nothing queries the terminal while Textual runs."""
    out = Output()
    monkeypatch.setattr(hooks, "driver_write", out)
    yield out
    model = out.model()
    assert model.no_quiet == 0 and model.queries == 0, "every command is quiet and nothing is queried"
