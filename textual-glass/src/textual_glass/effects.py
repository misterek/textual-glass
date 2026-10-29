"""Transitions and ambient motion: modal scrims, the terminal-played
effects of ``full`` motion (shimmer, glints, a pulsing halo), and flourishes.

Scrims are 1x1 images stretched over the window. Their frames are alpha steps
uploaded once; Python moves between them with ``a=a,c=N`` during a fade (a few
small commands), and nothing at all once the fade is done. How dark a scrim
gets is the theme's (``Palette.scrim_alpha``, the ``glass-scrim-alpha``
variable). Ambient effects are uploaded once and looped by the terminal itself.

Flourishes (``GlassEngine.flourish``) are rendered in the worker, uploaded
while stopped and stepped the same way as a fade, one ``a=a,c=N`` when each
frame's time comes; after the last frame the image is deleted.
"""

from __future__ import annotations

import bisect
import itertools
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import kitty
from .scene import visible_screens
from .sprites import glint_frames, halo_frames, shimmer_frames, solid_frames

if TYPE_CHECKING:
    from .engine import GlassEngine
    from .flourishes import Flourish, Rect, Stage
    from .palette import RGB
    from .scene import LevelScene

SCRIM_FRAMES = 6
SCRIM_TIME = 0.14


@dataclass
class Fade:
    """A 1x1 image stretched over the window whose alpha Python steps through."""

    image_id: int
    frames: int
    z: int
    target: int = 0          # frame to end on (0 = not shown)
    start_frame: int = 0
    shown: int = 0           # frame currently shown (0 = not placed)
    t0: float = 0.0
    duration: float = 0.0

    def busy(self, now: float) -> bool:
        return self.shown != self.target or now - self.t0 < self.duration


@dataclass
class Ambient:
    shimmer: int | None = None
    shimmer_key: tuple[Any, ...] | None = None
    glints: list[int] = field(default_factory=list)
    glint_key: tuple[Any, ...] | None = None
    halos: dict[tuple[Any, ...], tuple[int, tuple[int, int, int, int]]] = field(default_factory=dict)
    halo_shown: tuple[Any, ...] | None = None
    halo_target: tuple[Any, ...] | None = None
    pending: set[tuple[Any, ...]] = field(default_factory=set)  # being rendered in the worker

    def image_ids(self) -> list[int]:
        ids = [self.shimmer] if self.shimmer else []
        return ids + self.glints + [image_id for image_id, _ in self.halos.values()]


@dataclass
class Flight:
    """A flourish on screen: its frames uploaded while stopped, stepped through by glass."""

    image_id: int
    screen: Any         # the screen it plays on: it goes when that screen does
    ends: list[float]   # when each frame ends, in seconds after t0
    t0: float           # when the upload was sent
    shown: int = 1      # the frame on screen (1-based)


class Effects:
    """Owned by the engine; uses its writer, ids, size, palette and motion level."""

    def __init__(self, engine: GlassEngine) -> None:
        self.engine = engine
        self.scrims: dict[int, Fade] = {}
        self.ambient = Ambient()
        self._restore: dict[int, int] = {}  # scrim frames to come back at after a theme change
        self.flights: list[Flight] = []
        self.rendering: set[int] = set()  # image ids of flourishes still in the worker
        self.fired = 0  # flourishes fired so far: each one's seed

    # -- helpers -----------------------------------------------------------------------

    def _queue(self, data: str) -> None:
        self.engine.writer.queue(data)

    def _delete(self, image_id: int) -> None:
        self._queue(kitty.delete(image_id))
        self.engine.ids.free(image_id)

    def animating(self, now: float) -> bool:
        return bool(self.flights) or any(f.busy(now) for f in self.scrims.values())

    def reset(self) -> None:
        """Forget everything (the engine has already deleted every image)."""
        self.scrims.clear()
        self.ambient = Ambient()
        self._restore.clear()
        self.flights.clear()
        self.rendering.clear()  # their results are dropped when they land

    def recolour(self) -> None:
        """The theme changed: rebuild the images in the new colours, keeping open
        scrims where they are (the command palette previews themes live)."""
        self._restore = {level: fade.shown for level, fade in self.scrims.items() if fade.target}
        for fade in self.scrims.values():
            self._delete(fade.image_id)
        self.scrims.clear()
        self.clear_ambient()
        self.clear_flourishes()

    def stretch(self) -> None:
        """The window was resized: stretch the fades over the new size at once."""
        cols, rows = self.engine.size
        for fade in self.scrims.values():
            if fade.shown:
                self._queue(kitty.place(fade.image_id, 0, 0, cols, rows, fade.z))
        self.clear_ambient()
        self.clear_flourishes()

    # -- fades ------------------------------------------------------------------------

    def _fade_image(self, alpha: float, frames: int, z: int, color: RGB) -> Fade:
        image_id = self.engine.ids.new()
        alphas = [alpha * (k + 1) / frames for k in range(frames)]
        self._queue(kitty.frames_stopped(image_id, 1, 1, solid_frames(color, alphas)))
        return Fade(image_id, frames, z)

    def _fade_to(self, fade: Fade, target: int, now: float, duration: float) -> None:
        animate = self.engine.motion != "minimal" and duration > 0
        fade.start_frame = fade.shown
        fade.target = target
        fade.t0 = now
        fade.duration = duration * abs(target - fade.shown) / max(1, fade.frames) if animate else 0.0
        self._step(fade, now)
        if fade.duration:
            self.engine.kick()

    def _step(self, fade: Fade, now: float) -> None:
        if fade.duration > 0:
            k = min(1.0, (now - fade.t0) / fade.duration)
            frame = round(fade.start_frame + (fade.target - fade.start_frame) * k)
        else:
            frame = fade.target
        if frame == fade.shown:
            return
        if frame <= 0:
            self._queue(kitty.unplace(fade.image_id))
        else:
            command = kitty.animate(fade.image_id, c=frame)
            if fade.shown <= 0:
                cols, rows = self.engine.size
                command += kitty.place(fade.image_id, 0, 0, cols, rows, fade.z)
            self._queue(command)
        fade.shown = frame

    def step(self, now: float, live_levels: set[int]) -> None:
        """Advance fades; delete scrims whose modal has gone and that have faded out."""
        for level, fade in list(self.scrims.items()):
            self._step(fade, now)
            if fade.target == 0 and fade.shown == 0 and level not in live_levels:
                self._delete(fade.image_id)
                del self.scrims[level]

    def update_scrims(self, levels: list[LevelScene], now: float, z_for: Any) -> None:
        """One scrim under each modal screen, fading in as it opens and out as it closes."""
        palette = self.engine.palette
        assert palette is not None
        wanted = {ls.level for ls in levels if ls.level > 0}
        for level in wanted:
            fade = self.scrims.get(level)
            if fade is None:
                fade = self.scrims[level] = self._fade_image(palette.scrim_alpha, SCRIM_FRAMES, z_for(level),
                                                             palette.scrim)
                if level in self._restore:  # same modal, new colours: no fade
                    fade.target = self._restore.pop(level)
                    self._step(fade, now)
            if fade.target != fade.frames:
                self._fade_to(fade, fade.frames, now, SCRIM_TIME)
        for level, fade in list(self.scrims.items()):
            if level not in wanted and fade.target != 0:
                self._fade_to(fade, 0, now, SCRIM_TIME)
        self._restore.clear()
        self.step(now, wanted | {0})

    # -- ambient (motion = full) ---------------------------------------------------------

    def clear_ambient(self) -> None:
        for image_id in self.ambient.image_ids():
            self._delete(image_id)
        self.ambient = Ambient()

    def update_ambient(self, levels: list[LevelScene], z0: int, z_level: Any) -> None:
        engine = self.engine
        assert engine.grid is not None and engine.palette is not None
        amb, grid, pal = self.ambient, engine.grid, engine.palette
        cols, rows = engine.size
        key = (cols, rows, pal)
        if amb.shimmer_key != key:
            if amb.shimmer:
                self._delete(amb.shimmer)
            shimmer_id = amb.shimmer = engine.ids.new()
            amb.shimmer_key = key
            colour = (255, 255, 255) if pal.dark else pal.primary

            def shimmer(shimmer_id: int = shimmer_id) -> str:
                w, h, frames = shimmer_frames(cols, rows, 28, colour)
                gaps = [70] * (len(frames) - 1) + [7000]  # a slow sweep, then a long rest
                return kitty.animation(shimmer_id, w, h, frames, gaps)

            def shimmer_ready(command: str, shimmer_id: int = shimmer_id) -> None:
                if self.ambient is amb and amb.shimmer == shimmer_id:
                    self._queue(command + kitty.place(shimmer_id, 0, 0, cols, rows, z0))

            engine.background_job(shimmer, shimmer_ready)
        corners = levels[0].corners if levels else []
        glint_key = (tuple(corners), cols, rows, pal)
        if amb.glint_key != glint_key:
            for image_id in amb.glints:
                self._delete(image_id)
            amb.glints = []
            amb.glint_key = glint_key
            rng = random.Random(len(corners) * 7 + cols)
            for i, (x, y) in enumerate(corners[:5]):
                image_id = engine.ids.new()
                w, h, frames = glint_frames(grid.sx, grid.sy, 20, rng.random(), pal.foreground)
                gaps = [45] * (len(frames) - 1) + [3500 + 1700 * i]  # rare, and never in step
                self._queue(kitty.animation(image_id, w, h, frames, gaps) + kitty.place(image_id, x, y, 1, 1, z0 + 1))
                amb.glints.append(image_id)
        self._update_halo(levels, z_level)

    def _update_halo(self, levels: list[LevelScene], z_level: Any) -> None:
        engine = self.engine
        assert engine.grid is not None
        amb, grid = self.ambient, engine.grid
        top = levels[-1] if levels else None
        focus = top.focus if top is not None else None
        level = top.level if top is not None else 0
        wanted = None if focus is None else (focus.rect, focus.c1, focus.c2, grid, level)
        amb.halo_target = wanted
        if wanted == amb.halo_shown:
            return
        if amb.halo_shown is not None and amb.halo_shown in amb.halos:
            self._queue(kitty.unplace(amb.halos[amb.halo_shown][0]))
        amb.halo_shown = None
        if wanted is None or focus is None:
            return
        z = z_level(level) + 5
        if wanted in amb.halos:
            image_id, (c, r, w, h) = amb.halos[wanted]
            self._queue(kitty.place(image_id, c, r, w, h, z))
            amb.halo_shown = wanted
            return
        if wanted in amb.pending:
            return
        amb.pending.add(wanted)
        cols, rows = engine.size
        halo_id = engine.ids.new()
        panel = focus

        def halo() -> tuple[tuple[int, int, int, int], str]:
            rect, w, h, frames = halo_frames(grid, panel.rect, cols, rows, panel.c1, panel.c2, frames=16, res=0.35)
            return rect, kitty.animation(halo_id, w, h, frames, 85)

        def halo_ready(result: tuple[tuple[int, int, int, int], str]) -> None:
            amb.pending.discard(wanted)
            if self.ambient is not amb:  # motion changed meanwhile
                self.engine.ids.free(halo_id)
                return
            rect, command = result
            self._queue(command)
            amb.halos[wanted] = (halo_id, rect)
            if amb.halo_target == wanted and amb.halo_shown is None:
                self._queue(kitty.place(halo_id, *rect, z))
                amb.halo_shown = wanted
            while len(amb.halos) > 4:  # keep a few, so tabbing back and forth costs nothing
                old_key = next(k for k in amb.halos if k not in (wanted, amb.halo_shown))
                old_id, _ = amb.halos.pop(old_key)
                self._delete(old_id)

        engine.background_job(halo, halo_ready)

    # -- flourishes ------------------------------------------------------------------------

    @property
    def flourishes(self) -> int:
        """Flourishes playing or still rendering."""
        return len(self.flights) + len(self.rendering)

    def clear_flourishes(self) -> None:
        """Delete every flourish now; the ones still rendering are dropped when they land."""
        for flight in self.flights:
            self._delete(flight.image_id)
        self.flights.clear()
        self.rendering.clear()

    def flourish(self, kind: Flourish, stage: Stage, cells: Rect, z: int, screen: Any) -> None:
        """Render a flourish in the worker, then upload it and place it over ``cells``."""
        engine = self.engine
        image_id = engine.ids.new()
        self.rendering.add(image_id)
        self.fired += 1

        def work() -> tuple[str | None, Any]:
            try:
                frames, gaps = kind.make(stage)
                size = stage.width * stage.height * 4
                if not frames or len(gaps) != len(frames) or any(len(f) != size for f in frames):
                    raise ValueError(f"a flourish maker returned {len(frames)} frames and {len(gaps)} gaps, "
                                     f"not one gap per frame of {stage.width}x{stage.height} RGBA bytes")
                return kitty.frames_stopped(image_id, stage.width, stage.height, frames), list(gaps)
            except Exception as error:  # noqa: BLE001 - counted in done, which also frees the id
                return None, error

        def done(result: tuple[str | None, Any]) -> None:
            command, gaps = result  # or (None, the maker's error)
            dropped = image_id not in self.rendering  # a resize, a new theme or minimal motion meanwhile
            self.rendering.discard(image_id)
            if dropped or command is None or not any(s is screen for s in visible_screens(engine.app)):
                engine.ids.free(image_id)
                if command is None:
                    engine._failed(gaps)
                return
            self._queue(command + kitty.place(image_id, *cells, z))
            ends = list(itertools.accumulate(max(1, g) / 1000 for g in gaps))
            self.flights.append(Flight(image_id, screen, ends, time.monotonic()))  # the clock starts now
            engine.kick()

        engine.background_job(work, done)

    def update_flourishes(self, screens: list[Any], now: float) -> None:
        """Show each flourish's frame for ``now``; delete the ones past their last
        frame, and the ones whose screen has gone (their popup closed)."""
        for flight in list(self.flights):
            elapsed = now - flight.t0
            if elapsed >= flight.ends[-1] or not any(s is flight.screen for s in screens):
                self._delete(flight.image_id)
                self.flights.remove(flight)
                continue
            frame = bisect.bisect_right(flight.ends, elapsed) + 1  # the frame whose time has come
            if frame != flight.shown:
                self._queue(kitty.animate(flight.image_id, c=frame))
                flight.shown = frame
