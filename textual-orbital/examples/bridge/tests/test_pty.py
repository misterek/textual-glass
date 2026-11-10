"""The bridge in a real pty, as Ghostty would host it: its byte stream replayed through pyte and the kitty
model at 70x22, 90x28 and 140x45. A clean protocol, every image deleted on exit, exit code 0, silence while
idle, and a composite PNG of every step (approximate: first frames only, text in Menlo).

The PNGs go to ``$BRIDGE_PNG_DIR``, or else a temporary folder of pytest's, never this folder. About 25 seconds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from textual_glass.kitty import Z_BELOW_BACKGROUND
from textual_glass.testing.harness import BOX_GLYPHS, Capture, Replay, Step, graphics_in, replay_steps, run_app

ROOT = Path(__file__).resolve().parent.parent

#: The tour at 90x28: an alert at the start (two waiting, one error), a quiet stretch while paused, then
#: every key of the demo.
TOUR = [
    Step(3.0, "start"),
    Step(0.0, "pause", " "),
    Step(0.6, "idle"),
    Step(2.0, "resume", " "),
    Step(0.4, "new", "n"),
    Step(1.0, "wait", "w"),
    Step(1.4, "answer", "a"),
    Step(1.0, "error", "e"),
    Step(1.0, "end", "x"),
    Step(1.0, "resize", (120, 36)),
    Step(1.5, "quit", "q"),
]
#: Just a look, paused so the picture holds, then quit.
LOOK = [Step(3.0, "start"), Step(0.0, "pause", " "), Step(0.6, "idle"), Step(1.0, "quit", "q")]

RUNS = {
    "90x28": ((90, 28), "12.5", TOUR),
    "90x28_calm": ((90, 28), "33.5", LOOK),
    "70x22": ((70, 22), "12.5", LOOK),
    "140x45": ((140, 45), "12.5", LOOK),
}


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Capture, dict[str, Replay]]]:
    pngs = Path(os.environ.get("BRIDGE_PNG_DIR") or tmp_path_factory.mktemp("bridge-pty"))
    out = {}
    for name, (size, at, steps) in RUNS.items():
        capture = run_app([sys.executable, "app.py", "--at", at], steps, str(ROOT), size=size, timeout=40.0)
        png_dir = pngs / name
        png_dir.mkdir(parents=True, exist_ok=True)
        out[name] = (capture, {label: rp.snapshot() for label, rp in replay_steps(capture, size, str(png_dir))})
    return out


@pytest.mark.parametrize("name", list(RUNS))
def test_clean_protocol_and_exit(runs: dict[str, tuple[Capture, dict[str, Replay]]], name: str) -> None:
    capture, snaps = runs[name]
    raw = capture.raw
    assert capture.code == 0 and b"Traceback" not in raw, raw[-2000:]
    model = snaps["end"].model
    assert model.errors == [], model.errors[:5]
    assert model.no_quiet == 0 and model.queries == 0 and model.pending is None
    assert not model.images, "every image is deleted on exit"
    assert raw.rfind(b"\x1b[?1049l") > raw.rfind(b"\x1b_G"), "no graphics after leaving the alternate screen"


@pytest.mark.parametrize("name", list(RUNS))
def test_the_bridge_is_on_screen(runs: dict[str, tuple[Capture, dict[str, Replay]]], name: str) -> None:
    _, snaps = runs[name]
    rp = snaps["idle"]
    text = rp.all_text()
    calm = name.endswith("calm")
    assert ("BRIDGE" if calm else "RED ALERT") in text
    for word in ("api-gateway", "docs-site", "infra", "vacant"):
        assert word in text, word
    if calm:
        assert "web-frontend" in text and "codex" in text, "the Codex visitor, tagged"
    assert not BOX_GLYPHS & set(text), "the panel's border is pixels, not box characters"
    bgs = [bg for row in rp.cell_bgs() for bg in row]
    assert bgs.count("default") == len(bgs), "every cell lets the pixels show through"
    assert len(rp.placeholder_ids()) >= 7, "the stations, the viewscreen and the chair are sprites"
    for p in rp.model.placements.values():
        if not p["virtual"]:
            assert p["z"] < Z_BELOW_BACKGROUND, p


@pytest.mark.parametrize("name", list(RUNS))
def test_paused_is_silent(runs: dict[str, tuple[Capture, dict[str, Replay]]], name: str) -> None:
    capture, _ = runs[name]
    t0 = capture.mark_time("idle")
    t1 = capture.mark_time("resume" if "resume" in [m[1] for m in capture.marks] else "quit")
    quiet = capture.between(t0, t1)
    assert graphics_in(quiet) == 0 and len(quiet) == 0, quiet[:300]


def test_the_tour(runs: dict[str, tuple[Capture, dict[str, Replay]]]) -> None:
    _, snaps = runs["90x28"]
    assert "PAUSED" in snaps["idle"].all_text()
    assert "search-indexer" in snaps["wait"].all_text(), "n brought a new session aboard"
    assert "error" in snaps["end"].all_text()
    resized = snaps["quit"]
    assert "api-gateway" in resized.all_text(), "laid out again at 120x36"
    assert len(resized.placeholder_ids()) >= 7, "every sprite again (a left and a vacant station share one)"
    assert len(resized.model.images) <= 14, "the old size's sprites are freed"
