"""Scenes in a running app (``app.glass.play_scene``), with glass forced on: every
actor uploaded with the others, loops started after their frames, one-shots
stepped by glass; native actors re-placed at pixel offsets below the cell size and
cropped at the stage's edge, stretched ones on whole cells; cues once and in
order; the hold in silence; and each way a scene ends, its images deleted and its
ids freed. The actors here are tiny test pictures and the scenes' clock is a fake
one the tests move; the demo's test plays the demo's real show.
"""

from __future__ import annotations

import asyncio
import importlib.util
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Hashable, Iterator
from typing import Any

import numpy as np
import pyte
import pytest
from textual._context import active_app
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.geometry import Region
from textual.screen import ModalScreen, Screen
from textual.widgets import ContentSwitcher, Static

from textual_glass import Glass, kitty
from textual_glass import engine as engine_module
from textual_glass.engine import LEVEL_STEP, MAX_LEVELS, Z0, Z_AMBIENT, Z_FLOAT, Z_FLOURISH, Z_SCENE, Z_SCRIM
from textual_glass.player import MAX_SCENES, Showing
from textual_glass.scenes import MAX_ACTORS, ROLES, Actor, Cue, Route, Scene, Stage
from textual_glass.testing.kitty_emu import KittyModel, composite
from textual_glass.testing.pilot_helpers import Output, idle, settle, start

SIZE = (90, 28)
CELL = (9.0, 19.0)
MAGENTA = (255, 0, 255)


# -- the setting ---------------------------------------------------------------------------


@pytest.fixture
def cell(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> tuple[float, float]:
    """The terminal's cell size in pixels (9x19 unless a test is parametrized with ``cell``)."""
    size = getattr(request, "param", CELL)
    monkeypatch.setattr(engine_module, "terminal_cell_pixels", lambda: (*size, True))
    return size


class Clock:
    """The scenes' clock, moved by the test."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def gate() -> Iterator[threading.Event]:
    event = threading.Event()
    yield event
    event.set()  # never leave the worker thread waiting


class Log:
    """What the app heard: every cue and every end, as they came, on which thread."""

    def __init__(self) -> None:
        self.cues: list[Hashable] = []
        self.ends: list[str] = []
        self.threads: set[str] = set()
        self.apps: list[Any] = []

    def cue(self, data: Hashable) -> None:
        self.threads.add(threading.current_thread().name)
        self.apps.append(active_app.get(None))
        self.cues.append(data)

    def end(self, reason: str) -> None:
        self.threads.add(threading.current_thread().name)
        self.ends.append(reason)


class Popup(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", "Close")]
    DEFAULT_CSS = """
    Popup { align: center middle; }
    #pop { width: 40; height: 10; border: round $accent; background: $panel; }
    """

    def compose(self) -> ComposeResult:
        yield Static("a popup", id="pop")


class Cover(Screen[None]):
    DEFAULT_CSS = "#cover { border: round $primary; height: 1fr; }"

    def compose(self) -> ComposeResult:
        yield Static("covers everything", id="cover")


class Board(Glass, App[None]):
    CSS = """
    Horizontal { height: 1fr; }
    .panel { width: 1fr; height: 1fr; border: round $primary; }
    #hidden { display: none; }
    ContentSwitcher { height: 8; }
    .page { height: 8; border: round $secondary; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static("left", classes="panel", id="left")
            yield Static("right", classes="panel", id="right")
        with ContentSwitcher(initial="page1"):
            yield Static("page one", classes="page", id="page1")
            yield Static("page two", classes="page", id="page2")
        yield Static("hidden", id="hidden")


def picture(width: int, height: int, frames: int = 1, color: tuple[int, int, int] = MAGENTA) -> list[bytes]:
    """Test frames: a solid colour with a transparent one-pixel border (rule 17); the alpha grows per frame."""
    out = []
    for k in range(frames):
        a = np.zeros((height, width, 4), np.uint8)
        a[1:-1, 1:-1] = (*color, 255 - 10 * k)
        out.append(a.tobytes())
    return out


def actor(width: int = 6, height: int = 6, frames: int = 1, **fields: Any) -> Actor:
    fields.setdefault("route", Route.still(20, 20))
    return Actor(frames=picture(width, height, frames), width=width, height=height, **fields)


def maker(scene: Scene, seen: list[Stage] | None = None) -> Callable[[Stage], Scene]:
    def make(stage: Stage) -> Scene:
        if seen is not None:
            seen.append(stage)
        return scene

    return make


async def landed(app: Any, pilot: Any, show: Showing, timeout: float = 10.0) -> None:
    """Wait until the scene's upload has been sent (or it ended)."""
    deadline = time.monotonic() + timeout
    while show._t0 is None and show.ended is None:
        if time.monotonic() > deadline:
            raise AssertionError("the scene never landed")
        await asyncio.sleep(0.01)
    await asyncio.sleep(0)


async def at(app: Any, clock: Clock, show: Showing, t: float) -> None:
    """Move the scenes' clock to ``t`` seconds after the upload, tick, and let the callbacks run."""
    assert show._t0 is not None
    clock.now = show._t0 + t
    app.glass.tick()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def frames(app: Any, pilot: Any, count: int = 3) -> None:
    """A few Textual frames, with glass's frame after each, and the callbacks they queue."""
    for _ in range(count):
        await pilot.pause()
        app.glass.on_frame()
        await asyncio.sleep(0)


def _ticking(app: Any) -> bool:
    timer = app.glass._timer
    return timer is not None and timer._active.is_set()


PLACE = re.compile(r"\x1b7\x1b\[(\d+);(\d+)H\x1b_Ga=p,([^\x1b]*)\x1b\\\x1b8")
COMMAND = re.compile(r"\x1b_G(a=[adp][^;\x1b]*)\x1b\\")


def _keys(control: str) -> dict[str, str]:
    return dict(kv.split("=", 1) for kv in control.split(","))


def placed(text: str, image_id: int) -> list[dict[str, int]]:
    """Every placement of an image, in order: its cell (0-based) and its keys."""
    out = []
    for match in PLACE.finditer(text):
        keys = _keys("a=p," + match.group(3))
        if keys["i"] == str(image_id):
            out.append({"col": int(match.group(2)) - 1, "row": int(match.group(1)) - 1,
                        **{k: int(v) for k, v in keys.items() if k != "a"}})
    return out


def commands(text: str, image_id: int) -> list[str]:
    """What was sent for an image, in order: ``place``, ``unplace``, ``delete``, ``frame N``, ``play``, ``stop``."""
    out = []
    for match in COMMAND.finditer(text):
        keys = _keys(match.group(1))
        if keys.get("i") != str(image_id):
            continue
        if keys["a"] == "p":
            out.append("place")
        elif keys["a"] == "d":
            out.append("delete" if keys["d"] == "I" else f"unplace {keys['p']}" if "p" in keys else "unplace")
        elif keys.get("s") == "3":
            out.append("play")
        elif keys.get("s") == "1":
            out.append("stop")
        elif "c" in keys:
            out.append(f"frame {keys['c']}")
    return out


def clean(output: Output, cell: tuple[float, float] = CELL) -> None:
    model = output.model(cell)
    assert model.errors == [] and model.no_quiet == 0 and model.queries == 0, model.errors


def left_of(app: Any) -> Any:
    return app.query_one("#left")


def cells_of(widget: Any) -> tuple[int, int, int, int]:
    r = widget.region
    return r.x, r.y, r.width, r.height


# -- playing ---------------------------------------------------------------------------------


async def test_a_scene_goes_up_whole_plays_on_time_and_holds_in_silence(output: Output, cell: Any) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        assert engine._player is None and engine.stats()["scenes"] == engine.stats()["actors"] == 0
        clock = engine.player.clock = Clock()
        log, seen = Log(), []
        scene = Scene([
            actor(frames=3, gaps=(80,), route=Route.line((10, 10), (200, 150), 0, 1)),     # a loop that moves
            actor(frames=4, loop=False, gaps=(100, 200, 100, 100), enter=0.5),              # a one-shot
            actor(16, 8, frames=2, cells=(4, 2), z=7, route=Route.still(100, 100)),          # a stretched loop
        ], duration=2.0, cues=[Cue(1.0, "half"), Cue(2.0, "done")])
        mark = output.mark()
        show = engine.play_scene(maker(scene, seen), at=left_of(app), on_cue=log.cue, on_end=log.end)
        assert show is not None and engine.stats()["scenes"] == 1, "counted while it is made"
        await landed(app, pilot, show)
        (stage,) = seen
        x, y, w, h = cells_of(left_of(app))
        assert (stage.cols, stage.rows, stage.cell) == (w, h, CELL)
        assert (stage.width, stage.height) == (round(w * CELL[0]), round(h * CELL[1]))
        assert stage.colors["primary"] == engine.palette.primary and not stage.still and stage.dark
        assert show.stage is stage and engine.stats()["actors"] == 3
        assert show.scene is not None and (show.scene.duration, show.scene.cues) == (scene.duration, scene.cues)
        assert not any(a.frames for a in show.scene.actors), "the frames are let go once they are in the terminal"
        assert [a.route for a in show.scene.actors] == [a.route for a in scene.actors]

        moving, shot, big = (cast.image_id for cast in show._casts)
        model = output.model(CELL)
        assert len(model.images[moving]["frames"]) == 3 and model.images[moving]["state"] == 3, "a loop plays"
        assert len(model.images[shot]["frames"]) == 4 and model.images[shot]["state"] == 1, "a one-shot waits"
        assert model.images[big]["state"] == 3
        sent = output.since(mark)
        for image_id in (moving, big):
            assert sent.index(f"a=a,i={image_id},s=3") > sent.rindex(f"a=f,i={image_id},"), "frames first (rule 2)"
        for index, cast in enumerate(show._casts):
            assert cast.z == Z0 + Z_SCENE + cast.actor.z * MAX_ACTORS + index
            assert engine.layers[(0, "main")].z + Z_AMBIENT + 5 < cast.z < Z0 + Z_FLOURISH < kitty.Z_BELOW_BACKGROUND
        assert placed(sent, moving)[0]["z"] == show._casts[0].z
        assert commands(sent, shot) == [], "the one-shot enters at 0.5 s"
        assert _ticking(app), "glass ticks while it plays"

        for t in (0.25, 0.5, 0.65, 0.85, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5):
            await at(app, clock, show, t)
            assert show.elapsed == pytest.approx(min(t, 2.0))
        sent = output.since(mark)
        assert commands(sent, shot) == ["place", "frame 2", "frame 3", "frame 4"], "stepped from its entrance"
        assert len(placed(sent, moving)) >= 4 and len(placed(sent, big)) == 1
        assert log.cues == ["half", "done"] and log.ends == [] and log.threads == {threading.main_thread().name}
        assert log.apps == [app, app], "in the app's context (rule 13)"
        assert show.holding and show.elapsed == 2.0 and show.ended is None

        quiet = output.mark()
        engine.tick()
        assert not _ticking(app), "nothing moves that glass moves: the timer pauses"
        for _ in range(5):
            await at(app, clock, show, 30 + _)
            await pilot.pause()
            engine.on_frame()
            await asyncio.sleep(0.02)
        assert output.since(quiet) == "", "a holding scene sends nothing (rule 9)"
        assert log.cues == ["half", "done"] and show.holding
        model = output.model(CELL)
        assert {moving, shot, big} <= set(model.images) and model.images[shot]["cur"] == 3, "the last frame holds"
    clean(output)


def test_the_scene_layer_sits_between_ambient_effects_and_flourishes_at_every_level() -> None:
    for level in range(MAX_LEVELS):
        base = Z0 + level * LEVEL_STEP
        lowest, highest = base + Z_SCENE, base + Z_SCENE + 99 * MAX_ACTORS + MAX_ACTORS - 1  # z 0 to 99
        assert base + Z_AMBIENT + 5 < lowest and highest < base + Z_FLOURISH < base + Z_FLOAT
        assert highest < base + LEVEL_STEP + Z_SCRIM, "under the next popup's scrim"
        assert highest < kitty.Z_BELOW_BACKGROUND, f"level {level}: under cell backgrounds (rule 4)"


@pytest.mark.parametrize("cell", [(9.0, 19.0), (10.0, 20.0), (9.5, 19.25), (7.8, 16.6)], indirect=True)
async def test_native_actors_glide_at_pixel_offsets_below_the_cell_size(output: Output,
                                                                        cell: tuple[float, float]) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.player.clock = Clock()
        cw, ch = cell
        panel = cells_of(left_of(app))
        width, height = round(panel[2] * cw), round(panel[3] * ch)
        glide = actor(8, 6, route=Route.line((6, 5), (width - 6, height - 5), 0, 2))
        creep = actor(8, 6, route=Route.line((40, 40), (43, 40), 0, 2))  # 3 px in 2 s
        mark = output.mark()
        show = engine.play_scene(maker(Scene([glide, creep], 2.0)), at=left_of(app))
        assert show is not None
        await landed(app, pilot, show)
        steps = [k / 60 for k in range(121)]
        for t in steps:
            await at(app, clock, show, t)
        sent = output.since(mark)
        fast, slow = placed(sent, show._casts[0].image_id), placed(sent, show._casts[1].image_id)
        assert 60 < len(fast) <= len(steps) and len(slow) == 4, "sent only when the rounded position changes"
        for p in fast + slow:
            assert 0 <= p.get("X", 0) < cw and 0 <= p.get("Y", 0) < ch, p
            assert "c" not in p and "r" not in p, "drawn at its own pixels"
        for a, b in zip(fast, fast[1:], strict=False):
            assert a != b, "never the same placement twice in a row"
        x0, y0 = panel[0] * cw, panel[1] * ch
        for p in fast:
            left, top = p["col"] * cw + p.get("X", 0) - p.get("x", 0), p["row"] * ch + p.get("Y", 0) - p.get("y", 0)
            t = min(steps, key=lambda s: abs(x0 + glide.route.at(s)[0] - 4 - left))
            cx, cy = glide.route.at(t)
            assert abs(left - (x0 + cx - 4)) <= 1.01 and abs(top - (y0 + cy - 3)) <= 1.51, (p, t)
        assert fast[-1]["col"] * cw + fast[-1].get("X", 0) == pytest.approx(x0 + width - 10, abs=1)
        assert show.holding
    clean(output, cell)


async def test_native_actors_are_cropped_at_the_stage_and_unplaced_outside_it(output: Output, cell: Any) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.player.clock = Clock()
        cw, ch = CELL
        right = app.query_one("#right")
        panel = cells_of(right)
        width, height = round(panel[2] * cw), round(panel[3] * ch)
        across = actor(20, 10, route=Route.line((-30, 60), (width + 30, 60), 0, 2))
        down = actor(10, 20, route=Route.line((60, -30), (60, height + 30), 0, 2))
        mark = output.mark()
        show = engine.play_scene(maker(Scene([across, down], 2.0)), at=right)
        assert show is not None
        await landed(app, pilot, show)
        first, second = (cast.image_id for cast in show._casts)
        screen = pyte.Screen(*SIZE)
        stage_px = (panel[0] * 9, panel[1] * 19, (panel[0] + panel[2]) * 9, (panel[1] + panel[3]) * 19)
        seen_crop = shown = 0
        for k in range(121):
            await at(app, clock, show, k / 60)
            model = output.model(CELL)
            assert model.errors == []
            p = model.placements.get((first, 1))
            if p is not None:
                shown += 1
                assert p["w"] != 0 or p["x"] == 0, "a crop never asks for nothing (w=0 means the whole width)"
                if p["x"] or p["w"]:
                    seen_crop += 1
            picture_ = np.asarray(composite(screen, model, 9, 19))
            ys, xs = np.nonzero(np.all(picture_ == MAGENTA, axis=2))
            if len(xs):
                assert stage_px[0] <= xs.min() and xs.max() < stage_px[2], "nothing drawn beyond the stage"
                assert stage_px[1] <= ys.min() and ys.max() < stage_px[3]
        sent = output.since(mark)
        crops = [p for p in placed(sent, first) if "w" in p]
        assert crops[0]["col"] == panel[0] and "X" not in crops[0], "cropped on the left: at the stage's first cell"
        assert crops[0]["x"] + crops[0]["w"] == 20 and crops[0]["x"] > 0
        assert any(p["x"] == 0 and p["w"] < 20 for p in crops), "cropped on the right"
        assert all(p["h"] == 10 for p in crops)
        assert commands(sent, first).count("unplace 1") == 1, "unplaced once, when it is wholly outside"
        assert commands(sent, first)[-1] == "unplace 1" and seen_crop and shown
        downs = placed(sent, second)
        assert downs[0]["row"] == panel[1] and "Y" not in downs[0] and downs[0]["y"] > 0, "cropped at the top"
        assert downs[0]["y"] + downs[0]["h"] == 20 and all(p["w"] == 10 for p in downs if "w" in p)
        assert any("h" not in p for p in downs), "whole while it is in"
        assert any(p.get("y") == 0 and p["h"] < 20 for p in downs if "h" in p), "cropped at the bottom"
        assert commands(sent, second).count("unplace 1") == 1
    clean(output)


async def test_stretched_actors_sit_on_whole_cells_inside_the_stage(output: Output, cell: Any) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.player.clock = Clock()
        panel = cells_of(left_of(app))
        sweep = actor(12, 12, frames=3, cells=(6, 3), route=Route.line((-500, -300), (2000, 900), 0, 2))
        mark = output.mark()
        show = engine.play_scene(maker(Scene([sweep], 2.0)), at=left_of(app))
        assert show is not None
        await landed(app, pilot, show)
        for k in range(61):
            await at(app, clock, show, k / 30)
        found = placed(output.since(mark), show._casts[0].image_id)
        assert found[0]["col"] == panel[0] and found[0]["row"] == panel[1], "kept inside: its first corner"
        assert found[-1]["col"] == panel[0] + panel[2] - 6 and found[-1]["row"] == panel[1] + panel[3] - 3
        for p in found:
            assert (p["c"], p["r"]) == (6, 3) and "X" not in p and "Y" not in p and "w" not in p, p
            assert panel[0] <= p["col"] <= panel[0] + panel[2] - 6 and panel[1] <= p["row"] <= panel[1] + panel[3] - 3
        assert len(found) == len({(p["col"], p["row"]) for p in found}), "only when its cell changes"
    clean(output)


async def test_actors_enter_and_leave_on_time_and_their_images_go_with_them(output: Output, cell: Any) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.player.clock = Clock()
        brief = actor(frames=2, enter=0.5, leave=1.0)
        stays = actor(enter=1.5)
        mark = output.mark()
        show = engine.play_scene(maker(Scene([brief, stays], 2.0)), at=left_of(app))
        assert show is not None
        await landed(app, pilot, show)
        first, second = (cast.image_id for cast in show._casts)
        timeline = {}
        for t in (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            await at(app, clock, show, t)
            model = output.model(CELL)
            timeline[t] = ((first, 1) in model.placements, (second, 1) in model.placements)
        assert timeline == {0.25: (False, False), 0.5: (True, False), 0.75: (True, False), 1.0: (False, False),
                            1.25: (False, False), 1.5: (False, True), 2.0: (False, True)}
        assert commands(output.since(mark), first) == ["play", "place", "delete"], "deleted once, when it left"
        assert first not in engine.ids.live and first not in output.model(CELL).images
        assert engine.stats()["actors"] == 1 and second in engine.ids.live
    clean(output)


UPLOAD = re.compile(r"\x1b_G(a=[tf],[^;\x1b]*)")


def uploads(text: str, image_id: int) -> list[str]:
    """Each transmit (``t``) and frame (``f``) sent for an image, in order."""
    return [_keys(m.group(1))["a"] for m in UPLOAD.finditer(text) if _keys(m.group(1)).get("i") == str(image_id)]


def placements_of(model: KittyModel, image_id: int) -> set[int]:
    return {pid for iid, pid in model.placements if iid == image_id}


async def test_actors_with_the_same_frames_share_one_image_each_with_its_own_placement(output: Output,
                                                                                       cell: Any) -> None:
    """Stills and loops whose frames are the same object go up once, and each actor is a placement of
    that image; each placement goes as its actor leaves and the image with the last one, or all of it
    when the scene ends. One-shots never share: each is stepped on its own image."""
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.player.clock = Clock()
        light, flame, flash = picture(6, 6), picture(6, 6, frames=3), picture(6, 6, frames=3)
        going = [Actor(light, 6, 6, Route.still(10 + 9 * k, 20), enter=0.1 * k, leave=1.0 + 0.25 * k)
                 for k in range(5)]
        staying = [Actor(light, 6, 6, Route.still(10 + 9 * k, 40), gaps=(500,)) for k in range(3)]
        flames = [Actor(flame, 6, 6, Route.line((4, 60 + 8 * k), (120, 60 + 8 * k), 0, 2), gaps=(80,),
                        leave=2.2 + 0.1 * k) for k in range(4)]
        flashes = [Actor(flash, 6, 6, Route.still(10 + 30 * k, 100), loop=False, gaps=(100,), enter=0.5 * k)
                   for k in range(2)]
        live = set(engine.ids.live)
        mark = output.mark()
        show = engine.play_scene(maker(Scene(going + staying + flames + flashes, 3.0)), at=left_of(app))
        assert show is not None
        await landed(app, pilot, show)
        casts = show._casts
        assert casts is not None and len(casts) == 14
        light_id, flame_id = casts[0].image_id, casts[8].image_id
        flash_ids = [cast.image_id for cast in casts[12:]]
        assert {cast.image_id for cast in casts[:8]} == {light_id} and {c.image_id for c in casts[8:12]} == {flame_id}
        assert len({light_id, flame_id, *flash_ids}) == 4, "the one-shots have an image each"
        assert [cast.placement_id for cast in casts[:8]] == list(range(1, 9))
        assert [cast.placement_id for cast in casts[8:12]] == [1, 2, 3, 4]
        assert engine.ids.live - live == {light_id, flame_id, *flash_ids}
        assert engine.stats()["scene_images"] == 4 and engine.stats()["actors"] == 14
        sent = output.since(mark)
        assert uploads(sent, light_id) == ["t"], "one upload for eight actors"
        assert uploads(sent, flame_id) == ["t", "f", "f"] and commands(sent, flame_id).count("play") == 1
        assert all(uploads(sent, i) == ["t", "f", "f"] for i in flash_ids)

        await at(app, clock, show, 0.5)
        model = output.model(CELL)
        assert placements_of(model, light_id) == set(range(1, 9)), "every actor on stage is a placement"
        assert placements_of(model, flame_id) == {1, 2, 3, 4} and model.images[flame_id]["state"] == 3
        await at(app, clock, show, 1.1)
        assert placements_of(output.model(CELL), light_id) == set(range(2, 9)), "the first one left"
        await at(app, clock, show, 2.05)
        model = output.model(CELL)
        assert placements_of(model, light_id) == {6, 7, 8} and light_id in model.images
        for t in (2.25, 2.35, 2.45, 2.55):
            await at(app, clock, show, t)
        model = output.model(CELL)
        sent = output.since(mark)
        assert [c for c in commands(sent, light_id) if c != "place"] == [f"unplace {k}" for k in range(1, 6)]
        assert [c for c in commands(sent, flame_id) if c.startswith(("unplace", "delete"))] == \
            ["unplace 1", "unplace 2", "unplace 3", "delete"], "the last one to leave takes the image"
        assert flame_id not in model.images and flame_id not in engine.ids.live
        for flash_id in flash_ids:
            assert [c for c in commands(sent, flash_id) if c.startswith("frame")][-1] == "frame 3", "each stepped"
        await at(app, clock, show, 3.0)
        assert show.holding and engine.stats()["scene_images"] == 3

        show.stop()
        await frames(app, pilot)
        model = output.model(CELL)
        assert commands(output.since(mark), light_id)[-1] == "delete", "the scene's end: the image, all of it"
        assert not placements_of(model, light_id) and light_id not in model.images
        assert not ({light_id, *flash_ids} & set(model.images))
        assert engine.ids.live == live and engine.stats()["scene_images"] == 0, "every id freed"
    clean(output)


async def test_cues_come_once_in_order_after_the_start(output: Output, cell: Any) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.player.clock = Clock()
        cues = [Cue(1.0, "c"), Cue(0.0, "zero"), Cue(0.5, "a"), Cue(0.5, "b"), Cue(2.0, "end")]
        heard = {}
        for start_at in (0.0, 0.5, 2.0, math.inf):
            log = Log()
            show = engine.play_scene(maker(Scene([actor()], 2.0, cues=cues)), at=left_of(app), start=start_at,
                                     on_cue=log.cue)
            assert show is not None
            await landed(app, pilot, show)
            for t in (0.0, 0.5, 0.5, 0.7, 1.0, 1.0, 3.0, 4.0):
                await at(app, clock, show, t)
            await frames(app, pilot)
            heard[start_at] = log.cues
            show.stop()
        assert heard == {0.0: ["a", "b", "c", "end"], 0.5: ["c", "end"], 2.0: [], math.inf: []}, \
            "a cue at or before the start never comes (0 included)"
    clean(output)


async def test_start_offsets_the_clock_and_infinity_shows_the_end_now(output: Output, cell: Any) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.player.clock = Clock()
        moving = actor(route=Route.line((0, 0), (100, 50), 0, 2))
        gone = actor(leave=1.0)
        late = actor(frames=4, loop=False, enter=1.0, gaps=(100,))
        scene = Scene([moving, gone, late], 2.0)
        mark = output.mark()
        show = engine.play_scene(maker(scene), at=left_of(app), start=1.0)
        assert show is not None and show.elapsed == 1.0
        await landed(app, pilot, show)
        assert [c.actor.enter for c in show._casts] == [0.0, 1.0], "an actor gone by the start isn't uploaded"
        assert show._casts[0].placed == show._player._where(show, moving, 1.0) and show.elapsed == 1.0
        await at(app, clock, show, 0.5)
        assert show.elapsed == 1.5 and commands(output.since(mark), show._casts[1].image_id)[-1] == "frame 4"
        show.stop()

        mark = output.mark()
        show = engine.play_scene(maker(scene), at=left_of(app), start=math.inf)
        assert show is not None
        await landed(app, pilot, show)
        assert show.holding and show.elapsed == 2.0 and not engine.player.animating(), "the end, at once"
        assert [c.actor.enter for c in show._casts] == [0.0, 1.0]
        sent = output.since(mark)
        assert len(placed(sent, show._casts[0].image_id)) == 1, "placed once, where it ends"
        assert show._casts[0].placed == show._player._where(show, moving, 2.0)
        assert commands(sent, show._casts[1].image_id) == ["frame 4", "place"], "the one-shot's frame at the end"
        quiet = output.mark()
        await frames(app, pilot)
        engine.tick()
        assert output.since(quiet) == "" and not _ticking(app)
    clean(output)


# -- ending -------------------------------------------------------------------------------------

ENDINGS = ["stopped", "replaced", "moved", "hidden: display", "hidden: removed", "hidden: covered",
           "hidden: popup closes", "resized", "theme", "off: deactivate", "off: suspend", "off: errors"]


@pytest.mark.parametrize("holding", [False, True], ids=["playing", "holding"])
@pytest.mark.parametrize("ending", ENDINGS)
async def test_each_way_a_scene_ends_deletes_its_images_and_frees_its_ids(output: Output, cell: Any,
                                                                          ending: str, holding: bool) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.player.clock = Clock()
        if ending == "hidden: popup closes":
            app.push_screen(Popup())
            await idle(app, pilot)
            anchor = app.screen.query_one("#pop")
        else:
            anchor = left_of(app)
        log = Log()
        scene = Scene([actor(frames=3), actor(frames=3, loop=False), actor(16, 8, cells=(3, 2))], 1.0,
                      cues=[Cue(0.5, "half")])
        show = engine.play_scene(maker(scene), at=anchor, on_cue=log.cue, on_end=log.end)
        assert show is not None
        await landed(app, pilot, show)
        await at(app, clock, show, 2.0 if holding else 0.25)
        assert show.holding == holding
        ids = {cast.image_id for cast in show._casts}
        assert ids <= set(output.model(CELL).images) and ids <= engine.ids.live
        reason = ending.split(":")[0]
        if ending == "stopped":
            show.stop()
        elif ending == "replaced":
            assert engine.play_scene(maker(scene), at=anchor) is not None
        elif ending == "moved":
            anchor.styles.width = 20
        elif ending == "hidden: display":
            anchor.display = False
        elif ending == "hidden: removed":
            await anchor.remove()
        elif ending == "hidden: covered":
            app.push_screen(Cover())
        elif ending == "hidden: popup closes":
            await pilot.press("escape")
        elif ending == "resized":
            await pilot.resize_terminal(80, 24)
        elif ending == "theme":
            app.theme = "nord"
        elif ending == "off: deactivate":
            engine.deactivate()
        elif ending == "off: suspend":
            app.app_suspend_signal.publish(app)
        else:
            for _ in range(6):
                engine._failed(RuntimeError("a broken hook"))
            assert not engine.active
        await frames(app, pilot)
        await idle(app, pilot)
        assert log.ends == [reason], "on_end once, with the reason"
        assert show.ended == reason and not show.holding
        model = output.model(CELL)
        assert not ids & set(model.images) and not ids & engine.ids.live, "every image deleted, every id freed"
        assert log.cues == ([] if not holding else ["half"])
        expected = 1 if ending == "replaced" else 0
        assert engine.stats()["scenes"] == expected
        show.stop()
        await frames(app, pilot)
        assert log.ends == [reason], "and never again"
        if ending == "off: suspend":
            app.app_resume_signal.publish(app)
            await idle(app, pilot)
            assert engine.active and engine.stats()["scenes"] == 0, "nothing comes back"
    clean(output)


async def test_a_page_switch_while_holding_ends_the_scene(output: Output, cell: Any) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.player.clock = Clock()
        log = Log()
        show = engine.play_scene(maker(Scene([actor(frames=3)], 0.5)), at=app.query_one("#page1"), on_end=log.end)
        assert show is not None
        await landed(app, pilot, show)
        await at(app, clock, show, 1.0)
        engine.tick()
        assert show.holding and not _ticking(app), "holding: no ticks, only Textual's frames"
        app.query_one(ContentSwitcher).current = "page2"
        await frames(app, pilot)
        assert log.ends == ["hidden"] and engine.stats()["scenes"] == 0
        assert show._casts[0].image_id not in output.model(CELL).images
    clean(output)


async def test_a_scene_plays_at_its_anchors_level(output: Output, cell: Any) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        below = engine.play_scene(maker(Scene([actor()], 1.0)), at=left_of(app))
        app.push_screen(Popup())
        await idle(app, pilot)
        above = engine.play_scene(maker(Scene([actor()], 1.0)), at=app.screen.query_one("#pop"))
        assert below is not None and above is not None
        await landed(app, pilot, below)
        await landed(app, pilot, above)
        scrim = engine.effects.scrims[1].z
        assert below._casts[0].z == Z0 + Z_SCENE < scrim, "below the popup: under its scrim"
        assert scrim < engine.layers[(1, "main")].z < above._casts[0].z == Z0 + LEVEL_STEP + Z_SCENE, \
            "on the popup: over its scrim and its glass"
        await frames(app, pilot)
        assert below.ended is None and above.ended is None, "a popup over a scene leaves it playing"
        await pilot.press("escape")
        await frames(app, pilot)
        assert above.ended == "hidden" and below.ended is None
    clean(output)


async def test_play_scene_does_nothing_when_it_cannot_play(output: Output, cell: Any) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        engine = app.glass
        made: list[Stage] = []
        make = maker(Scene([actor()], 1.0), made)
        assert engine.play_scene(make, at=left_of(app)) is None, "glass is off"
        assert engine._player is None and output.chunks == []

        await start(app, pilot)
        mark = output.mark()
        assert engine.play_scene(make, at=app.query_one("#hidden")) is None, "no area"
        assert engine.play_scene(make, at=app.query_one("#page2")) is None, "a page that isn't current"
        assert engine.play_scene(make, at=Static("never mounted")) is None, "not on a screen"
        assert engine.play_scene(make, at=Region(200, 50, 10, 4)) is None, "outside the window"
        app.push_screen(Cover())
        await idle(app, pilot)
        mark = output.mark()
        assert engine.play_scene(make, at=left_of(app)) is None, "its screen is hidden"
        await settle(app, pilot)
        assert output.since(mark) == "" and not made and engine.player.fired == 0

        cover = app.screen.query_one("#cover")
        first = engine.play_scene(make, at=cover)
        second = engine.play_scene(make, at=Region(2, 2, 10, 3))
        assert first is not None and second is not None and MAX_SCENES == 2
        assert engine.play_scene(make) is None, f"at most {MAX_SCENES} at once, while they are made too"
        await landed(app, pilot, first)
        await landed(app, pilot, second)
        assert engine.play_scene(make) is None, "and while they show"
        log = Log()
        third = engine.play_scene(make, at=Region(2, 2, 10, 3), on_end=log.end)
        assert third is not None and second.ended == "replaced", "the same anchor: replaced, within the limit"
        assert engine.play_scene(make, at=cover) is not None and first.ended == "replaced"
        await idle(app, pilot)
        assert engine.stats()["scenes"] == 2 and log.ends == []
    clean(output)


@pytest.mark.parametrize("broken", ["raises", "not a scene", "too long", "bad frames", "route raises"])
async def test_a_broken_scene_counts_as_an_error_and_leaves_nothing(output: Output, cell: Any, broken: str) -> None:
    class Wrong(Route):
        def at(self, t: float) -> tuple[float, float]:
            if t > 0.5:
                raise RuntimeError("the route broke")
            return super().at(t)

    def make(stage: Stage) -> Any:
        if broken == "raises":
            raise RuntimeError("the maker broke")
        if broken == "not a scene":
            return [actor()]
        if broken == "too long":
            return Scene([actor()], 400)
        if broken == "bad frames":
            return Scene([Actor([b"\0" * 12], 6, 6, Route.still(1, 1))], 1)
        return Scene([actor(route=Wrong(((0.0, 10.0, 10.0), (2.0, 30.0, 10.0))))], 2)

    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.player.clock = Clock()
        live = set(engine.ids.live)
        log = Log()
        mark = output.mark()
        show = engine.play_scene(make, at=left_of(app), on_end=log.end)
        assert show is not None, "it is only known to fail once it is made"
        await idle(app, pilot)
        if broken == "route raises":
            await landed(app, pilot, show)
            await at(app, clock, show, 1.0)
        await frames(app, pilot)
        assert log.ends == ["failed"] and show.ended == "failed"
        assert engine.errors == 1 and engine.active
        why = {"raises": "the maker broke", "not a scene": "not a Scene", "too long": "at most 300",
               "bad frames": "RGBA bytes", "route raises": "the route broke"}[broken]
        assert why in (engine.last_error or "")
        assert engine.stats()["scenes"] == 0 and engine.ids.live == live
        if broken != "route raises":
            assert output.since(mark) == ""
        assert not output.model(CELL).images.keys() - {layer.image_id for layer in engine.layers.values()}
    clean(output)


# -- motion levels ------------------------------------------------------------------------------


async def test_at_minimal_motion_a_scene_is_a_still_of_its_end(output: Output, cell: Any) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        engine.player.clock = Clock()
        engine.set_motion("minimal")
        seen: list[Stage] = []
        log = Log()
        moving = actor(frames=3, route=Route.line((0, 0), (100, 50), 0, 2))
        shot = actor(frames=4, loop=False, enter=1.85, gaps=(100,))
        mark = output.mark()
        show = engine.play_scene(maker(Scene([moving, shot], 2.0, cues=[Cue(1.0, "half")]), seen), at=left_of(app),
                                 on_cue=log.cue)
        assert show is not None
        await landed(app, pilot, show)
        assert seen[0].still and show.holding and show.elapsed == 2.0
        loop_id, shot_id = (cast.image_id for cast in show._casts)
        model = output.model(CELL)
        assert len(model.images[loop_id]["frames"]) == 3 and model.images[loop_id]["state"] == 1, "no loops run"
        assert model.images[shot_id]["cur"] == 1, "the one-shot at its frame for the end (0.15 s in)"
        assert show._casts[0].placed == show._player._where(show, moving, 2.0)
        await frames(app, pilot)
        assert log.cues == ["half"], "the cues after the start come at once, as it starts at its end"
        sent = output.since(mark)
        assert "s=3" not in sent
        engine.set_motion("subtle")
        engine.tick()
        assert commands(output.since(mark), loop_id)[-1] == "play", "back up: the loops play"
        assert len(placed(output.since(mark), loop_id)) == 1 and show.holding, "and it doesn't replay"
    clean(output)


async def test_minimal_motion_mid_show_jumps_to_the_end_and_back_up_the_loops_play_again(output: Output,
                                                                                       cell: Any) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.player.clock = Clock()
        log = Log()
        moving = actor(frames=3, route=Route.line((0, 0), (100, 50), 0, 2))
        stretched = actor(8, 8, frames=2, cells=(2, 1), route=Route.line((0, 0), (200, 100), 0, 2))
        show = engine.play_scene(maker(Scene([moving, stretched], 2.0, cues=[Cue(0.5, "a"), Cue(1.5, "b")])),
                                 at=left_of(app), on_cue=log.cue)
        assert show is not None
        await landed(app, pilot, show)
        await at(app, clock, show, 0.75)
        assert log.cues == ["a"] and not show.holding
        mark = output.mark()
        engine.set_motion("minimal")
        engine.tick()
        await asyncio.sleep(0)
        sent = output.since(mark)
        for cast in show._casts:
            assert "stop" in commands(sent, cast.image_id) and "s=1,c=1" in sent, "loops stop on frame 1"
        assert show.holding and show.elapsed == 2.0 and log.cues == ["a", "b"], "at its end, the cues heard"
        assert show._casts[0].placed == show._player._where(show, moving, 2.0)
        quiet = output.mark()
        await at(app, clock, show, 1.0)
        await frames(app, pilot)
        assert output.since(quiet) == ""
        engine.set_motion("subtle")
        engine.tick()
        sent = output.since(quiet)
        assert [commands(sent, cast.image_id) for cast in show._casts] == [["play"], ["play"]], "no replay"
        assert "a=t" not in sent and show.holding and log.cues == ["a", "b"]
    clean(output)


async def test_a_scene_made_at_one_level_lands_at_the_next(output: Output, cell: Any, gate: threading.Event) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        engine.player.clock = Clock()

        def make(stage: Stage) -> Scene:
            gate.wait(10)
            return Scene([actor(frames=3, route=Route.line((0, 0), (90, 40), 0, 2))], 2.0)

        show = engine.play_scene(make, at=left_of(app))
        assert show is not None and not show.stage.still
        engine.set_motion("minimal")
        gate.set()
        await landed(app, pilot, show)
        model = output.model(CELL)
        assert show.holding and model.images[show._casts[0].image_id]["state"] == 1, "a still after all"
    clean(output)


# -- staying bounded -------------------------------------------------------------------------------


async def test_a_long_show_and_many_replays_stay_bounded(output: Output, cell: Any) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.player.clock = Clock()
        base_ids, base_images = len(engine.ids.live), len(output.model(CELL).images)
        cast = [actor(frames=2, enter=k * 0.25, leave=k * 0.25 + 2, loop=k % 2 == 0,
                      route=Route.line((-10, 30), (300, 30), k * 0.25, k * 0.25 + 2)) for k in range(200)]
        show = engine.play_scene(maker(Scene(cast, 60.0)), at=left_of(app))
        assert show is not None
        await landed(app, pilot, show)
        peak = 0
        for k in range(0, 601, 3):
            await at(app, clock, show, k / 10)
            live = len(engine.ids.live) - base_ids
            peak = max(peak, live)
            assert live == engine.stats()["actors"] == sum(1 for a in cast if k / 10 < a.leave), k
        assert peak == 200 and show.holding and engine.stats()["actors"] == 0, "each image goes when its actor leaves"
        assert len(output.model(CELL).images) == base_images
        for n in range(30):  # replays, at the end and from the start
            show = engine.play_scene(maker(Scene(cast[:20], 60.0)), at=left_of(app), start=math.inf if n % 2 else 0)
            assert show is not None
            await landed(app, pilot, show)
            await at(app, clock, show, 1.0)
            assert len(engine.ids.live) - base_ids <= 20
        show.stop()
        await idle(app, pilot)
        assert len(engine.ids.live) == base_ids and len(output.model(CELL).images) == base_images
        assert engine.stats()["scenes"] == 0 and engine.errors == 0
    clean(output)


# -- what an app that never plays a scene sees -------------------------------------------------------


async def test_an_app_that_never_plays_a_scene_has_no_player(output: Output) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        engine.set_motion("minimal")
        app.theme = "nord"
        await pilot.resize_terminal(80, 24)
        await idle(app, pilot)
        engine.deactivate()
        engine.activate(force=True)
        await idle(app, pilot)
        assert engine._player is None
        assert engine.stats()["scenes"] == 0 and engine.stats()["actors"] == 0
    clean(output)


def test_importing_glass_leaves_the_player_unloaded() -> None:
    code = "import sys, textual_glass; print('textual_glass.player' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True)
    assert result.stdout.strip() == "False"


# -- the kitty model ------------------------------------------------------------------------------


def test_the_kitty_model_knows_offsets_and_source_rectangles() -> None:
    def run(model: KittyModel, data: str) -> None:
        for match in re.finditer(r"(?:\x1b\[(\d+);(\d+)H)?\x1b_G([^\x1b]*)\x1b\\", data):
            col, row = match.group(2), match.group(1)
            model.apc(match.group(3).encode(), (int(col) - 1, int(row) - 1) if col else (0, 0))

    frame = np.zeros((10, 20, 4), np.uint8)
    frame[:, :10] = (255, 0, 0, 255)
    frame[:, 10:] = (0, 0, 255, 255)
    upload = kitty.transmit_raw(7, 20, 10, frame.tobytes())
    model = KittyModel(cell=(9, 19))
    run(model, upload + kitty.place_px(7, 2, 1, 5, x_off=8, y_off=18, crop=(10, 0, 10, 10)))
    p = model.placements[(7, 1)]
    assert model.errors == [] and (p["x"], p["w"], p["X"], p["Y"], p["c"]) == (10, 10, 8, 18, 0)
    picture_ = np.asarray(composite(pyte.Screen(10, 4), model, 9, 19))
    ys, xs = np.nonzero(np.all(picture_ == (0, 0, 255), axis=2))
    assert (xs.min(), xs.max(), ys.min(), ys.max()) == (26, 35, 37, 46), "the right half, at its own pixels"
    assert not np.all(picture_ == (255, 0, 0), axis=2).any(), "the cropped half isn't drawn"

    for bad, why in ((kitty.place_px(7, 0, 0, 5, x_off=9), "cell size"), (kitty.place_px(7, 0, 0, 5, y_off=19), "cell"),
                     (kitty.place_px(7, 0, 0, 5, crop=(15, 0, 10, 10)), "runs off")):
        model = KittyModel(cell=(9, 19))
        run(model, upload + bad)
        assert len(model.errors) == 1 and why in model.errors[0], model.errors
    model = KittyModel()
    run(model, upload + kitty.place_px(7, 0, 0, 5, x_off=9))
    assert model.errors == [], "without the cell size, offsets aren't checked"


# -- the demo -------------------------------------------------------------------------------------------

DEMO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "scenes", "app.py")


def _demo() -> Any:
    spec = importlib.util.spec_from_file_location("scene_demo", DEMO)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def holding(app: Any, timeout: float = 20.0) -> Showing:
    """Wait until the demo's scene holds (holding stays until something ends it)."""
    deadline = time.monotonic() + timeout
    while True:
        show = app.showing
        if show is not None and show.holding:
            await asyncio.sleep(0.01)  # the callbacks glass queued as it got there (the last cue)
            return show
        if time.monotonic() > deadline:
            raise AssertionError(f"the demo's scene never held: {show!r}")
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("size", [(70, 22), (90, 28), (140, 45)])
async def test_the_demo_plays_to_its_hold(output: Output, cell: Any, size: tuple[int, int]) -> None:
    demo = _demo()
    app = demo.GlassDemo()
    async with app.run_test(size=size) as pilot:
        await start(app, pilot)
        engine = app.glass
        base = time.monotonic()
        engine.player.clock = lambda: base + 4 * (time.monotonic() - base)  # four times as fast
        await pilot.press("r")
        show = await holding(app)
        assert show.scene is not None and len(show.scene.cues) == 7
        assert app.ends in ([], ["replaced"]), "the demo also plays once it mounts, if glass is on by then"
        assert engine.errors == 0 and app.heard == [c.data for c in show.scene.cues], "every cue, in order"
        assert app.caption() == demo.FINAL and output.model(CELL).errors == []
        stage = cells_of(app.query_one("#stage"))
        for cast in show._casts:
            if cast.placed is not None and cast.actor.cells is None:
                assert stage[0] <= cast.placed[0] < stage[0] + stage[2]
        quiet = output.mark()
        await frames(app, pilot)
        engine.tick()
        assert output.since(quiet) == "", "it holds in silence"

        await pilot.press("l")  # a light theme: the scene ends, and the demo puts its end back
        await frames(app, pilot)
        again = await holding(app)
        assert again is not show and app.ends[-1] == "theme" and app.caption() == demo.FINAL
        assert again.elapsed == show.scene.duration and app.heard == [], "its end, at once: no cues"
        await pilot.press("f9", "f9")  # full, minimal
        assert engine.motion == "minimal"
        await pilot.press("r")
        still = await holding(app)
        assert still.stage.still and app.caption() == demo.FINAL
        assert engine.errors == 0
    clean(output)


def test_the_demos_pictures_are_transparent_at_their_border() -> None:
    demo = _demo()
    for dark in (True, False):
        stage = Stage(630, 380, 70, 20, CELL, {role: (90, 200, 255) for role in ROLES}, dark=dark,
                      still=False, seed=0)
        scene = demo.make_show(stage)
        for n, a in enumerate(scene.actors):
            for k, f in enumerate(a.frames):
                alpha = np.frombuffer(f, np.uint8).reshape(a.height, a.width, 4)[..., 3]
                ring = np.concatenate([alpha[0], alpha[-1], alpha[:, 0], alpha[:, -1]])
                assert ring.max() == 0, f"actor {n} frame {k}: light at its border (rule 17)"
