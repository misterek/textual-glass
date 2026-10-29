"""The scene player: plays a :class:`~textual_glass.scenes.Scene` over a widget
(``app.glass.play_scene``). The engine owns one, made the first time an app
plays a scene, so an app that never does pays nothing.

Each actor is one placement of a terminal image, uploaded with all the others
when the scene starts (encoded in the worker, like the maker's drawing). Stills
and loops whose frames are the same object share one image, each actor with a
placement id of its own; every one-shot has its own image. Every tick glass
works out where each actor on stage is and re-places it only when that changed
(an actor whose route has ended is not worked out again):

* a native actor (``cells=None``) is drawn at its own pixels: at a cell plus
  ``X``/``Y`` pixel offsets, each less than the cell's size, and cropped to the
  stage with a source rectangle; it is unplaced while it is wholly outside;
* a stretched actor (``cells=(c, r)``) covers whole cells, kept inside the stage.

One-shot actors are stepped with ``a=a,c=N`` from their entrance and hold their
last frame; loops go up whole and are then started, and the terminal plays them
(rule 2). An actor that leaves deletes its placement, and the last one of an
image deletes the image. After its duration a scene holds: what is on stage
stays, only the loops move, and glass sends nothing (rule 9). A scene goes
(every image deleted, every id freed, ``on_end(reason)`` called once) when the
app stops or replaces it, when its anchor moves or is hidden, and on a resize, a
new theme or glass turning off. At ``minimal`` motion a scene is a still: it
starts at its end, and its loops stay on frame 1.
"""

from __future__ import annotations

import bisect
import dataclasses
import itertools
import math
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from textual.geometry import Region

from . import _textual_hooks as hooks
from . import kitty
from .engine import LEVEL_STEP, MAX_LEVELS, Z0, Z_SCENE
from .scene import visible_screens
from .scenes import MAX_ACTORS, ROLES, Actor, Scene, SceneMaker, Stage, check, gaps, shares

if TYPE_CHECKING:
    from textual.screen import Screen
    from textual.widget import Widget

    from .engine import GlassEngine

#: At most this many scenes at once, playing, holding or being made: ``play_scene`` refuses more.
MAX_SCENES = 2

#: Why a scene ended, as ``on_end(reason)`` hears it.
REASONS = ("stopped", "replaced", "moved", "hidden", "resized", "theme", "off", "failed")

Cells = tuple[int, int, int, int]  # x, y, width, height in screen cells
Placed = tuple[Any, ...]


@dataclass
class Picture:
    """One terminal image and the actors that show it: one, or every still or loop
    whose frames are the same object."""

    image_id: int
    #: The actor whose frames go up (until they are in the terminal).
    source: Actor | None
    #: An animation the terminal plays (a loop of more than one frame).
    looping: bool
    casts: list[Cast] = dataclasses.field(default_factory=list)
    gone: bool = False  # deleted, its id freed


@dataclass
class Cast:
    """One actor in the terminal: its placement of a picture and what was last sent for it."""

    actor: Actor
    picture: Picture
    placement_id: int
    z: int
    ends: list[float]  # when each frame ends, in seconds after its entrance (one-shots)
    placed: Placed | None = None  # the placement last sent; None while it isn't placed
    frame: int = 1  # the frame shown (one-shots), 1-based
    settled: bool = False  # its route has ended and it is where it stays: not worked out again
    gone: bool = False  # it left for good, or the scene ended: its placement is deleted

    @property
    def image_id(self) -> int:
        return self.picture.image_id


class Showing:
    """A scene over its anchor: what ``app.glass.play_scene`` returns.

    ``stop()`` ends it. ``elapsed`` is how far into the scene it is (seconds),
    ``holding`` is True once it has reached its end and stays as it is, and
    ``ended`` says why it went (one of ``REASONS``), or None while it shows.
    """

    def __init__(self, player: Player, anchor: Any, screen: Any, level: int, cells: Cells, stage: Stage,
                 start: float, on_cue: Callable[[Hashable], None] | None,
                 on_end: Callable[[str], None] | None) -> None:
        self._player = player
        self._anchor = anchor  # the widget, the Region or None the app named
        self._screen = screen
        self._level = level
        self._cells = cells
        self._on_cue = on_cue
        self._on_end = on_end
        self._start = start
        self._at_end = stage.still  # minimal motion: shown at its end
        self._t0: float | None = None  # when the upload was sent: the clock starts
        self._settled = False  # its end is on screen: nothing more is sent for it
        self._casts: list[Cast] | None = None  # None until the maker's scene is in
        self._pictures: list[Picture] = []  # the scene's images, each shown by one or more casts
        self._loops: bool | None = None  # whether its loops play in the terminal (None: not uploaded)
        self._cues: list[Any] = []
        self._next_cue = 0
        #: What the scene was made for.
        self.stage = stage
        #: The maker's scene, once it is made. Once its pictures are in the terminal, its
        #: actors keep everything but their frames (a summary may hold all day).
        self.scene: Scene | None = None
        #: Why it ended (one of ``REASONS``), or None while it shows.
        self.ended: str | None = None

    def stop(self) -> None:
        """End it now: its images go, and ``on_end("stopped")`` follows."""
        self._player.end(self, "stopped")
        self._player.engine.writer.flush()

    @property
    def elapsed(self) -> float:
        """Seconds into the scene, from 0 to its duration. Until it is uploaded, where it will start."""
        return self._time(self._player.clock())

    @property
    def holding(self) -> bool:
        """It has reached its end and holds there (only its loops move, played by the terminal)."""
        return self._settled and self.ended is None

    def _time(self, now: float) -> float:
        scene = self.scene
        if scene is None:
            return self._start if math.isfinite(self._start) else 0.0
        if self._at_end:
            return scene.duration
        if self._t0 is None:
            return min(self._start, scene.duration)
        return min(scene.duration, self._start + (now - self._t0))

    def _same_anchor(self, anchor: Any, screen: Any) -> bool:
        if isinstance(anchor, tuple) or anchor is None:
            return self._screen is screen and self._anchor == anchor
        return self._anchor is anchor

    def __repr__(self) -> str:
        state = self.ended or ("holding" if self._settled else "playing" if self._t0 is not None else "making")
        return f"<Showing {self._cells} {state}>"


class Player:
    """Owned by the engine; uses its writer, ids, palette, motion level and worker."""

    def __init__(self, engine: GlassEngine) -> None:
        self.engine = engine
        self.showings: list[Showing] = []
        self.fired = 0  # scenes asked for so far: each one's seed
        #: The scenes' clock (seconds); a test may replace it.
        self.clock: Callable[[], float] = time.monotonic

    # -- what the engine asks ---------------------------------------------------------------

    @property
    def actors(self) -> int:
        """Actors in the terminal (or on their way there), over every scene."""
        return sum(1 for show in self.showings for cast in show._casts or () if not cast.gone)

    @property
    def images(self) -> int:
        """Images the actors show (actors that share their frames share one)."""
        return sum(1 for show in self.showings for picture in show._pictures if not picture.gone)

    def animating(self) -> bool:
        """Something still moves that glass moves (the ticks must go on)."""
        return any(show._t0 is not None and not show._settled for show in self.showings)

    def update(self, screens: list[Screen[Any]]) -> None:
        """Every frame and tick: end the scenes whose anchor moved or went, and move
        the rest on. A scene that raises ends as "failed" and counts as an error."""
        if not self.showings:
            return
        now = self.clock()
        for show in list(self.showings):
            if show.ended is not None:
                continue
            try:
                lost = self._lost(show, screens)
                if lost is not None:
                    self.end(show, lost)
                elif show._t0 is not None and not show._settled:
                    self._advance(show, now)
            except Exception as error:  # noqa: BLE001 - the app's scene (a route that raises, say)
                self.end(show, "failed")
                self.engine._failed(error)

    def end_all(self, reason: str) -> None:
        for show in list(self.showings):
            self.end(show, reason)

    def reset(self) -> None:
        """Glass turned off: the engine has deleted every image and forgotten every id."""
        shows, self.showings = self.showings, []
        for show in shows:
            for cast in show._casts or ():
                cast.gone = True
            for picture in show._pictures:
                picture.gone = True
            self._finish(show, "off")

    def motion_changed(self) -> None:
        """``minimal`` jumps every scene to its end and stops its loops on frame 1;
        back up, the loops play again (the scene doesn't replay)."""
        still = self.engine.motion == "minimal"
        for show in self.showings:
            if still and not show._at_end:
                show._at_end = True
                show._settled = False  # the next tick puts it at its end
            self._set_loops(show, not still)

    # -- playing ----------------------------------------------------------------------------------

    def play(self, make: SceneMaker, at: Widget | Region | None, start: float,
             on_cue: Callable[[Hashable], None] | None, on_end: Callable[[str], None] | None) -> Showing | None:
        engine = self.engine
        palette = engine.palette
        screens = visible_screens(engine.app)[:MAX_LEVELS]
        if not engine.active or palette is None or not screens:
            return None
        found = self._anchor_cells(at, screens)
        if found is None:
            return None
        screen, cells = found
        replaced = [show for show in self.showings if show._same_anchor(at, screen)]
        for show in replaced:
            self.end(show, "replaced")
        if replaced:
            engine.writer.flush()
        if len(self.showings) >= MAX_SCENES:
            return None
        cell = engine.cell_pixels()
        stage = Stage(width=max(1, round(cells[2] * cell[0])), height=max(1, round(cells[3] * cell[1])),
                      cols=cells[2], rows=cells[3], cell=cell,
                      colors={role: getattr(palette, role) for role in ROLES}, dark=palette.dark,
                      still=engine.motion == "minimal", seed=self.fired, space=palette.space,
                      nebula=palette.nebula)
        level = next(i for i, s in enumerate(screens) if s is screen)
        start = float(start)
        show = Showing(self, at, screen, level, cells, stage, start if start > 0 else 0.0, on_cue, on_end)
        self.showings.append(show)
        self.fired += 1

        def work() -> tuple[bool, Any]:
            try:
                scene = make(stage)
                check(scene, stage)
                return True, scene
            except Exception as error:  # noqa: BLE001 - counted in _made, which ends it as "failed"
                return False, error

        engine.background_job(work, lambda result: self._made(show, result))
        return show

    def _anchor_cells(self, at: Any, screens: list[Screen[Any]]) -> tuple[Screen[Any], Cells] | None:
        """The screen the anchor is on and its cells there, clipped to the window:
        None if it isn't on a visible screen or shows no cells."""
        window = Region(0, 0, *self.engine.app.size)
        if at is None:
            return screens[-1], (0, 0, window.width, window.height)
        if isinstance(at, tuple):  # a Region, on the top screen
            screen, region = screens[-1], Region(*at)
        else:
            try:
                screen = at.screen
            except Exception:  # noqa: BLE001 - not mounted
                return None
            if not any(s is screen for s in screens) or not _attached(at):
                return None
            region = hooks.shown_region(at)
        region = region.intersection(window)
        if not region.area:
            return None
        return screen, (region.x, region.y, region.width, region.height)

    def _lost(self, show: Showing, screens: list[Screen[Any]]) -> str | None:
        """Why a scene must go now ("hidden" or "moved"), or None."""
        level = next((i for i, s in enumerate(screens) if s is show._screen), None)
        if level is None:
            return "hidden"
        if level != show._level:
            return "moved"
        anchor = show._anchor
        if anchor is None or isinstance(anchor, tuple):
            return None
        found = self._anchor_cells(anchor, screens)
        if found is None:
            return "hidden"
        return "moved" if found[1] != show._cells else None

    def _made(self, show: Showing, result: tuple[bool, Any]) -> None:
        """The maker's scene, on the app thread: give each image an id and each actor that
        can still show a placement of one and a z, then encode the uploads in the worker."""
        if show.ended is not None:
            return
        made, scene = result
        if not made:
            self.end(show, "failed")
            self.engine._failed(scene)  # after: it may turn glass off, and this scene failed
            return
        show.scene = scene
        show._cues = sorted(scene.cues, key=lambda cue: cue.t)
        show._next_cue = bisect.bisect_right(show._cues, show._start, key=lambda cue: cue.t)
        first = show._time(self.clock())
        engine = self.engine
        base = Z0 + show._level * LEVEL_STEP + Z_SCENE
        casts: list[Cast] = []
        shared: dict[int, Picture] = {}  # by the frames' identity: stills and loops only
        for index, actor in enumerate(scene.actors):
            if actor.leave <= first or actor.enter > scene.duration:
                continue  # never on stage from here on: not worth uploading
            picture = shared.get(id(actor.frames)) if shares(actor) else None
            if picture is None:
                picture = Picture(engine.ids.new(), actor, actor.loop and len(actor.frames) > 1)
                show._pictures.append(picture)
                if shares(actor):
                    shared[id(actor.frames)] = picture
            ends = list(itertools.accumulate(g / 1000 for g in gaps(actor)))
            cast = Cast(actor, picture, len(picture.casts) + 1, base + actor.z * MAX_ACTORS + index, ends)
            picture.casts.append(cast)
            casts.append(cast)
        show._casts = casts
        pictures = list(show._pictures)
        loops = engine.motion != "minimal"

        def work() -> tuple[bool, Any]:
            try:
                return True, "".join(_upload(picture, loops) for picture in pictures)
            except Exception as error:  # noqa: BLE001
                return False, error

        engine.background_job(work, lambda result: self._encoded(show, loops, result))

    def _encoded(self, show: Showing, loops: bool, result: tuple[bool, Any]) -> None:
        """The uploads, on the app thread: send them, start the clock and place what is on stage."""
        if show.ended is not None:
            return
        encoded, payload = result
        if not encoded:
            self.end(show, "failed")
            self.engine._failed(payload)
            return
        engine = self.engine
        lost = self._lost(show, visible_screens(engine.app)[:MAX_LEVELS])
        if lost is not None:
            self.end(show, lost)
            return
        engine.writer.queue(payload)
        _let_frames_go(show)
        show._loops = loops
        show._t0 = self.clock()
        self._set_loops(show, engine.motion != "minimal")  # the motion level changed meanwhile
        self._advance(show, show._t0)
        engine.kick()

    def _advance(self, show: Showing, now: float) -> None:
        """Bring the terminal to the scene at ``now``: cues, entrances and exits,
        one-shot frames and placements. At the end it holds."""
        scene, casts = show.scene, show._casts
        assert scene is not None and casts is not None
        t = show._time(now)
        cues = show._cues
        while show._next_cue < len(cues) and cues[show._next_cue].t <= t:
            self._call(show._on_cue, cues[show._next_cue].data)
            show._next_cue += 1
        out = []
        for cast in casts:
            actor = cast.actor
            if cast.gone or t < actor.enter:
                continue
            if t >= actor.leave:  # gone for good: time only moves on
                out.append(self._leave(cast))
                continue
            if not actor.loop and len(cast.ends) > 1:
                frame = min(len(cast.ends), bisect.bisect_right(cast.ends, t - actor.enter) + 1)
                if frame != cast.frame:
                    out.append(kitty.animate(cast.image_id, c=frame))
                    cast.frame = frame
            if cast.settled:
                continue
            where = self._where(show, actor, t)
            if where != cast.placed:
                out.append(_unplace(cast) if where is None else _place(cast, where))
                cast.placed = where
            cast.settled = t >= actor.route.settles
        self.engine.writer.queue("".join(out))
        if t >= scene.duration:
            show._settled = True

    def _leave(self, cast: Cast) -> str:
        """An actor leaves for good: its placement goes, and with the last one its image."""
        cast.gone = True
        picture = cast.picture
        if all(other.gone for other in picture.casts):
            picture.gone = True
            self.engine.ids.free(picture.image_id)
            return kitty.delete(picture.image_id)
        return "" if cast.placed is None else _unplace(cast)

    def _where(self, show: Showing, actor: Actor, t: float) -> Placed | None:
        """Where an actor goes at ``t``: (col, row, X, Y, crop) for a native actor, None
        when it is wholly outside the stage; (col, row) for a stretched one."""
        stage = show.stage
        cell_w, cell_h = stage.cell
        col0, row0 = show._cells[0], show._cells[1]
        cx, cy = actor.route.at(t)
        if actor.cells is not None:
            cols, rows = actor.cells
            col = math.floor((cx - cols * cell_w / 2) / cell_w + 0.5)
            row = math.floor((cy - rows * cell_h / 2) / cell_h + 0.5)
            return col0 + min(max(col, 0), stage.cols - cols), row0 + min(max(row, 0), stage.rows - rows)
        w, h = actor.width, actor.height
        x, y = math.floor(cx - w / 2 + 0.5), math.floor(cy - h / 2 + 0.5)  # its top left, on the stage
        x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, stage.width), min(y + h, stage.height)
        if x1 <= x0 or y1 <= y0:
            return None
        crop = None if (x0, y0, x1, y1) == (x, y, x + w, y + h) else (x0 - x, y0 - y, x1 - x0, y1 - y0)
        col, x_off = _split(x0, cell_w, stage.cols)
        row, y_off = _split(y0, cell_h, stage.rows)
        return col0 + col, row0 + row, x_off, y_off, crop

    def _set_loops(self, show: Showing, play: bool) -> None:
        """Play the scene's loops in the terminal, or stop them on frame 1."""
        if show._loops is None or show._loops == play or show._casts is None:
            return
        show._loops = play
        out = [kitty.play(picture.image_id) if play else kitty.animate(picture.image_id, s=1, c=1)
               for picture in show._pictures if not picture.gone and picture.looping]
        self.engine.writer.queue("".join(out))

    # -- ending -------------------------------------------------------------------------------------

    def end(self, show: Showing, reason: str) -> None:
        """A scene goes: its images are deleted, its ids freed, and ``on_end(reason)`` follows."""
        if show.ended is not None:
            return
        if show in self.showings:
            self.showings.remove(show)
        engine = self.engine
        out = []
        for picture in show._pictures:
            if not picture.gone:
                if show._t0 is not None:  # uploaded (an upload still in the worker is dropped when it lands)
                    out.append(kitty.delete(picture.image_id))
                engine.ids.free(picture.image_id)
                picture.gone = True
        for cast in show._casts or ():
            cast.gone = True
        engine.writer.queue("".join(out))
        self._finish(show, reason)

    def _finish(self, show: Showing, reason: str) -> None:
        show.ended = reason
        show._settled = False
        self._call(show._on_end, reason)

    def _call(self, callback: Callable[[Any], None] | None, arg: Any) -> None:
        """Run the app's callback on the app thread, in the app's context, once glass is
        done with the frame or call it came from (so it may play or stop scenes). What it
        raises counts as a glass error."""
        engine = self.engine
        loop = engine._loop
        if callback is None or loop is None:
            return

        def run() -> None:
            try:
                callback(arg)
            except Exception as error:  # noqa: BLE001
                engine._failed(error)

        try:
            loop.call_soon(run, context=engine._app_context.copy())
        except RuntimeError:  # the loop has closed
            pass


def _let_frames_go(show: Showing) -> None:
    """The pictures are in the terminal: keep the actors without their frames."""
    bare: dict[int, Actor] = {}

    def light(actor: Actor) -> Actor:
        found = bare.get(id(actor))
        if found is None:
            found = bare[id(actor)] = dataclasses.replace(actor, frames=())
        return found

    assert show.scene is not None and show._casts is not None
    show.scene = dataclasses.replace(show.scene, actors=[light(actor) for actor in show.scene.actors])
    for cast in show._casts:
        cast.actor = light(cast.actor)
    for picture in show._pictures:
        picture.source = None


def _attached(widget: Any) -> bool:
    try:
        return bool(getattr(widget, "is_attached", True))
    except Exception:  # noqa: BLE001
        return False


def _split(p: int, size: float, cells: int) -> tuple[int, int]:
    """A pixel on the stage as a cell and an offset into it: the offset is less than
    the cell's size, even when that is fractional, and the cell is on the stage."""
    cell = int(p // size)
    offset = math.floor(p - cell * size + 0.5)
    if offset >= size:
        cell, offset = cell + 1, 0
    if cell >= cells:  # only when a fractional size rounds the stage's last pixel up
        cell, offset = cells - 1, min(math.ceil(size) - 1, math.floor(p - (cells - 1) * size))
    return cell, max(0, offset)


def _place(cast: Cast, where: Placed) -> str:
    if cast.actor.cells is None:
        col, row, x_off, y_off, crop = where
        return kitty.place_px(cast.image_id, col, row, cast.z, x_off, y_off, crop, cast.placement_id)
    col, row = where
    cols, rows = cast.actor.cells
    return kitty.place(cast.image_id, col, row, cols, rows, cast.z, cast.placement_id)


def _unplace(cast: Cast) -> str:
    """Take one actor off the stage: its own placement, not the others of its image."""
    return kitty.unplace(cast.image_id, cast.placement_id)


def _upload(picture: Picture, loops: bool) -> str:
    """One image's upload (in the worker): a still, a loop with its gaps (started
    after every frame is up, unless ``loops`` is False), or a one-shot's frames, stopped."""
    actor, image_id = picture.source, picture.image_id
    assert actor is not None
    frames = list(actor.frames)
    if len(frames) == 1:
        return kitty.transmit_raw(image_id, actor.width, actor.height, frames[0])
    if actor.loop:
        return kitty.animation(image_id, actor.width, actor.height, frames, gaps(actor), start=loops)
    return kitty.frames_stopped(image_id, actor.width, actor.height, frames)
