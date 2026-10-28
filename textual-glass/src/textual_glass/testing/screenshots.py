"""Approximate screenshots of an app: run it in a pty, write composites of chosen steps.

    from textual_glass.testing.harness import Step
    from textual_glass.testing.screenshots import shoot

    steps = [Step(3.0, "home"), Step(0.0, "to settings", "2"), Step(1.5, "settings"), Step(0.0, "quit", "q")]
    for path in shoot([sys.executable, "app.py"], ROOT, (90, 28), steps, ("home", "settings"), "shots"):
        print(path)                       # shots/90x28_home.png, shots/90x28_settings.png

Each picture is the screen just before that step's keys, drawn from the app's
real byte stream by the kitty model (pixels) and a monospace font at 9x19 px
cells (text). They are approximations, not terminal screenshots: no animation,
no dimmed text, and the font and cell size differ from Ghostty's.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Sequence

from textual_glass.testing.harness import Step, replay_steps, run_app


def shoot(argv: Sequence[str], root: str, size: tuple[int, int], steps: Sequence[Step], labels: Collection[str],
          out: str, prefix: str = "", timeout: float = 40.0) -> list[str]:
    """Run ``argv`` (in directory ``root``) at ``size``, play ``steps``, and write
    ``{prefix}{cols}x{rows}_{label}.png`` into ``out`` for each step named in ``labels``.

    Returns the paths written. Exits (``SystemExit``) when the app fails or the
    kitty model saw a protocol error: a picture of a broken run is no screenshot.
    """
    tag = f"{prefix}{size[0]}x{size[1]}"
    capture = run_app(argv, steps, root, size=size, timeout=timeout)
    if capture.code != 0 or b"Traceback" in capture.raw:
        raise SystemExit(f"{tag}: the app failed (exit code {capture.code})")
    os.makedirs(out, exist_ok=True)
    written = []
    for label, rp in replay_steps(capture, size):
        if label in labels:
            path = os.path.join(out, f"{tag}_{label}.png")
            rp.composite(path)
            written.append(path)
        if label == "end" and rp.model.errors:
            raise SystemExit(f"{tag}: kitty protocol errors: {rp.model.errors[:3]}")
    return written
