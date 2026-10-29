"""textual-glass scenes: a small show over a panel, in plain placeholder art.

Run it from the repository root, in Ghostty::

    uv run python textual-glass/examples/scenes/app.py

Six glowing dots fly in on curves from beyond the panel's edge (cropped there),
each one flashes as it docks on a ring around a big soft disc, the caption
follows the scene's cues, and the scene holds at its end: the dots twinkle and
the disc breathes, played by the terminal, and glass sends nothing.

Keys: ``r`` plays it again, ``f9`` the motion level (``minimal`` shows the end
as a still), ``l`` a light theme and back, ``q`` quits. A resize or a new theme
ends the scene; the demo then puts its end back at once (``start=math.inf``).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Hashable
from typing import Any, ClassVar

import numpy as np
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.theme import Theme
from textual.widgets import Footer, Static

from textual_glass import Glass
from textual_glass.player import Showing
from textual_glass.scenes import Actor, Cue, Route, Scene, Stage

NAVY = Theme(name="scene-navy", primary="#38e1ff", secondary="#a46bff", accent="#ffc24b", background="#070b18",
             surface="#0d1430", panel="#16203d", foreground="#c9d6f2", success="#6dffa8", warning="#ffc24b",
             error="#ff4d6d", dark=True,
             variables={"glass-backdrop": "nebula", "glass-scrim-alpha": "0.62", "glass-nebula-strength": "0.8"})
LIGHT = "textual-light"

DOTS = 6
FIRST = 0.2      # seconds: the first dot sets off
SPACING = 0.35   # between dots
FLIGHT = 1.6     # each dot's flight
FLASH = (10, 40)  # a docking flash: frames, ms each
START = "six incoming"
FINAL = "all six docked"

RGB = tuple[int, int, int]


# -- the art: placeholder pictures in numpy, each fully transparent at its border (rule 17) ----------


def _rgba(rgb: np.ndarray, alpha: np.ndarray) -> bytes:
    out = np.dstack([np.clip(rgb, 0, 255), np.clip(alpha, 0, 1) * 255])
    return (out + 0.5).astype(np.uint8).tobytes()


def _distance(width: int, height: int) -> np.ndarray:
    """Each pixel's distance from the middle, 1 at one pixel inside the inscribed ellipse."""
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32) + 0.5
    rx, ry = max(1.0, width / 2 - 1), max(1.0, height / 2 - 1)
    return np.hypot((xs - width / 2) / rx, (ys - height / 2) / ry)


def _tint(color: RGB, white: np.ndarray, dark: bool) -> np.ndarray:
    """``color``, whitened where ``white`` is high on a dark theme (on a light one, colour only)."""
    c = np.array(color, np.float32)
    if not dark:
        return np.broadcast_to(c * 0.85, (*white.shape, 3)).copy()
    w = np.clip(white, 0, 1)[..., None]
    return c * (1 - w) + 255 * w


def dot_frames(size: int, color: RGB, dark: bool, frames: int = 8) -> list[bytes]:
    """A glowing dot that twinkles: its core and halo breathe a little, frame by frame."""
    d = _distance(size, size)
    halo = np.clip(1 - d, 0, 1) ** 2.2
    core = np.clip(1 - d * 3.2, 0, 1) ** 0.7
    out = []
    for k in range(frames):
        glow = 0.8 + 0.2 * math.cos(2 * math.pi * k / frames)
        alpha = 0.95 * halo * glow + core
        out.append(_rgba(_tint(color, core * 1.3 + halo * 0.25, dark), alpha))
    return out


def flash_frames(size: int, color: RGB, dark: bool, frames: int = FLASH[0]) -> list[bytes]:
    """A ring that swells and fades around a bright core, once; the last frame is empty."""
    d = _distance(size, size)
    out = []
    for k in range(frames):
        u = k / (frames - 1)
        ring = np.exp(-(((d - (0.2 + 0.75 * u)) / 0.1) ** 2)) * (1 - u) ** 1.2
        core = np.clip(1 - d * 2.5, 0, 1) * (1 - u) ** 2
        alpha = (ring + core) * np.clip((1 - d) * 6, 0, 1)
        out.append(_rgba(_tint(color, core * 2 + ring * 0.5, dark), alpha))
    return out


def disc_frames(width: int, height: int, color: RGB, dark: bool, frames: int = 12) -> list[bytes]:
    """A big soft disc that breathes, drawn small for the terminal to stretch over its cells."""
    d = _distance(width, height)
    body = np.clip(1 - d, 0, 1) ** 1.6
    out = []
    for k in range(frames):
        breath = 0.72 + 0.28 * (0.5 + 0.5 * math.sin(2 * math.pi * k / frames))
        out.append(_rgba(_tint(color, body ** 3 * 0.5 * breath, dark), body * 0.55 * breath))
    return out


# -- the show ---------------------------------------------------------------------------------------


def make_show(stage: Stage) -> Scene:
    """Six dots fly in from beyond the stage's edge on curves and dock on a ring around a
    breathing disc, each with a flash. Runs in glass's worker thread."""
    width, height = stage.width, stage.height
    cell_w, cell_h = stage.cell
    cx, cy = width / 2, height / 2
    ring = 0.34 * min(width, height)
    colors = stage.colors
    dot = max(16, round(2.8 * cell_w))
    flash = max(32, round(6 * cell_w))

    cols = max(2, min(stage.cols - 2, round(0.46 * min(width, height) / cell_w)))
    rows = max(1, min(stage.rows - 1, round(cols * cell_w / cell_h)))
    disc_w = 64
    disc_h = max(8, round(disc_w * rows * cell_h / (cols * cell_w)))
    actors = [Actor(disc_frames(disc_w, disc_h, colors["accent"], stage.dark), disc_w, disc_h,
                    Route.still(cx, cy), gaps=(110,), cells=(cols, rows))]

    outside = math.hypot(width, height) / 2 + dot  # every point this far from the middle is off the stage
    cues = []
    arrival = 0.0
    for k in range(DOTS):
        angle = -math.pi / 2 + 2 * math.pi * k / DOTS
        target = (cx + ring * math.cos(angle), cy + ring * math.sin(angle))
        begin = (cx + outside * math.cos(angle + 1.2), cy + outside * math.sin(angle + 1.2))
        bend = (cx + 1.3 * ring * math.cos(angle + 0.5), cy + 1.3 * ring * math.sin(angle + 0.5))

        def path(u: float, a: tuple[float, float] = begin, b: tuple[float, float] = bend,
                 c: tuple[float, float] = target) -> tuple[float, float]:
            u = u * u * (3 - 2 * u)  # ease in and out
            return ((1 - u) ** 2 * a[0] + 2 * (1 - u) * u * b[0] + u * u * c[0],
                    (1 - u) ** 2 * a[1] + 2 * (1 - u) * u * b[1] + u * u * c[1])

        depart = FIRST + k * SPACING
        arrival = depart + FLIGHT
        color = colors["primary"] if k % 2 == 0 else colors["secondary"]
        actors.append(Actor(dot_frames(dot, color, stage.dark), dot, dot,
                            Route.sample(path, depart, arrival, step=1 / 60), gaps=(90,), enter=depart, z=20))
        frames, gap = FLASH
        actors.append(Actor(flash_frames(flash, color, stage.dark), flash, flash, Route.still(*target),
                            gaps=(gap,), loop=False, enter=arrival, leave=arrival + frames * gap / 1000 + 0.05,
                            z=30))
        cues.append(Cue(arrival, f"{k + 1} of {DOTS} docked"))
    duration = arrival + 0.6
    cues.append(Cue(duration, FINAL))
    return Scene(actors, duration, cues)


# -- the app ------------------------------------------------------------------------------------------


class GlassDemo(Glass, App[None]):
    """A panel the show plays over, the caption its cues drive, and what the app heard."""

    TITLE = "textual-glass scenes"
    CSS_PATH = "scenes.tcss"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (84, "-wide")]
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "replay", "Replay"),
        Binding("f9", "glass_motion", "Motion"),
        Binding("l", "light", "Light"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.showing: Showing | None = None
        self.heard: list[Hashable] = []  # the current scene's cues, as they came
        self.ends: list[str] = []  # every end heard, oldest first
        self.history: deque[str] = deque(maxlen=12)

    def compose(self) -> ComposeResult:
        yield Static(id="bar")
        with Horizontal(id="main"):
            yield Static(id="stage")
            with Vertical(id="side"):
                yield Static(id="caption")
                yield Static(id="log")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(NAVY)
        self.theme = NAVY.name
        self.query_one("#stage").border_title = "DOCKING"
        self.query_one("#caption").border_title = "NOW"
        self.query_one("#log").border_title = "HEARD"
        self.set_caption(START)
        self.show_bar()
        self.call_after_refresh(self.play)

    # -- playing ---------------------------------------------------------------------------------------

    def play(self, start: float = 0.0) -> None:
        """Play the show over the stage panel; ``math.inf`` puts its end up at once."""
        self.heard = []
        self.set_caption(FINAL if start == math.inf else START)  # cues at or before the start never come
        self.showing = self.glass.play_scene(make_show, at=self.query_one("#stage"), start=start,
                                             on_cue=self.cue, on_end=self.ended)
        if self.showing is None:
            self.note("glass is off: nothing plays")
        self.show_bar()

    def cue(self, data: Hashable) -> None:
        self.heard.append(data)
        self.set_caption(str(data))
        self.note(f"cue: {data}")
        self.show_bar()

    def ended(self, reason: str) -> None:
        self.ends.append(reason)
        self.note(f"end: {reason}")
        if reason in ("resized", "theme", "moved"):  # the same show, at its end, at the new size or colours
            self.call_after_refresh(self.play, math.inf)
        self.show_bar()

    def action_replay(self) -> None:
        self.play()

    def action_light(self) -> None:
        self.theme = LIGHT if self.theme == NAVY.name else NAVY.name

    def action_glass_motion(self, motion: str = "") -> None:
        super().action_glass_motion(motion)
        self.show_bar()

    # -- what the panels say -----------------------------------------------------------------------------

    def caption(self) -> str:
        return str(self.query_one("#caption", Static).content)

    def set_caption(self, text: str) -> None:
        self.query_one("#caption", Static).update(text)

    def note(self, line: str) -> None:
        self.history.append(line)
        self.query_one("#log", Static).update("\n".join(reversed(self.history)))

    def show_bar(self) -> None:
        motion = self.glass.motion if self.glass.active else "off"
        show = self.showing
        state = "none" if show is None else show.ended or ("holding" if self.caption() == FINAL else "playing")
        muted = "$text-muted"
        self.query_one("#bar", Static).update(Content.assemble(
            (" ◆ ", "bold $text-primary"), ("scenes", "bold"),
            ("   motion ", muted), (motion, "bold $text-success" if motion != "off" else "bold $text-warning"),
            ("   scene ", muted), (state, "bold $text-accent")))


def main() -> None:
    GlassDemo().run()


if __name__ == "__main__":
    main()
