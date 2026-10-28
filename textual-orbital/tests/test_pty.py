"""End to end: Mission Console in a real pty (as Ghostty would host it), its
byte stream replayed through pyte and the kitty model, plus idle cost, three
window sizes, alerts with notifications and the text fallback.

Slow (a minute or two). Run directly to also write composites of every step::

    uv run python textual-orbital/tests/test_pty.py --png /tmp/orbital-shots
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

import pytest

from textual_glass.engine import LEVEL_STEP, Z0, Z_FLOAT, Z_SCRIM
from textual_glass.kitty import PLACEHOLDER, Z_BELOW_BACKGROUND
from textual_glass.testing import harness
from textual_glass.testing.harness import BOX_GLYPHS, CH, CW, Capture, Replay, Step, graphics_in, replay_steps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = [sys.executable, os.path.join(ROOT, "examples", "console", "app.py")]
STATIC = [*APP, "--static", "--no-alerts", "--notify", "off"]
SIZE = (90, 28)
PNG_DIR = os.environ.get("ORBITAL_PNG_DIR")


def run_app(argv: list[str], script: list[Step], **kwargs: Any) -> Capture:
    return harness.run_app(argv, script, ROOT, **kwargs)


TOUR = [
    Step(4.0, "start"),
    Step(0.0, "idle"),
    Step(4.0, "down", "j"),
    Step(0.4, "down2", "j"),
    Step(0.6, "releases", "2"),
    Step(1.6, "week", "3"),
    Step(1.6, "board", "4"),
    Step(1.6, "console", "1"),
    Step(1.4, "help", "?"),
    Step(1.0, "help_close", "escape"),
    Step(0.6, "test_alert", "t"),
    Step(2.5, "alert_open"),
    Step(0.0, "alert_close", "escape"),
    Step(1.2, "resize", (120, 36)),
    Step(2.5, "q", "q"),
]


def _snaps(capture: Capture, size: tuple[int, int], prefix: str) -> dict[str, Replay]:
    png_dir = os.path.join(PNG_DIR, prefix) if PNG_DIR else None
    return {label: rp.snapshot() for label, rp in replay_steps(capture, size, png_dir)}


@pytest.fixture(scope="module")
def tour() -> tuple[Capture, dict[str, Replay]]:
    capture = run_app([*APP, "--static", "--no-alerts"], TOUR, size=SIZE)
    return capture, _snaps(capture, SIZE, "tour")


def _placements(rp: Replay) -> list[dict[str, Any]]:
    return [p for p in rp.model.placements.values() if not p["virtual"]]


def test_clean_exit_and_protocol_hygiene(tour: tuple[Capture, dict[str, Replay]]) -> None:
    capture, snaps = tour
    raw = capture.raw
    assert capture.code == 0 and b"Traceback" not in raw
    model = snaps["end"].model
    assert not model.errors, model.errors[:5]
    assert model.no_quiet == 0, "every graphics command carries q=2"
    assert model.queries == 0, "with an environment hint nothing is queried"
    assert model.pending is None, "no chunked transfer left open"
    assert not model.images, "every image is deleted on exit"
    assert raw.rfind(b"\x1b[?1049l") > raw.rfind(b"\x1b_G"), "no graphics after leaving the alternate screen"
    assert raw.rfind(b"\x1b]111\x07") > raw.rfind(b"\x1b]11;#"), "the default background is reset"
    titles = re.findall(rb"\x1b\]2;([^\x07]*)\x07", raw)
    assert titles and titles[0] == b"Mission Console" and titles[-1] == b"", "the title is cleared on exit"


def test_everything_sits_below_cell_backgrounds(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    for label, rp in snaps.items():
        for p in _placements(rp):
            assert p["z"] < Z_BELOW_BACKGROUND, (label, p)


def test_text_and_backdrop(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    rp = snaps["idle"]
    text = rp.all_text()
    for word in ("NEEDS ATTENTION", "RELEASES", "GAUGES", "CLAUDE CODE", "fix-flaky-checkout-test", "CANARY"):
        assert word in text, word
    assert not BOX_GLYPHS & set(text), "borders are pixels, not box characters"
    bgs = [bg for row in rp.cell_bgs() for bg in row]
    assert bgs.count("default") == len(bgs), "every cell lets the pixels show through"
    backdrop = [p for p in _placements(rp) if p["z"] == Z0]
    assert len(backdrop) == 1 and (backdrop[0]["c"], backdrop[0]["r"]) == SIZE
    assert rp.placeholder_ids(), "beacons are placeholder sprites"


def _crosses_minute(capture: Capture, t0: float, t1: float) -> bool:
    # The minute tick fires about half a second after the boundary, so a window that starts just after
    # :00 can still catch it: look one second earlier.
    return (datetime.fromtimestamp(capture.started + t0 - 1.0).minute
            != datetime.fromtimestamp(capture.started + t1).minute)


def test_idle_sends_nothing(tour: tuple[Capture, dict[str, Replay]]) -> None:
    capture, _ = tour
    t0, t1 = capture.mark_time("idle"), capture.mark_time("down")
    quiet = capture.between(t0 + 0.3, t1)
    assert graphics_in(quiet) == 0, "no graphics while idle"
    # Text only changes on a minute boundary (ages, the clock); otherwise nothing at all.
    if not _crosses_minute(capture, t0 + 0.3, t1):
        assert quiet == b"", f"{len(quiet)} bytes while idle"


def test_cursor_moves_with_partial_edits(tour: tuple[Capture, dict[str, Replay]]) -> None:
    capture, _ = tour
    sent = capture.between(capture.mark_time("down"), capture.mark_time("releases"))
    assert b"a=t" not in sent, "moving the selection re-uploads nothing"
    assert re.search(rb"a=f,i=\d+,r=1", sent), "the pill slides with in-place edits"
    assert len(sent) < 400_000


def test_every_page_has_its_panels(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    # each snapshot is taken just before its step's key: "week" shows page 2, and so on
    expected = {"week": "RELEASE TRACK", "board": "THIS WEEK", "console": "GOALS", "help": "NEEDS ATTENTION"}
    for label, text in expected.items():
        assert text in snaps[label].all_text(), label
        assert len([p for p in _placements(snaps[label]) if p["z"] == Z0]) == 1


def _placeholder_ids(rp: Replay, rows: range) -> set[int]:
    out = set()
    for y in rows:
        line = rp.screen.buffer[y]
        for x in range(rp.size[0]):
            cell = line[x]
            if cell.data and cell.data[0] == PLACEHOLDER and re.fullmatch("[0-9a-fA-F]{6}", cell.fg):
                out.add(int(cell.fg, 16))
    return out


def test_alert_scrim_dialog_and_picture(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    rp = snaps["alert_close"]  # just before Escape closes it
    text = rp.all_text()
    assert "INCOMING TRANSMISSION" in text and "N E E D S" in text and "test-transmission" in text
    zs = sorted(p["z"] for p in _placements(rp))
    assert Z0 + LEVEL_STEP + Z_SCRIM in zs, "the scrim dims the console"
    assert Z0 + LEVEL_STEP in zs, "the dialog has its own glass layer"
    after = snaps["resize"]
    assert all(p["z"] == Z0 for p in _placements(after)), "alert layers are gone after Escape"
    # Placeholder sprites draw above the scrim, so under the popup they show their dimmed copy
    dimmable = {i for i, img in rp.model.images.items() if len(img["gaps"]) > 1 and img["gaps"][-1] < 0}
    shown = _placeholder_ids(rp, rows=range(0, 4))  # the beacons, above the dialog (not its own picture)
    assert dimmable and shown & dimmable
    for image_id in shown & dimmable:
        image = rp.model.images[image_id]
        assert image["cur"] == len(image["frames"]) - 1 and image["state"] == 1, image_id
        assert after.model.images[image_id]["cur"] == 0, "bright again once the popup closes"


def _outside_the_dialog(rp: Replay) -> list[tuple[int, int, str, bool]]:
    """The cells outside a popup's own glass (its placement at modal level 1):
    (x, y, background, is a placeholder cell)."""
    (dialog,) = [p for p in _placements(rp) if p["z"] == Z0 + LEVEL_STEP]
    x0, y0 = dialog["col"], dialog["row"]
    x1, y1 = x0 + dialog["c"], y0 + dialog["r"]
    return [(x, y, bg, rp.screen.buffer[y][x].data[:1] == PLACEHOLDER) for y, row in enumerate(rp.cell_bgs())
            for x, bg in enumerate(row) if not (x0 <= x < x1 and y0 <= y < y1)]


def test_cells_stay_transparent_under_a_popup(tour: tuple[Capture, dict[str, Replay]]) -> None:
    """Under the help screen and the alert, every cell outside the dialog keeps
    the terminal's default background, so the dimmed glass shows through. Glass
    once gave the pixel widgets' text cells the modal's own background, and the
    beacons' placeholder cells the screen's: flat dark blocks over the scrim."""
    _, snaps = tour
    for label in ("help_close", "alert_close"):  # each taken just before Escape closes the popup
        cells = _outside_the_dialog(snaps[label])
        text = {(x, y) for x, y, _, _ in cells if snaps[label].screen.buffer[y][x].data.strip()}
        assert any(placeholder for *_, placeholder in cells) and len(text) > 100, (label, "beacons and text")
        painted = [cell for cell in cells if cell[2] != "default"]
        assert not painted, (label, painted[:8])


def test_resize_rerenders(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    model = snaps["q"].model
    backdrop = [(i, p) for (i, _), p in model.placements.items() if p["z"] == Z0]
    assert len(backdrop) == 1
    image_id, placement = backdrop[0]
    assert (placement["c"], placement["r"]) == (120, 36)
    assert (model.images[image_id]["w"], model.images[image_id]["h"]) == (120 * CW, 36 * CH)


# -- window sizes -------------------------------------------------------------------------


@pytest.mark.parametrize("size, wants, absent", [
    ((70, 22), ["NEEDS ATTENTION", "RELEASES", "BUILD", "CLAUDE CODE"], ["GAUGES", "SELECTED"]),
    ((90, 28), ["NEEDS ATTENTION", "GAUGES", "CANARY", "LOAD"], ["SELECTED", "BOARD"]),
    ((140, 45), ["NEEDS ATTENTION", "GAUGES", "SELECTED", "BOARD", "RELEASES"], []),
])
def test_window_sizes(size: tuple[int, int], wants: list[str], absent: list[str]) -> None:
    steps = [Step(4.0, "console"), Step(0.0, "p2", "2"), Step(1.6, "releases"), Step(0.0, "p3", "3"),
             Step(1.6, "week"), Step(0.0, "p4", "4"), Step(1.6, "board"), Step(0.0, "q", "q")]
    capture = run_app(STATIC, steps, size=size)
    snaps = _snaps(capture, size, f"{size[0]}x{size[1]}")
    assert capture.code == 0 and b"Traceback" not in capture.raw
    text = snaps["p2"].all_text()
    for word in wants:
        assert word in text, (size, word)
    for word in absent:
        assert word not in text, (size, word)
    assert "IN FLIGHT" in snaps["p3"].all_text() and "THIS WEEK" in snaps["p4"].all_text()
    assert "GOALS" in snaps["q"].all_text()
    model = snaps["end"].model
    assert not model.errors and not model.images


# -- alerts from the demo world -------------------------------------------------------------


def test_a_toast_is_placed_on_the_float_layer_and_deleted() -> None:
    """``--alert-style toast``: the startup wait stays a popup (``NeedsYouRule``); ``t`` raises
    a toast in the bottom-right corner, glass on the float layer above the backdrop and its
    beacon a placeholder sprite, every cell's background still the default; when it times
    out its float image is deleted and the page shows whole again."""
    steps = [Step(4.0, "popup"), Step(0.0, "esc", "escape"), Step(1.0, "t", "t"), Step(1.5, "toast"),
             Step(2.5, "gone"), Step(0.0, "q", "q")]
    argv = [*APP, "--static", "--notify", "off", "--alert-style", "toast", "--toast-timeout", "2"]
    capture = run_app(argv, steps, size=SIZE)
    snaps = _snaps(capture, SIZE, "toast")
    assert capture.code == 0 and b"Traceback" not in capture.raw
    assert "INCOMING TRANSMISSION" in snaps["esc"].all_text(), "the startup wait is a popup"
    rp = snaps["toast"]
    x0, y0, x1, y1 = SIZE[0] - 41, SIZE[1] - 6, SIZE[0] - 1, SIZE[1] - 1  # 40 x 5, above the key bar
    assert "NEEDS YOU" in rp.text(y0)[x0:x1] and "test-transmission" in rp.text(y0 + 1)[x0:x1]
    assert "s snooze" in rp.text(y0 + 3)[x0:x1]
    (image_id, placement), = [(i, p) for (i, _), p in rp.model.placements.items()
                              if not p["virtual"] and p["z"] == Z0 + Z_FLOAT]
    assert placement["col"] <= x0 and placement["col"] + placement["c"] >= x1, placement
    assert placement["row"] <= y0 and placement["row"] + placement["r"] >= y1, placement
    sprites = _placeholder_ids(rp, range(y0 + 1, y1 - 1))
    assert sprites and sprites <= set(rp.model.images), "the beacon, uploaded"
    bgs = [bg for row in rp.cell_bgs() for bg in row]
    assert bgs.count("default") == len(bgs), "the toast's cells let its glass show through"
    gone = snaps["gone"]
    assert image_id not in gone.model.images, "its float image is deleted"
    assert all(p["z"] != Z0 + Z_FLOAT for p in gone.model.placements.values())
    assert "test-transmission" not in gone.all_text() and "RELEASES" in gone.all_text()
    end = snaps["end"].model
    assert not end.errors and not end.images


def test_demo_dynamics_raise_every_alert_kind_with_notifications() -> None:
    steps = [Step(2.5, "start")] + [Step(1.1, f"dismiss{i}", "escape") for i in range(8)] + [Step(0.5, "q", "q")]
    capture = run_app([*APP, "--fake-speed", "30", "--notify", "osc777"], steps, size=SIZE)
    raw = capture.raw
    assert capture.code == 0 and b"Traceback" not in raw
    notes = re.findall(rb"\x1b\]777;notify;Mission Console: ([^;]*);", raw)
    kinds = {n.decode() for n in notes}
    assert {"Needs you", "Abort \xb7 release failed", "Incident \xb7 you are paged"} <= kinds, kinds
    assert b"\x1b]2;\xe2\x97\x89 1 waiting \xc2\xb7 Mission Console\x07" in raw, "the title badge"
    assert b"\x07" in re.sub(rb"\x1b\][^\x07]*\x07", b"", raw), "the bell rang (a BEL outside any OSC)"
    rp = Replay(*SIZE)
    for _, data in capture.chunks:
        rp.feed(data)
    assert not rp.model.errors and not rp.model.images


def test_notifications_pass_through_tmux() -> None:
    """Under tmux (glass is off there), OSC 777 goes out wrapped in DCS passthrough, inner ESCs
    doubled, through Textual's writer in one piece; the bell and the title are not wrapped."""
    steps = [Step(3.0, "start"), Step(0.0, "q", "q")]
    env = {"TMUX": "/tmp/tmux-501/default,1,0", "TERM_PROGRAM": "tmux", "GHOSTTY_RESOURCES_DIR": "/opt/ghostty"}
    capture = run_app([*APP, "--static"], steps, size=SIZE, env=env, term="tmux-256color")
    raw = capture.raw
    assert capture.code == 0 and b"Traceback" not in raw
    note = re.search(rb"\x1bPtmux;\x1b\x1b\]777;notify;Mission Console: Needs you;([^\x07]*)\x07\x1b\\", raw)
    assert note and b"fix-flaky-checkout-test" in note.group(1), "Claude waits at startup: notified through tmux"
    assert raw[note.end():note.end() + 1] == b"\x07", "then the bell, for tmux itself"
    assert b"\x1b]777;" not in raw.replace(note.group(0), b""), "never unwrapped"
    assert b"\x1b]2;\xe2\x97\x89 1 waiting \xc2\xb7 Mission Console\x07" in raw, "the title badge, unwrapped"


def test_quitting_never_waits_for_a_hung_fetch() -> None:
    """Rule 21: a fetch that never returns runs in a daemon thread, so q exits at once. With
    the old thread pool, the interpreter joined its worker at exit and the process hung."""
    steps = [Step(2.5, "start"), Step(0.0, "q", "q")]
    capture = run_app([sys.executable, os.path.join(ROOT, "tests", "hung_app.py")], steps, size=SIZE, timeout=8)
    assert capture.code == 0, capture.code
    assert capture.wall - capture.mark_time("q") < 4.0
    assert b"Traceback" not in capture.raw and b"STUCK" in capture.raw


# -- idle cost -------------------------------------------------------------------------------


def test_idle_cpu_is_tiny() -> None:
    """Static data, no alerts: over 8 seconds the process should barely run,
    and send nothing at all (unless a minute boundary moves the ages)."""
    measured: dict[str, float] = {}

    def cpu(pid: int) -> float:
        out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        minutes, _, seconds = out.rpartition(":")
        return float(minutes or 0) * 60 + float(seconds or 0)

    def probe(label: str, pid: int) -> None:
        measured[label] = cpu(pid)
        measured[label + "_t"] = time.monotonic()

    steps = [Step(5.0, "settled"), Step(8.0, "after"), Step(0.0, "q", "q")]
    capture = run_app(STATIC, steps, size=SIZE, probe=probe)
    assert capture.code == 0
    used = measured["after"] - measured["settled"]
    wall = measured["after_t"] - measured["settled_t"]
    share = used / wall
    t0, t1 = capture.mark_time("settled") + 0.3, capture.mark_time("after")
    quiet = capture.between(t0, t1)
    print(f"idle CPU: {used:.2f} s over {wall:.1f} s = {share:.2%}; {len(quiet)} bytes")
    assert share < 0.02, f"idle CPU {share:.1%}"
    assert graphics_in(quiet) == 0
    if not _crosses_minute(capture, t0, t1):
        assert quiet == b"", f"{len(quiet)} bytes while idle"


# -- fallback --------------------------------------------------------------------------------


def _plain_checks(capture: Capture) -> None:
    raw = capture.raw
    assert capture.code == 0 and b"Traceback" not in raw
    after = raw.split(b"\x1b[?1049h", 1)[-1]
    assert b"\x1b_G" not in after, "no graphics once Textual runs"
    assert b"\x1b]11;" not in raw


@pytest.mark.parametrize("label, kwargs", [
    ("--plain", dict(argv=[*STATIC, "--plain"])),
    ("TEXTUAL_GLASS=off", dict(env={"TEXTUAL_GLASS": "off"})),
    ("xterm-256color, no answer", dict(term="xterm-256color")),
])
def test_fallback_is_a_clean_text_tui(label: str, kwargs: dict[str, Any]) -> None:
    argv = kwargs.pop("argv", STATIC)
    steps = [Step(3.0, "start"), Step(0.0, "q", "q")]
    capture = run_app(argv, steps, size=SIZE, **kwargs)
    _plain_checks(capture)
    snaps = _snaps(capture, SIZE, f"plain_{re.sub('[^a-z]+', '_', label.lower())}")
    text = snaps["q"].all_text()
    assert "NEEDS ATTENTION" in text and "fix-flaky-checkout-test" in text and "RELEASES" in text
    assert BOX_GLYPHS & set(text), "plain Textual borders"
    assert "▂" in text, "block-character signal meters"
    if label.startswith("xterm"):
        raw = capture.raw
        assert 0 <= raw.find(b"a=q") < raw.find(b"\x1b[?1049h"), "the query is sent before Textual starts"
        assert graphics_in(raw) == 1
    else:
        assert graphics_in(capture.raw) == 0


def test_a_terminal_that_answers_the_query_gets_pixels() -> None:
    steps = [Step(3.0, "start"), Step(0.0, "q", "q")]
    capture = run_app(STATIC, steps, size=SIZE, term="xterm-256color", answer=True)
    assert capture.code == 0
    rp = Replay(*SIZE)
    for _, data in capture.chunks:
        rp.feed(data)
    assert rp.model.counts.get("t", 0) >= 1 and not rp.model.errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--png", help="write approximate composite PNGs of each step here")
    parser.add_argument("pytest_args", nargs="*")
    args = parser.parse_args()
    if args.png:
        os.environ["ORBITAL_PNG_DIR"] = os.path.abspath(args.png)
    return pytest.main([__file__, "-v", "-p", "no:cacheprovider", *args.pytest_args])


if __name__ == "__main__":
    sys.exit(main())
