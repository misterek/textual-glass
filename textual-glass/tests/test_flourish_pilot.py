"""Flourishes in a running app (``app.glass.flourish``), with glass forced on:
uploaded while stopped, placed over the stage at the anchor's level, stepped by
glass, deleted after the last frame, and dropped whenever the window, the theme,
the motion level or the anchor's popup changes. The makers are test kinds, so
nothing here depends on how the real ones look.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Horizontal
from textual.geometry import Region
from textual.screen import ModalScreen, Screen
from textual.widgets import Static

from textual_glass import FLOURISHES, Glass, kitty
from textual_glass.effects import Flight
from textual_glass.engine import (
    LEVEL_STEP,
    MAIN,
    MAX_FLOURISHES,
    MAX_LEVELS,
    Z0,
    Z_AMBIENT,
    Z_FLOAT,
    Z_FLOURISH,
    Z_SCRIM,
)
from textual_glass.flourishes import Flourish, Stage, stage_cells
from textual_glass.testing.pilot_helpers import Output, idle, settle, start

SIZE = (90, 28)
STEADY = (4, 60)  # frames, ms each
LONG = (10, 300)
Maker = Callable[[Stage], tuple[list[bytes], list[int]]]


def frames_of(count: int, gap: int, seen: list[Stage] | None = None) -> Maker:
    """A test maker: ``count`` frames that differ (their alpha grows), transparent at the border."""

    def make(stage: Stage) -> tuple[list[bytes], list[int]]:
        if seen is not None:
            seen.append(stage)
        out = []
        for k in range(count):
            a = np.zeros((stage.height, stage.width, 4), np.uint8)
            a[1:-1, 1:-1] = (255, 255, 255, 20 * (k + 1))
            out.append(a.tobytes())
        return out, [gap] * count

    return make


@pytest.fixture
def gate() -> Iterator[threading.Event]:
    event = threading.Event()
    yield event
    event.set()  # never leave the worker thread waiting


@pytest.fixture
def kinds(monkeypatch: pytest.MonkeyPatch, gate: threading.Event) -> list[Stage]:
    """Test kinds: ``test-steady`` (4 x 60 ms), ``test-long`` (10 x 300 ms) and ``test-gated``,
    whose maker waits for ``gate``. Returns the stages the makers were given."""
    seen: list[Stage] = []

    def gated(stage: Stage) -> tuple[list[bytes], list[int]]:
        gate.wait(10)
        return frames_of(*STEADY)(stage)

    monkeypatch.setitem(FLOURISHES, "test-steady", Flourish(frames_of(*STEADY, seen), grow=(2, 1),
                                                            color="success", accent="warning"))
    monkeypatch.setitem(FLOURISHES, "test-long", Flourish(frames_of(*LONG, seen), grow=(2, 1)))
    monkeypatch.setitem(FLOURISHES, "test-gated", Flourish(gated))
    return seen


class Popup(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", "Close")]
    DEFAULT_CSS = """
    Popup { align: center middle; }
    #pop { width: 40; height: 8; border: round $accent; background: $panel; }
    """

    def compose(self) -> ComposeResult:
        yield Static("a popup", id="pop")


class Cover(Screen[None]):
    """A full screen: the one below it is hidden."""

    DEFAULT_CSS = "#cover { border: round $primary; height: 1fr; }"

    def compose(self) -> ComposeResult:
        yield Static("covers everything", id="cover")


class Board(Glass, App[None]):
    CSS = """
    Horizontal { height: 1fr; }
    .panel { width: 1fr; border: round $primary; }
    #hidden { display: none; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static("left", classes="panel", id="left")
            yield Static("right", classes="panel", id="right")
        yield Static("hidden", id="hidden")


def cells_of(widget: Any) -> tuple[int, int, int, int]:
    r = widget.region
    return r.x, r.y, r.width, r.height


def placement(text: str, image_id: int) -> tuple[int, int, int, int, int]:
    """Where an image was placed: column, row, columns, rows and z."""
    found = re.search(rf"\x1b7\x1b\[(\d+);(\d+)H\x1b_Ga=p,i={image_id},p=1,c=(\d+),r=(\d+),z=(-?\d+),C=1", text)
    assert found, f"image {image_id} was never placed"
    row, col, cols, rows, z = (int(v) for v in found.groups())
    return col - 1, row - 1, cols, rows, z


async def landed(app: Any, pilot: Any) -> Flight:
    """Wait until the last flourish fired is uploaded and playing."""
    for _ in range(300):
        await asyncio.sleep(0.01)
        await pilot.pause()
        if app.glass.effects.flights and not app.glass.effects.rendering:
            return app.glass.effects.flights[-1]
    raise AssertionError("the flourish never landed")


def _ticking(app: Any) -> bool:
    timer = app.glass._timer
    return timer is not None and timer._active.is_set()


def clean(output: Output) -> None:
    model = output.model()
    assert model.errors == [] and model.no_quiet == 0 and model.queries == 0


# -- playing -------------------------------------------------------------------------------


async def test_a_flourish_is_uploaded_stopped_stepped_by_glass_and_deleted_after_its_last_frame(
        output: Output, kinds: list[Stage]) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        left = app.query_one("#left")
        assert engine.stats()["flourishes"] == 0
        mark = output.mark()
        assert engine.flourish("test-steady", at=left)
        assert engine.stats()["flourishes"] == 1, "counted while it renders"
        flight = await landed(app, pilot)
        image_id, count = flight.image_id, STEADY[0]
        stage = kinds[-1]
        uploaded = output.model().images[image_id]
        assert len(uploaded["frames"]) == count and uploaded["state"] == 1, "every frame up, never started"
        assert (uploaded["w"], uploaded["h"]) == (stage.width, stage.height)

        while engine.effects.flights:  # the real 60 Hz timer runs too
            engine.tick()
            await asyncio.sleep(0.016)
        gone_at = time.monotonic()
        assert gone_at - flight.t0 >= count * STEADY[1] / 1000, "deleted only after its last frame"
        sent = output.since(mark)
        assert not re.search(rf"a=a,i={image_id},[^\x1b]*s=", sent), "never played by the terminal (rule 2)"
        x, y, w, h, z = placement(sent, image_id)
        assert (x, y, w, h) == stage_cells(FLOURISHES["test-steady"], cells_of(left), None, app.size)
        assert engine.layers[MAIN].z + Z_AMBIENT < z == Z0 + Z_FLOURISH < Z0 + Z_FLOAT < Z0 + LEVEL_STEP + Z_SCRIM
        assert z < kitty.Z_BELOW_BACKGROUND
        steps = [int(c) for c in re.findall(rf"a=a,i={image_id},c=(\d+)", sent)]
        assert steps and steps == sorted(set(steps)) and set(steps) <= set(range(2, count + 1)), steps
        assert sent.index(f"a=d,d=I,i={image_id},") > sent.rindex(f"a=a,i={image_id},c="), "deleted after the steps"
        model = output.model()
        assert image_id not in model.images and image_id not in engine.ids.live
        assert engine.stats()["flourishes"] == 0

        engine.tick()  # nothing animates: the timer pauses
        assert not _ticking(app)
        quiet = output.mark()
        for _ in range(5):
            await pilot.pause()
            engine.on_frame()
            await asyncio.sleep(0.02)
        assert output.since(quiet) == "", "an idle window sends nothing (rule 9)"
        assert not _ticking(app)
    clean(output)


def test_the_flourish_layer_sits_between_a_levels_glass_and_the_next_scrim_at_every_level() -> None:
    for level in range(MAX_LEVELS):
        base = Z0 + level * LEVEL_STEP
        z = base + Z_FLOURISH
        assert base + Z_AMBIENT + 5 < z < base + Z_FLOAT, "over the glass, the ambient layer and the halo"
        assert z < base + LEVEL_STEP + Z_SCRIM, "under the next popup's scrim"
        assert z < kitty.Z_BELOW_BACKGROUND, f"level {level}: under cell backgrounds (rule 4)"


async def test_a_flourish_plays_on_its_anchors_level(output: Output, kinds: list[Stage]) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        app.push_screen(Popup())
        await idle(app, pilot)
        pop = app.screen.query_one("#pop")
        mark = output.mark()
        assert engine.flourish("test-long", at=pop)
        on_popup = await landed(app, pilot)
        assert engine.flourish("test-long", at=app.screen_stack[0].query_one("#left"))
        below = await landed(app, pilot)
        sent = output.since(mark)
        scrim, dialog = engine.effects.scrims[1].z, engine.layers[(1, "main")].z
        z_popup, z_below = placement(sent, on_popup.image_id)[4], placement(sent, below.image_id)[4]
        assert scrim < dialog < z_popup == Z0 + LEVEL_STEP + Z_FLOURISH < Z0 + LEVEL_STEP + Z_FLOAT, \
            "on the popup: over its scrim and its glass"
        assert z_below == Z0 + Z_FLOURISH < scrim, "below the popup: under its scrim"
        assert engine.flourish("test-long", at=Region(2, 2, 10, 3))
        region = await landed(app, pilot)
        assert placement(output.since(mark), region.image_id)[4] == z_popup, "a region is on the top screen"
    clean(output)


async def test_the_stage_comes_from_the_anchor_the_target_and_the_palette(output: Output,
                                                                       kinds: list[Stage]) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        left, right, hidden = app.query_one("#left"), app.query_one("#right"), app.query_one("#hidden")
        palette = engine.palette
        assert palette is not None
        for kwargs in ({"at": left, "to": right}, {"at": left, "color": "#ff0000"},
                       {"at": left, "to": hidden, "color": (1, 2, 3)}, {"color": Color(9, 8, 7)}):
            assert engine.flourish("test-steady", **kwargs)
            await landed(app, pilot)
            await idle(app, pilot)
        travel, red, tuple_colour, window = kinds
        assert travel.color == palette.success and travel.accent == palette.warning, "the kind's palette roles"
        assert travel.to is not None and travel.to[0] > travel.at[0], "the target is on the stage"
        assert red.color == (255, 0, 0) and red.accent == palette.warning and red.to is None
        assert tuple_colour.color == (1, 2, 3) and tuple_colour.to is None, "a target that isn't shown is left out"
        assert window.color == (9, 8, 7) and window.window_edges == (True, True, True, True), "no anchor: the window"
        assert len({s.seed for s in kinds}) == 4, "each one gets a seed of its own"
    clean(output)


# -- when it does nothing -------------------------------------------------------------------


async def test_a_flourish_is_a_no_op_when_it_cannot_play(output: Output, kinds: list[Stage]) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        engine = app.glass
        left = app.query_one("#left")
        assert not engine.flourish("test-steady", at=left), "glass is off"
        with pytest.raises(ValueError):
            engine.flourish("no-such-kind", at=left)
        with pytest.raises(ValueError):
            engine.flourish("test-steady", color="not a colour")
        assert output.chunks == []

        await start(app, pilot)
        mark = output.mark()
        assert not engine.flourish("test-steady", at=app.query_one("#hidden")), "no area"
        assert not engine.flourish("test-steady", at=Static("never mounted")), "not on a screen"
        assert not engine.flourish("test-steady", at=Region(200, 50, 10, 4)), "outside the window"
        engine.set_motion("minimal")
        assert not engine.flourish("test-steady", at=left), "minimal motion"
        engine.set_motion("subtle")
        await settle(app, pilot)
        assert output.since(mark) == "" and not kinds

        app.push_screen(Cover())
        await idle(app, pilot)
        mark = output.mark()
        assert not engine.flourish("test-steady", at=left), "its screen is hidden"
        await settle(app, pilot)
        assert output.since(mark) == "" and not kinds and engine.effects.fired == 0
        assert engine.flourish("test-steady", at=app.screen.query_one("#cover")), "the top screen plays"
    clean(output)


async def test_at_most_max_flourishes_play_at_once(output: Output, kinds: list[Stage]) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        left = app.query_one("#left")
        assert all(engine.flourish("test-steady", at=left) for _ in range(MAX_FLOURISHES)), "the same one plays twice"
        assert not engine.flourish("test-steady", at=left), "rendering counts"
        await landed(app, pilot)
        assert engine.stats()["flourishes"] == MAX_FLOURISHES
        assert len({f.image_id for f in engine.effects.flights}) == MAX_FLOURISHES
        assert not engine.flourish("test-steady"), "playing counts"
        while engine.effects.flights:
            engine.tick()
            await asyncio.sleep(0.016)
        assert engine.flourish("test-steady", at=left), "room again once they end"
        await idle(app, pilot)
    clean(output)


# -- dropped ----------------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["resize", "theme", "minimal", "popup closes"])
async def test_flourishes_are_dropped_when_their_world_changes(output: Output, kinds: list[Stage],
                                                               gate: threading.Event, change: str) -> None:
    """A playing flourish is deleted at once, and one still rendering is never placed."""
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        if change == "popup closes":
            app.push_screen(Popup())
            await idle(app, pilot)
            anchor = app.screen.query_one("#pop")
        else:
            anchor = app.query_one("#left")
        assert engine.flourish("test-long", at=anchor)
        playing = await landed(app, pilot)
        assert engine.flourish("test-gated", at=anchor)
        (rendering,) = engine.effects.rendering
        if change == "resize":
            await pilot.resize_terminal(80, 24)
        elif change == "theme":
            app.theme = "nord"
        elif change == "minimal":
            engine.set_motion("minimal")
        else:
            await pilot.press("escape")
        await pilot.pause()
        engine.on_frame()
        assert not engine.effects.flights, "deleted in the same frame"
        model = output.model()
        assert playing.image_id not in model.images and playing.image_id not in engine.ids.live
        gate.set()
        await idle(app, pilot)
        assert not engine.effects.flights and engine.stats()["flourishes"] == 0
        assert not re.search(rf"a=[tp],i={rendering},", output.text), "the late render was dropped"
        assert rendering not in engine.ids.live
    clean(output)


async def test_suspend_and_resume_mid_flight_leave_nothing_behind(output: Output, kinds: list[Stage],
                                                                  gate: threading.Event) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        left = app.query_one("#left")
        assert engine.flourish("test-long", at=left)
        playing = await landed(app, pilot)
        assert engine.flourish("test-gated", at=left)
        (rendering,) = engine.effects.rendering
        app.app_suspend_signal.publish(app)
        model = output.model()
        assert not model.images and not engine.effects.flights and not engine.effects.rendering
        gate.set()
        app.app_resume_signal.publish(app)
        await idle(app, pilot)
        model = output.model()
        assert engine.active and not engine.effects.flights and engine.stats()["flourishes"] == 0
        assert playing.image_id not in model.images, "nothing comes back"
        assert not re.search(rf"a=[tp],i={rendering},", output.text), "the late render was dropped"
        assert engine.flourish("test-steady", at=left), "and the next one plays"
        await landed(app, pilot)
    clean(output)


@pytest.mark.parametrize("broken", ["raises", "bad frames"])
async def test_a_broken_maker_counts_as_an_error_and_leaves_nothing(output: Output, monkeypatch: pytest.MonkeyPatch,
                                                                   broken: str) -> None:
    def make(stage: Stage) -> tuple[list[bytes], list[int]]:
        if broken == "raises":
            raise RuntimeError("the maker broke")
        return [b"\0" * 12], [50]

    monkeypatch.setitem(FLOURISHES, "comet", Flourish(make))
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        live = set(engine.ids.live)
        mark = output.mark()
        assert engine.flourish("comet", at=app.query_one("#left")), "it is only known to fail once it renders"
        await idle(app, pilot)
        assert engine.errors == 1 and engine.active
        assert ("the maker broke" if broken == "raises" else "RGBA bytes") in (engine.last_error or "")
        assert engine.stats()["flourishes"] == 0 and engine.ids.live == live
        assert output.since(mark) == ""
    clean(output)

