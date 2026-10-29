"""Write approximate screenshots of every page, the alert, a toast, the text fallback and the
Replay page's summary.

    uv run python textual-orbital/tests/screenshots.py              # into textual-orbital/docs/screenshots
    uv run python textual-orbital/tests/screenshots.py /tmp/shots

These are composites drawn by the test model (``textual_glass.testing``) from
the app's real byte stream in a pty: pixels from the kitty model, text in a
monospace font at 9x19 px cells. They are approximations, not terminal
screenshots: no animation, no dim text, and the font and cell size differ from
Ghostty's.
"""

from __future__ import annotations

import argparse
import os
import sys

from textual_glass.testing.harness import Step
from textual_glass.testing.screenshots import shoot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = [sys.executable, os.path.join(ROOT, "examples", "console", "app.py"), "--static", "--notify", "off"]
PAGES = ("console", "releases", "week", "board")
SIZES = ((90, 28), (140, 45), (70, 22))
STEPS = [Step(4.0, "alert"), Step(0.0, "esc", "escape"), Step(2.0, "console"),
         Step(0.0, "k2", "2"), Step(1.8, "releases"), Step(0.0, "k3", "3"), Step(1.8, "week"),
         Step(0.0, "k4", "4"), Step(1.8, "board"), Step(0.0, "q", "q")]
#: The Replay page's summary: at minimal motion glass shows the end of the show as a still.
REPLAY_STEPS = [Step(4.0, "alert"), Step(0.0, "esc", "escape"), Step(1.0, "k5", "5"), Step(3.0, "replay"),
                Step(0.0, "q", "q")]
#: An alert as a toast: the startup wait stays a popup, then t raises a test alert.
TOAST_STEPS = [Step(4.0, "popup"), Step(0.0, "esc", "escape"), Step(1.0, "t", "t"), Step(1.8, "toast"),
               Step(0.0, "q", "q")]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", nargs="?", default=os.path.join(ROOT, "docs", "screenshots"))
    args = parser.parse_args()
    paths: list[str] = []
    for size in SIZES:
        paths += shoot(APP, ROOT, size, STEPS, ("alert", *PAGES), args.out)
    paths += shoot([*APP, "--plain"], ROOT, SIZES[0], STEPS, ("console",), args.out, prefix="plain_")
    paths += shoot([*APP, "--alert-style", "toast"], ROOT, SIZES[0], TOAST_STEPS, ("toast",), args.out)
    paths += shoot(["env", "TEXTUAL_GLASS_MOTION=minimal", *APP], ROOT, SIZES[0], REPLAY_STEPS, ("replay",), args.out)
    print("\n".join(paths))


if __name__ == "__main__":
    main()
