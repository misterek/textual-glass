"""The app in a real pty, as Ghostty would host it: its byte stream replayed through pyte and the kitty model.

* ``demo``: ``app.py --demo`` at 90x28, through every key: boards, the full view, the remove confirmation, help.
* ``quiet``: a folder of panels of every kind whose commands print the same thing every time, run every second:
  after the first runs, not one graphics command and not one byte (rule 9: equal data sends no pixels).
* ``plain``: ``--plain`` sends no graphics at all.

Every run must exit 0 with a clean protocol and every image deleted. About 30 seconds. The composite PNGs go to
``$BOARDS_PNG_DIR``, or else a temporary folder of pytest's, never this folder.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from boards.seed import FEED, seed
from textual_glass.kitty import Z_BELOW_BACKGROUND
from textual_glass.testing.harness import BOX_GLYPHS, Capture, Replay, Step, graphics_in, replay_steps, run_app
from textual_orbital.boards import Kind, PanelSpec, Store

ROOT = Path(__file__).resolve().parent.parent

#: The demo, key by key. Each label's screen is the one just before its keys go.
TOUR = [
    Step(5.0, "main"),
    Step(0.0, "to-incident", "]"),
    Step(1.5, "incident"),
    Step(0.0, "back", "["),
    Step(1.0, "open", "enter"),
    Step(1.5, "full"),
    Step(0.0, "close", "escape"),
    Step(0.8, "move", "right"),
    Step(0.6, "remove", "x"),
    Step(1.0, "confirm"),
    Step(0.0, "keep", "n"),
    Step(0.8, "help", "?"),
    Step(1.0, "helped"),
    Step(0.0, "unhelp", "escape"),
    Step(0.8, "quit", "q"),
]
QUIET = [Step(5.0, "start"), Step(0.0, "idle"), Step(4.0, "quit", "q")]
PLAIN = [Step(4.0, "main"), Step(0.0, "quit", "q")]
SIZE = (90, 28)

MATRIX = {"status": "success", "data": {"resultType": "matrix", "result": [
    {"metric": {"instance": name}, "values": [[1_700_000_000 + 15 * k, str(base + (k % 7))] for k in range(40)]}
    for name, base in (("api", 100), ("db", 60))]}}


def quiet_folder(root: Path) -> Path:
    """Panels of every kind whose commands print the same output every time, run every second."""
    store = Store(root)
    at = datetime.now(timezone.utc)
    matrix = shlex.quote(json.dumps(MATRIX))
    panels: list[tuple[str, Kind, dict[str, Any]]] = [
        ("latency", "timeseries", {"cmd": f"printf '%s\\n' {matrix}", "unit": "ms"}),
        ("requests", "stat", {"cmd": f"printf '%s\\n' {matrix}"}),
        ("cpu", "gauge", {"cmd": f"echo run >> {shlex.quote(str(root / 'runs.log'))}; echo 42", "unit": "%",
                          "ymax": 100.0}),
        ("queues", "bars", {"cmd": "printf 'billing 3\\nsearch 5\\n'"}),
        ("services", "table", {"cmd": "printf 'NAME STATUS\\napi Running\\ndb Pending\\n'"}),
        ("log", "text", {"cmd": "echo hello"}),
        ("broken", "stat", {"cmd": "echo 'no route to host' >&2; exit 1"}),
        ("notes", "text", {"data": "inline notes"}),
    ]
    for k, (title, kind, fields) in enumerate(panels):
        fields.setdefault("every", 1.0)
        store.add(PanelSpec(id=store.new_id(), board="main", title=title, kind=kind, added_at=at.replace(microsecond=k),
                            **fields))
    return root


QUIET_LOG: list[Path] = []
Run = tuple[Capture, dict[str, Replay]]


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Run]]:
    pngs = Path(os.environ.get("BOARDS_PNG_DIR") or tmp_path_factory.mktemp("boards-pty"))
    # The function-scoped fixtures aren't set up yet for a module-scoped one: the runs get their own guards.
    env = {"BOARDS_DIR": str(tmp_path_factory.mktemp("boards-home")), "TEXTUAL_ORBITAL_SOUND": "off"}
    folder = quiet_folder(tmp_path_factory.mktemp("quiet"))
    QUIET_LOG[:] = [folder / "runs.log"]

    def run(name: str, argv: list[str], steps: list[Step], size: tuple[int, int] = SIZE) -> Run:
        capture = run_app([sys.executable, "app.py", *argv], steps, str(ROOT), size=size, env=env, timeout=60.0)
        png_dir = pngs / name
        png_dir.mkdir(parents=True, exist_ok=True)
        return capture, {label: rp.snapshot() for label, rp in replay_steps(capture, size, str(png_dir))}

    yield {
        "demo": run("demo", ["--demo"], TOUR),
        "quiet": run("quiet", ["--dir", str(folder)], QUIET, size=(140, 45)),  # all eight panels on screen
        "plain": run("plain", ["--demo", "--plain"], PLAIN),
    }


@pytest.mark.parametrize("name", ["demo", "quiet", "plain"])
def test_it_starts_and_quits_cleanly(runs: dict[str, Run], name: str) -> None:
    capture, snaps = runs[name]
    raw = capture.raw
    assert capture.code == 0 and b"Traceback" not in raw, raw[-3000:]
    model = snaps["end"].model
    assert model.errors == [], model.errors[:5]
    assert model.no_quiet == 0 and model.queries == 0 and model.pending is None
    assert not model.images, "every image is deleted on exit"
    if name == "plain":
        assert graphics_in(raw) == 0, "--plain sends no graphics"
    else:
        assert raw.rfind(b"\x1b[?1049l") > raw.rfind(b"\x1b_G"), "no graphics after leaving the alternate screen"


def test_the_demo_tour(runs: dict[str, Run]) -> None:
    _, snaps = runs["demo"]
    main = snaps["main"].all_text()
    for word in ("BOARDS", "◆ main 6", "incident-db 4", "p95 latency", "requests", "CPU", "queue depth", "services",
                 "nightly backup", "backup-db:5432", "Running", "now"):
        assert word in main, word
    assert not BOX_GLYPHS & set(main), "the panels' borders are pixels, not box characters"
    bgs = [bg for row in snaps["main"].cell_bgs() for bg in row]
    assert bgs.count("default") == len(bgs), "every cell lets the glass show through"
    for p in snaps["main"].model.placements.values():
        if not p["virtual"]:
            assert p["z"] < Z_BELOW_BACKGROUND, p
    incident = snaps["incident"].all_text()
    for word in ("◆ incident-db 4", "replica lag", "connections", "canary errors", "incident notes", "14:02"):
        assert word in incident, word
    assert "◆ main 6" in snaps["open"].all_text(), "[ goes back"
    full = snaps["full"].all_text()
    assert "$ " in full and "last run" in full and "esc back" in full, "the full view: command, last run, keys"
    assert "p95 latency" in full and "window 15m" in full, "of the focused panel"
    assert 'Remove "requests"? y / n' in snaps["confirm"].all_text(), "x on the panel the arrow moved to"
    assert "p95 latency" in snaps["help"].all_text() and "requests" in snaps["help"].all_text(), "n kept the panel"
    assert "KEYS AND LEGEND" in snaps["helped"].all_text()


def test_equal_data_sends_nothing(runs: dict[str, Run]) -> None:
    capture, snaps = runs["quiet"]
    text = snaps["idle"].all_text()
    for word in ("latency", "requests", "42%", "billing", "Running", "hello", "no route to host", "inline notes"):
        assert word in text, word
    runs_logged = QUIET_LOG[0].read_text().count("run")
    assert runs_logged >= 6, f"the panels really ran, every second ({runs_logged} runs of one)"
    t0, t1 = capture.mark_time("idle"), capture.mark_time("quit")
    quiet = capture.between(t0 + 0.3, t1)
    assert graphics_in(quiet) == 0, "panels that ran every second with the same output: no pixels"
    clock = {datetime.fromtimestamp(t).strftime("%H:%M") for t in (capture.chunks[0][0], t1)}
    if len(clock) == 1:  # the header's clock changes on the minute; otherwise not one byte
        assert quiet == b"", quiet[:300]


def test_plain_shows_the_same_board(runs: dict[str, Run]) -> None:
    _, snaps = runs["plain"]
    text = snaps["main"].all_text()
    for word in ("◆ main 6", "p95 latency", "requests", "queue depth", "services", "now", "╔"):
        assert word in text, word


def test_the_demo_seed() -> None:
    """``seed``: two boards, every kind, ``cmd`` panels on feed.py, one ``data`` panel, one that fails and one
    that expires in 3 minutes."""

    class Folder:
        def __init__(self) -> None:
            self.added: list[PanelSpec] = []

        def new_id(self) -> str:
            return f"{len(self.added) + 1:08x}"

        def add(self, spec: PanelSpec) -> PanelSpec:
            self.added.append(spec)
            return spec

    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    folder = Folder()
    specs = seed(folder, now=now, python="python3.12")
    assert specs == folder.added
    assert {s.board for s in specs} == {"main", "incident-db"}
    assert {s.kind for s in specs} == {"timeseries", "stat", "gauge", "bars", "table", "text"}
    feeds = [s for s in specs if s.cmd and s.cmd.startswith("python3.12 feed.py ")]
    assert len(feeds) == 8 and all(s.cwd == str(FEED.parent) for s in feeds), "feed.py in its own folder"
    assert not any(str(Path.home()) in (s.cmd or "") for s in specs), "no paths of this machine on screen"
    assert sum(s.data is not None for s in specs) == 1, "one data panel"
    assert sum(bool(s.cmd and "exit 1" in s.cmd) for s in specs) == 1, "one that fails"
    assert [s.expires_at - now for s in specs if s.expires_at and s.expires_at - now < timedelta(hours=1)] == [
        timedelta(minutes=3)], "one that expires in 3 minutes"
    assert [s.added_at for s in specs] == sorted(s.added_at for s in specs), "in order on their boards"
