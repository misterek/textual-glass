"""Scenes: longer shows an app plays over a widget, made of moving parts.
No Textual imports.

A flourish is a short movie, every frame drawn ahead. A scene is a set of
:class:`Actor`\\ s instead: small images uploaded once, each moved along its
:class:`Route` by glass re-placing it at pixel offsets inside the cells. An
actor's frames either loop in the terminal by itself (a flame, a planet's
clouds) or are stepped through once by glass (a flash, a light coming on),
holding the last. So a thirty-second show costs about what its pictures do,
and once nothing moves (the scene's end, which holds) glass sends nothing.

The app writes the scene: ``make(stage) -> Scene`` runs in glass's worker
thread when the app calls ``app.glass.play_scene(make, at=widget)``. Glass
never plays one by itself. Lengths are terminal pixels on the :class:`Stage`
(the widget's cells); times are seconds into the scene.

**Every frame is fully transparent at its border** (rule 17): a native actor
moves and is cropped at the stage's edge, a stretched one fills whole cells.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..render import RGB

#: At most this many actors, frames per actor, pixels in all (every frame of
#: every actor) and seconds per scene.
MAX_ACTORS = 256
MAX_FRAMES = 64
MAX_PIXELS = 16_000_000
MAX_SECONDS = 300.0

#: Palette roles a stage carries (``Stage.colors``).
ROLES = ("primary", "secondary", "accent", "success", "warning", "error", "foreground", "background")

Point = tuple[float, float]
Key = tuple[float, float, float]  # t, x, y


@dataclass(frozen=True)
class Stage:
    """What a scene is made for: the anchor's cells, in terminal pixels."""

    width: int
    height: int
    cols: int
    rows: int
    #: One cell, in terminal pixels (width, height).
    cell: tuple[float, float]
    #: The theme's colours by role (``ROLES``).
    colors: Mapping[str, RGB]
    #: A dark theme: light reads as light. On a light theme, draw in colour, not white.
    dark: bool
    #: ``minimal`` motion: the scene is shown at its end and nothing moves or loops,
    #: so one frame per actor is enough.
    still: bool
    #: For any randomness: the same seed gives the same scene.
    seed: int


@dataclass(frozen=True)
class Route:
    """Where an actor's centre is over time: keys of ``(t, x, y)`` in order of
    ``t``, with straight lines between them. Before the first key it is at the
    first, after the last at the last. Sample a curve with :meth:`sample`."""

    keys: tuple[Key, ...]

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("a route needs at least one key")
        if any(b[0] < a[0] for a, b in zip(self.keys, self.keys[1:], strict=False)):
            raise ValueError("a route's keys must be in order of time")

    @classmethod
    def still(cls, x: float, y: float) -> Route:
        return cls(((0.0, x, y),))

    @classmethod
    def line(cls, start: Point, end: Point, t0: float, t1: float,
             ease: Callable[[float], float] | None = None, step: float = 1 / 30) -> Route:
        """From ``start`` at ``t0`` to ``end`` at ``t1``, eased (sampled every ``step``)."""
        if ease is None:
            return cls(((t0, *start), (t1, *end)))
        (x0, y0), (x1, y1) = start, end
        return cls.sample(lambda u: (x0 + (x1 - x0) * ease(u), y0 + (y1 - y0) * ease(u)), t0, t1, step)

    @classmethod
    def sample(cls, at: Callable[[float], Point], t0: float, t1: float, step: float = 1 / 30) -> Route:
        """``at(u)`` for ``u`` from 0 to 1, spread over ``t0`` to ``t1``."""
        n = max(1, math.ceil((t1 - t0) / step))
        return cls(tuple((t0 + (t1 - t0) * k / n, *at(k / n)) for k in range(n + 1)))

    def at(self, t: float) -> Point:
        keys = self.keys
        k = bisect.bisect_right(keys, t, key=lambda key: key[0])
        if k == 0:
            return keys[0][1], keys[0][2]
        if k == len(keys):
            return keys[-1][1], keys[-1][2]
        (ta, xa, ya), (tb, xb, yb) = keys[k - 1], keys[k]
        u = (t - ta) / (tb - ta) if tb > ta else 1.0
        return xa + (xb - xa) * u, ya + (yb - ya) * u

    @property
    def settles(self) -> float:
        """When it stops moving (its last key)."""
        return self.keys[-1][0]


@dataclass(frozen=True)
class Actor:
    """One moving part: an image of one or more frames, and where it is when."""

    #: Straight-alpha RGBA frames, ``width * height * 4`` bytes each, transparent at their border.
    frames: Sequence[bytes]
    width: int
    height: int
    route: Route
    #: How long each frame shows, in ms: one value for all, or one per frame.
    gaps: Sequence[int] = (100,)
    #: True: the terminal loops the frames by itself. False: glass steps through them
    #: once from ``enter`` and holds the last.
    loop: bool = True
    #: When it appears and goes, in seconds into the scene. One still on stage at the
    #: scene's end stays there for as long as the scene shows.
    enter: float = 0.0
    leave: float = math.inf
    #: Stacking among the scene's actors: higher draws on top (0 to 99).
    z: int = 0
    #: None: drawn at its own pixels, moved smoothly and cropped at the stage's edge. Or
    #: ``(cols, rows)``: stretched by the terminal over that many cells (for a big, soft
    #: picture rendered small), placed at whole cells and kept inside the stage.
    cells: tuple[int, int] | None = None


@dataclass(frozen=True)
class Cue:
    """A moment the app hears about (``on_cue(data)`` on the app thread): to change a
    caption as a ship lands, say. ``t`` is from 0 to the scene's duration. A cue at or
    before the time the scene starts at (``play_scene(start=...)``, 0 by default)
    never comes: the app shows that state itself."""

    t: float
    data: Hashable


@dataclass(frozen=True)
class Scene:
    actors: Sequence[Actor]
    #: How long it plays, in seconds. Then it holds: what is on stage stays, and only
    #: the loops move (the terminal plays them), so glass sends nothing.
    duration: float
    cues: Sequence[Cue] = ()


#: ``make(stage) -> Scene``, run in glass's worker thread.
SceneMaker = Callable[[Stage], Scene]


def check(scene: Any, stage: Stage) -> None:
    """Raise ValueError, saying what is wrong, unless ``scene`` is a :class:`Scene` within the limits."""
    if not isinstance(scene, Scene):
        raise ValueError(f"a scene maker returned {type(scene).__name__}, not a Scene")
    if not 0 < scene.duration <= MAX_SECONDS:
        raise ValueError(f"a scene lasts more than 0 and at most {MAX_SECONDS:g} s, not {scene.duration:g}")
    if len(scene.actors) > MAX_ACTORS:
        raise ValueError(f"a scene has at most {MAX_ACTORS} actors, not {len(scene.actors)}")
    if any(not 0 <= cue.t <= scene.duration for cue in scene.cues):
        raise ValueError(f"a scene's cues are at 0 to its duration ({scene.duration:g} s): later ones never come")
    pixels = 0
    for n, actor in enumerate(scene.actors):
        size = actor.width * actor.height * 4
        frames = len(actor.frames)
        if actor.width < 1 or actor.height < 1:
            raise ValueError(f"actor {n} is {actor.width}x{actor.height}: it needs at least one pixel")
        if not 1 <= frames <= MAX_FRAMES or any(len(f) != size for f in actor.frames):
            raise ValueError(f"actor {n} has {frames} frames: it needs 1 to {MAX_FRAMES} of "
                             f"{actor.width}x{actor.height} RGBA bytes")
        if len(actor.gaps) not in (1, frames) or min(actor.gaps) < 1:
            raise ValueError(f"actor {n} needs one gap of at least 1 ms, or one per frame")
        if not 0 <= actor.z <= 99 or actor.leave <= actor.enter:
            raise ValueError(f"actor {n} needs a z from 0 to 99 and to leave after it enters")
        if actor.cells is not None:
            cols, rows = actor.cells
            if not (1 <= cols <= stage.cols and 1 <= rows <= stage.rows):
                raise ValueError(f"actor {n} is stretched over {cols}x{rows} cells, "
                                 f"more than the stage's {stage.cols}x{stage.rows}")
        pixels += actor.width * actor.height * frames
    if pixels > MAX_PIXELS:
        raise ValueError(f"a scene has at most {MAX_PIXELS:,} pixels in all its frames, not {pixels:,}")
