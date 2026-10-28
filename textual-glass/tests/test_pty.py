"""End-to-end: the showcase example (``examples/showcase``) in a real pty, its
byte stream replayed through pyte and the kitty model, plus the fallback paths.

The tour covers both views: stock widgets (1), where glass comes for free, and
textual-glass's pixel widgets (2), with a terminal-played sprite.

Slow (about a minute). Run directly to also write composite PNGs of each step::

    uv run python textual-glass/tests/test_pty.py --png /tmp/glass-shots
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

from textual_glass.kitty import Z_BELOW_BACKGROUND
from textual_glass.testing import harness
from textual_glass.testing.harness import BOX_GLYPHS, CH, CW, Capture, Replay, Step, graphics_in, replay_steps
from textual_glass.testing.screenshots import shoot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOWCASE = [sys.executable, os.path.join(ROOT, "examples", "showcase", "app.py")]

Z0 = -2_000_000_000  # the backdrop
Z_SCRIM_0 = -1_950_000_000  # the scrim under the first popup
ART_CELLS = (40, 20)  # the modal's picture at 45 rows (showcase.tcss: ShipScreen.-tall #art)
BEACON_CELLS = (14, 7)  # the detail panel's sprite at 45 rows (Screen.-tall #beacon)


def run_app(argv, script, **kwargs):  # type: ignore[no-untyped-def]
    return harness.run_app(argv, script, ROOT, **kwargs)


SIZE = (140, 45)
RESIZED = (90, 28)
TOUR = [
    Step(3.0, "start"),
    Step(0.0, "idle_subtle"),
    Step(2.5, "down", "down"),  # stock widgets: the services table
    Step(0.6, "detail", "enter"),
    Step(1.4, "back", "escape"),  # a service's details (a modal with a table and Markdown)
    Step(1.0, "ship", "m"),
    Step(1.6, "close", "escape"),  # the "ready to ship" modal with the generated picture
    Step(1.0, "pixels", "2"),  # pixel widgets
    Step(1.6, "pixels_idle"),
    Step(2.0, "pixels_down", "down"),
    Step(1.2, "pixels_modal", "m"),
    Step(1.6, "pixels_close", "escape"),  # the modal over the pixel widgets: the sprite is dimmed
    Step(1.2, "stock_again", "1"),  # back to the stock widgets' page, from the page cache
    Step(1.2, "palette", "ctrl+p"),
    Step(1.2, "palette_type", "glass"),
    Step(1.0, "palette_close", "escape"),
    Step(1.0, "full", "f9"),
    Step(3.2, "idle_full"),
    Step(2.5, "resize", RESIZED),
    Step(2.0, "q", "q"),
]


@pytest.fixture(scope="module")
def tour() -> tuple[Capture, dict[str, Replay]]:
    capture = run_app(SHOWCASE, TOUR, size=SIZE)
    snaps = {}
    for label, rp in replay_steps(capture, SIZE, os.environ.get("GLASS_PNG_DIR")):
        snaps[label] = rp.snapshot()
    return capture, snaps


def _glass_placements(rp: Replay) -> list[dict]:
    return [p for p in rp.model.placements.values() if not p["virtual"]]


def _backdrop(rp: Replay) -> tuple[int, dict]:
    (found,) = [(i, p) for (i, _), p in rp.model.placements.items() if p["z"] == Z0]
    return found


def _row_with(rp: Replay, text: str) -> int:
    return next(y for y in range(rp.size[1]) if text in rp.text(y))


def _virtual(rp: Replay, image_id: int) -> dict:
    return next(p for (i, _), p in rp.model.placements.items() if i == image_id and p["virtual"])


# -- the stock widgets' view -------------------------------------------------------------------


def test_clean_exit_and_protocol_hygiene(tour: tuple[Capture, dict[str, Replay]]) -> None:
    capture, snaps = tour
    raw = capture.raw
    assert capture.code == 0
    assert b"Traceback" not in raw
    model = snaps["end"].model
    assert not model.errors, model.errors[:5]
    assert model.no_quiet == 0, "every graphics command must carry q=2"
    assert model.queries == 0, "with an environment hint nothing is queried"
    assert model.pending is None, "a chunked transfer was left open"
    assert not model.images, "every image is deleted on exit"
    leave = raw.rfind(b"\x1b[?1049l")
    assert leave > raw.rfind(b"\x1b_G"), "graphics after leaving the alt screen"
    assert raw.rfind(b"\x1b]111\x07") > raw.rfind(b"\x1b]11;#"), "the default background is reset"
    assert b"d=A" not in raw and b"d=R" not in raw, "a clean exit leaves the hard-exit cleanup unused"


def test_everything_sits_below_cell_backgrounds(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    for label, rp in snaps.items():
        for p in _glass_placements(rp):
            assert p["z"] < Z_BELOW_BACKGROUND, (label, p)


def test_text_and_backdrop(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    rp = snaps["down"]
    _, backdrop = _backdrop(rp)
    assert (backdrop["c"], backdrop["r"]) == SIZE
    text = rp.all_text()
    assert "Services" in text and "Incidents (6)" in text and "Deploy queue" in text and "Rollouts" in text
    assert not BOX_GLYPHS & set(text), "border glyphs are drawn in pixels, not text"
    bgs = [bg for row in rp.cell_bgs() for bg in row]
    assert bgs.count("default") > 0.95 * len(bgs)
    row = _row_with(rp, "INC-231")
    assert rp.cell_bgs()[row][4] != "default", "the severity badge keeps its own background"


def test_cursor_moves_with_partial_edits(tour: tuple[Capture, dict[str, Replay]]) -> None:
    capture, _ = tour
    sent = capture.between(capture.mark_time("down"), capture.mark_time("detail"))
    assert b"a=t" not in sent, "moving the cursor re-uploads nothing"
    assert re.search(rb"a=f,i=\d+,r=1", sent), "the pill moves with in-place edits"


def test_idle_sends_nothing(tour: tuple[Capture, dict[str, Replay]]) -> None:
    capture, _ = tour
    for start, end in (("idle_subtle", "down"), ("pixels_idle", "pixels_down"), ("idle_full", "resize")):
        t0, t1 = capture.mark_time(start), capture.mark_time(end)
        assert graphics_in(capture.between(t0 + 0.2, t1)) == 0, f"graphics while idle ({start})"


def test_full_motion_is_played_by_the_terminal(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    model = snaps["idle_full"].model
    playing = [img for img in model.images.values() if img["state"] == 3]
    assert len(playing) >= 2, "shimmer, glints and halo loop in the terminal"
    assert all(len(img["frames"]) > 1 for img in playing)


def test_detail_modal_gets_glass(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    rp = snaps["back"]  # the service's details, just before Escape
    zs = [p["z"] for p in _glass_placements(rp)]
    assert Z_SCRIM_0 in zs and any(z > Z_SCRIM_0 for z in zs), "a scrim, and the dialog's own glass"
    text = rp.all_text()
    assert "Recent deploys" in text and "Notes" in text
    assert not rp.model.errors


def test_modal_scrim_dialog_and_image(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    rp = snaps["close"]  # just before Escape closes the "ready to ship" modal
    zs = sorted(p["z"] for p in _glass_placements(rp))
    assert Z_SCRIM_0 in zs, "the scrim dims level 0"
    assert any(z >= -1_900_000_000 for z in zs), "the dialog has its own glass layer"
    assert "Ready for launch" in rp.all_text()
    ids = rp.placeholder_ids()
    assert len(ids) == 1, "the picture is the only image on screen"
    (image_id,) = ids
    virtual = _virtual(rp, image_id)
    assert (virtual["c"], virtual["r"]) == ART_CELLS
    image = rp.model.images[image_id]
    assert image["w"] >= ART_CELLS[0] * 4, "the picture is real pixels, scaled up crisply"
    assert len(image["frames"]) > 2 and image["state"] == 3, "the rocket's frames are uploaded, then played"
    assert image["gaps"][-1] < 0, "with a gapless dimmed copy last"
    row = _row_with(rp, "INC-231")
    badge = rp.cell_bgs()[row][4]
    assert badge not in ("default", snaps["down"].cell_bgs()[row][4]), "the badge under the modal is dimmed"
    after = snaps["pixels"]
    assert all(p["z"] < Z_SCRIM_0 for p in _glass_placements(after)), "modal layers are gone after Escape"


def test_command_palette_gets_glass(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    zs = [p["z"] for p in _glass_placements(snaps["palette_close"])]
    assert Z_SCRIM_0 in zs and any(z > Z_SCRIM_0 for z in zs)
    assert "Glass: motion" in snaps["palette_close"].all_text()


def test_resize_rerenders(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    model = snaps["q"].model
    image_id, placement = _backdrop(snaps["q"])
    assert (placement["c"], placement["r"]) == RESIZED
    assert (model.images[image_id]["w"], model.images[image_id]["h"]) == (RESIZED[0] * CW, RESIZED[1] * CH)
    assert "Activity" not in snaps["q"].all_text(), "at 28 rows the activity log makes room"


# -- the pixel widgets' view -------------------------------------------------------------------


def test_page_switch_lands_in_the_same_frame(tour: tuple[Capture, dict[str, Replay]]) -> None:
    """The new page's backdrop is placed in the synchronized update that shows its text (rule 19)."""
    capture, _ = tour
    sent = capture.between(capture.mark_time("pixels"), capture.mark_time("pixels_idle"))
    frames = sent.split(b"\x1b[?2026l")
    first = next(i for i, frame in enumerate(frames) if b"CLUSTER" in frame)
    assert re.search(rb"\x1b_Ga=[Tp][^\x1b]*z=-2000000000", frames[first]), "backdrop and text change together"


def test_pixel_widgets_and_their_sprite(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    rp = snaps["pixels_idle"]
    text = rp.all_text()
    for label in ("CLUSTER", "RELEASES", "SERVICES", "DEPLOYS", "REQUESTS / MIN", "ERRORS / HOUR", "58%", "62%"):
        assert label in text, label
    assert "Index shard 3 is not answering" in text, "at 45 rows each service has a second line"
    assert not BOX_GLYPHS & set(text) and not rp.model.errors
    bgs = [bg for row in rp.cell_bgs() for bg in row]
    assert bgs.count("default") > 0.98 * len(bgs), "pixel widgets leave every cell to the glass"
    ids = rp.placeholder_ids()
    assert len(ids) == 1, "the detail panel's beacon is a sprite"
    (sprite,) = ids
    virtual = _virtual(rp, sprite)
    assert (virtual["c"], virtual["r"]) == BEACON_CELLS
    image = rp.model.images[sprite]
    assert len(image["frames"]) > 2 and image["state"] == 3, "the terminal plays the sprite"
    assert image["gaps"][-1] < 0, "its dimmed copy is a gapless last frame"


def test_select_list_pill_slides_with_edits(tour: tuple[Capture, dict[str, Replay]]) -> None:
    capture, snaps = tour
    backdrop, _ = _backdrop(snaps["pixels_down"])
    sent = capture.between(capture.mark_time("pixels_down"), capture.mark_time("pixels_modal"))
    assert re.search(rb"a=f,i=%d,r=1" % backdrop, sent), "the pill and the row meters move with in-place edits"
    assert not re.search(rb"a=t,i=%d," % backdrop, sent), "the backdrop is not uploaded again"
    assert _backdrop(snaps["pixels_modal"])[0] == backdrop
    before, after = snaps["pixels_down"].placeholder_ids(), snaps["pixels_modal"].placeholder_ids()
    assert len(after) == 1 and after != before, "the next service's state is another sprite"
    assert snaps["pixels_modal"].model.images[next(iter(after))]["state"] == 3
    assert "CHECKOUT" in snaps["pixels_modal"].all_text(), "the detail panel follows the cursor"


def test_sprites_dim_under_a_modal_and_wake_after(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    (sprite,) = snaps["pixels_modal"].placeholder_ids()
    under = snaps["pixels_close"].model.images[sprite]
    assert under["state"] == 1 and under["cur"] == len(under["frames"]) - 1, "stopped on its dimmed frame"
    awake = snaps["stock_again"].model.images[sprite]
    assert awake["state"] == 3 and awake["cur"] == 0, "playing again once the modal is gone"


def test_cached_page_comes_back(tour: tuple[Capture, dict[str, Replay]]) -> None:
    capture, snaps = tour
    stock, _ = _backdrop(snaps["down"])
    assert _backdrop(snaps["pixels_idle"])[0] != stock, "the pixel widgets' page has its own backdrop"
    assert _backdrop(snaps["palette"])[0] == stock, "the stock page's backdrop is placed again, not redrawn"
    sent = capture.between(capture.mark_time("stock_again"), capture.mark_time("palette"))
    assert not re.search(rb"a=t,i=%d," % stock, sent)


def test_pixel_widgets_stay_transparent_under_a_modal(tour: tuple[Capture, dict[str, Replay]]) -> None:
    _, snaps = tour
    rp = snaps["pixels_close"]
    row = _row_with(snaps["pixels_idle"], "58%")
    assert rp.cell_bgs()[row][2:12] == ["default"] * 10, "the ring gauge's cells still show the (dimmed) glass"


# -- fallback ------------------------------------------------------------------------------

QUIT = [Step(2.5, "start"), Step(0.0, "q", "q")]


def _plain_checks(capture: Capture) -> None:
    raw = capture.raw
    assert capture.code == 0 and b"Traceback" not in raw
    assert b"\x1b_G" not in raw.replace(b"a=q", b"").split(b"\x1b[?1049h", 1)[-1], "no graphics once Textual runs"
    assert b"\x1b]11;" not in raw
    rp = Replay(*SIZE)
    for _, data in capture.chunks[:-1]:
        rp.feed(data)
    assert "Services" in rp.all_text()


@pytest.mark.parametrize("label, kwargs", [
    ("--plain", dict(argv=[*SHOWCASE, "--plain"])),
    ("TEXTUAL_GLASS=off", dict(env={"TEXTUAL_GLASS": "off"})),
    ("xterm-256color, no answer", dict(term="xterm-256color")),
])
def test_fallback_is_stock_textual(label: str, kwargs: dict) -> None:
    argv = kwargs.pop("argv", SHOWCASE)
    capture = run_app(argv, QUIT, size=SIZE, **kwargs)
    _plain_checks(capture)
    if label.startswith("xterm"):
        raw = capture.raw
        query = raw.find(b"a=q")
        assert 0 <= query < raw.find(b"\x1b[?1049h"), "the query is sent before Textual starts"
        assert graphics_in(raw) == 1
    else:
        assert graphics_in(capture.raw) == 0


def test_a_terminal_that_answers_the_query_gets_glass() -> None:
    capture = run_app(SHOWCASE, QUIT, size=SIZE, term="xterm-256color", answer=True)
    assert capture.code == 0
    rp = Replay(*SIZE)
    for _, data in capture.chunks:
        rp.feed(data)
    assert rp.model.counts.get("t", 0) >= 1 and not rp.model.errors


def test_screenshots_are_written_for_the_named_steps(tmp_path: Path) -> None:
    out = str(tmp_path / "shots")
    paths = shoot(SHOWCASE, ROOT, RESIZED, QUIT, ("start",), out, prefix="x_")
    assert paths == [os.path.join(out, "x_90x28_start.png")]
    with Image.open(paths[0]) as picture:
        assert picture.size == (RESIZED[0] * CW, RESIZED[1] * CH)
    with pytest.raises(SystemExit, match="exit code 3"):  # a failed run is an error, not a picture
        shoot([sys.executable, "-c", "raise SystemExit(3)"], ROOT, RESIZED, [Step(0.5, "start")], ("start",), out)
    assert os.listdir(out) == ["x_90x28_start.png"]


# -- hard exits ------------------------------------------------------------------------------

HARD_EXIT_SCRIPT = '''
import os, signal, sys, time
from textual_glass import on_hard_exit
mode = sys.argv[1]
if mode == "chain":
    signal.signal(signal.SIGTERM, lambda signum, frame: os.write(2, b"<previous>"))
if mode == "nohup":
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
on_hard_exit("t.one", "<one>")
on_hard_exit("t.two", "<two>")
on_hard_exit("t.one", "<ONE>")
on_hard_exit("t.three", "<three>")
on_hard_exit("t.three", None)
if mode == "clean":
    on_hard_exit("t.one", None)
    on_hard_exit("t.two", None)
if mode in ("atexit", "clean"):
    sys.exit(0)
open("ready", "w").close()
time.sleep(20)
'''


@pytest.mark.parametrize("mode, signums, code, expected", [
    ("plain", [signal.SIGTERM], -signal.SIGTERM, b"<ONE><two>"),
    ("plain", [signal.SIGHUP], -signal.SIGHUP, b"<ONE><two>"),
    ("chain", [signal.SIGTERM], -signal.SIGTERM, b"<ONE><two><previous>"),
    ("nohup", [signal.SIGHUP, signal.SIGTERM], -signal.SIGTERM, b"<ONE><two>"),
    ("atexit", [], 0, b"<ONE><two>"),
    ("clean", [], 0, b""),
])
def test_on_hard_exit_writes_on_signals_and_at_exit(tmp_path: Path, mode: str, signums: list[int], code: int,
                                                    expected: bytes) -> None:
    """No app, no glass: SIGTERM and SIGHUP write the entries, call the handler that was there
    before, and end the process as the default would; an ignored signal stays ignored; an
    interpreter exit writes whatever is still registered."""
    script = tmp_path / "script.py"
    script.write_text(HARD_EXIT_SCRIPT)

    def probe(label: str, pid: int) -> None:
        if not signums:
            return
        deadline = time.monotonic() + 15
        while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        for signum in signums:
            os.kill(pid, signum)
            time.sleep(0.3)

    capture = harness.run_app([sys.executable, str(script), mode], [Step(0.2, "signal")], str(tmp_path),
                              size=(40, 10), timeout=20, probe=probe)
    out = capture.raw
    assert capture.code == code, (capture.code, out[-300:])
    assert b"<three>" not in out and b"<one>" not in out
    if expected:
        assert expected in out and out.count(b"<ONE>") == 1
    else:
        assert b"<ONE>" not in out and b"<two>" not in out


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_hard_exit_still_cleans_the_terminal(signum: int) -> None:
    """Killed with sprites on screen: OSC 111 and every glass image deleted, the
    sprites behind placeholders included (Ghostty's delete-all keeps those)."""
    def probe(label: str, pid: int) -> None:
        if label == "kill":
            os.kill(pid, signum)

    steps = [Step(3.0, "start"), Step(0.0, "pixels", "2"), Step(2.0, "kill")]
    capture = run_app(SHOWCASE, steps, size=RESIZED, timeout=10, probe=probe)
    raw = capture.raw
    assert capture.code == -signum, capture.code
    assert raw.rfind(b"\x1b]111\x07") > raw.rfind(b"\x1b]11;#"), "the default background is reset"
    rp = Replay(*RESIZED)
    sprites = set()
    for _, data in capture.chunks:
        rp.feed(data)
        sprites |= {i for (i, _), p in rp.model.placements.items() if p["virtual"]}
    assert sprites, "a sprite was on screen"
    assert not rp.model.images and not rp.model.errors, rp.model.errors[:3]


def test_a_hard_exit_without_glass_writes_no_graphics() -> None:
    def probe(label: str, pid: int) -> None:
        if label == "kill":
            os.kill(pid, signal.SIGTERM)

    capture = run_app(SHOWCASE, [Step(3.0, "kill")], size=RESIZED, timeout=10, probe=probe,
                      env={"TEXTUAL_GLASS": "off"})
    assert capture.code == -signal.SIGTERM
    assert graphics_in(capture.raw) == 0 and b"\x1b]111" not in capture.raw


STRESS = (
    [Step(2.5, "start"), Step(0.0, "resize early", (150, 42))]
    + [Step(0.03, "down", "down")] * 8 + [Step(0.03, "up", "up")] * 6
    + [Step(0.05, "ship", "m"), Step(0.05, "esc", "escape"), Step(0.05, "ship", "m"),
       Step(0.2, "resize in modal", (120, 36)),
       Step(0.3, "esc", "escape"), Step(0.05, "palette", "ctrl+p"), Step(0.1, "type", "gl"), Step(0.1, "esc", "escape"),
       Step(0.1, "f9", "f9"), Step(0.3, "f9", "f9"), Step(0.3, "f9", "f9"), Step(0.05, "2", "2"),
       Step(0.05, "down", "down"), Step(0.05, "down", "down"), Step(0.05, "restart", "r"), Step(0.05, "1", "1"),
       Step(0.05, "2", "2"), Step(0.1, "ship", "m"), Step(0.2, "resize in modal", (90, 28)),
       Step(0.1, "ship it", "enter"), Step(0.05, "1", "1"), Step(0.05, "off", "shift+f9"), Step(0.4, "on", "shift+f9"),
       Step(0.05, "detail", "enter"), Step(0.3, "back", "escape"), Step(0.3, "resize", (140, 45)),
       Step(0.05, "tab", "tab")] + [Step(0.02, "down", "down")] * 10 + [Step(1.5, "q", "q")]
)


def test_stress() -> None:
    capture = run_app(SHOWCASE, STRESS, size=SIZE)
    raw = capture.raw
    assert capture.code == 0 and b"Traceback" not in raw
    rp = Replay(*SIZE)
    for _, data in capture.chunks:
        rp.feed(data)
    model = rp.model
    assert not model.errors, model.errors[:5]
    assert model.no_quiet == 0 and model.pending is None
    assert not model.images


def main() -> Iterator[None] | int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--png", help="write composite PNGs of each step here")
    args = parser.parse_args()
    if args.png:
        os.environ["GLASS_PNG_DIR"] = args.png
    return pytest.main([__file__, "-v"])


if __name__ == "__main__":
    sys.exit(main())  # type: ignore[arg-type]
