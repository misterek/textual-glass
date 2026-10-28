"""Write the README's screenshots of the showcase (``examples/showcase``). From the repository root::

    uv run python textual-glass/tests/screenshots.py              # into textual-glass/docs/screenshots
    uv run python textual-glass/tests/screenshots.py /tmp/shots   # somewhere else

For each size (90x28 and 140x45): the stock widgets' view, the pixel widgets'
view and the "ready to ship" modal; plus both views with ``--plain`` at 90x28.

These are approximate composites, not terminal screenshots: the app runs in a
real pty and ``textual_glass.testing.harness`` draws its byte stream with the
kitty model (pixels) and Menlo at 9x19 px cells (text). No animation (sprites
and the modal's picture show their first frame), no dimmed text, and the font
and cell size differ from Ghostty's.
"""

from __future__ import annotations

import argparse
import os
import sys

from textual_glass.testing.harness import Step
from textual_glass.testing.screenshots import shoot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOWCASE = [sys.executable, os.path.join(ROOT, "examples", "showcase", "app.py")]
SIZES = ((90, 28), (140, 45))

GLASS_STEPS = [Step(3.5, "stock"), Step(0.0, "to pixels", "2"), Step(2.5, "pixels"), Step(0.0, "to stock", "1"),
               Step(1.5, "back"), Step(0.0, "to modal", "m"), Step(2.5, "modal"), Step(0.0, "close", "escape"),
               Step(0.5, "quit", "q")]  # a modal screen blocks the app's keys, q included
PLAIN_STEPS = [Step(3.0, "stock"), Step(0.0, "to pixels", "2"), Step(1.5, "pixels"), Step(0.0, "quit", "q")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Write approximate screenshots of the showcase.")
    parser.add_argument("out", nargs="?", default=os.path.join(ROOT, "docs", "screenshots"))
    args = parser.parse_args()
    paths: list[str] = []
    for size in SIZES:
        paths += shoot(SHOWCASE, ROOT, size, GLASS_STEPS, ("stock", "pixels", "modal"), args.out)
    paths += shoot([*SHOWCASE, "--plain"], ROOT, SIZES[0], PLAIN_STEPS, ("stock", "pixels"), args.out, prefix="plain_")
    print("\n".join(paths))


if __name__ == "__main__":
    main()
