"""textual-glass flourishes: every kind, over a tiny made-up dashboard, to pick the ones you like.

Run it from the repository root, in Ghostty::

    uv run python textual-glass/examples/flourishes/app.py            # with glass
    uv run python textual-glass/examples/flourishes/app.py --plain    # stock Textual: nothing plays

Keys: ``↑`` ``↓`` pick a kind, ``enter`` or ``space`` plays it at the target
panel (a comet flies from the target to the next panel), ``t`` moves the
target, ``a`` plays every kind in turn, ``l`` a light theme and back, ``f9``
the motion level (``minimal`` plays nothing), ``shift+f9`` glass off and on,
``q`` quits.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
from typing import Any, ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid, Horizontal, Vertical
from textual.content import Content
from textual.theme import Theme
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Static

from textual_glass import FLOURISHES, Glass, RingGauge, SelectList
from textual_glass.engine import MAX_FLOURISHES
from textual_glass.widgets import Part, ellipsize, tone
from textual_glass.widgets.base import BRIGHT, DIM, FAINT

NAVY = Theme(name="flourish-navy", primary="#38e1ff", secondary="#a46bff", accent="#ffc24b", background="#070b18",
             surface="#0d1430", panel="#16203d", foreground="#c9d6f2", success="#6dffa8", warning="#ffc24b",
             error="#ff4d6d", dark=True,
             variables={"glass-backdrop": "nebula", "glass-scrim-alpha": "0.62", "glass-nebula-strength": "0.8"})
LIGHT = "textual-light"
AUTOPLAY_GAP = 2.5  # seconds between kinds

# The dashboard's panels, in target order; made-up data.
PANELS = {"queue": "QUEUE", "gauges": "GAUGES", "launches": "LAUNCHES", "log": "LOG"}
QUEUE = [("build", "web", "2m"), ("test", "api", "5m"), ("deploy", "web", "9m"), ("lint", "docs", "12m"),
         ("bench", "db", "20m"), ("audit", "api", "31m"), ("pack", "web", "44m")]
LAUNCHES = [("●", "$text-success", "kestrel", "14:05", "go"), ("●", "$text-warning", "heron", "15:30", "hold"),
            ("○", "$text-muted", "osprey", "17:10", "draft"), ("○", "$text-muted", "swift", "tmrw", "draft")]
LOG = ["build 4126 passed", "deploy 4125 rolled out", "backup finished", "cert renewed for 90 days",
       "queue drained", "nightly report sent"]


class KindList(SelectList[str]):
    """Every kind: its number and name, and what it could mean on a dashboard."""

    row_lines = 2

    def render_row(self, row: str, line: int, width: int, selected: bool) -> list[Part]:
        pal = self.theme_palette
        if line:
            return [("    ", ""), (ellipsize(FLOURISHES[row].about, width - 4), tone(pal, DIM))]
        number = list(FLOURISHES).index(row) + 1
        return [(f" {number:>2} ", tone(pal, DIM if selected else FAINT)),
                (row, f"bold {tone(pal, BRIGHT)}" if selected else tone(pal))]


class Demo(App[None]):
    """A queue, two gauges, launches and a log, and a list of every flourish to play over them."""

    TITLE = "textual-glass flourishes"
    CSS_PATH = "flourishes.tcss"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (84, "-medium"), (120, "-wide")]
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("space", "play", "Play"),
        Binding("t", "target", "Target"),
        Binding("a", "autoplay", "Auto"),
        Binding("l", "light", "Light"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.target = 0  # the panel flourishes play at (an index into PANELS)
        self.last: str | None = None  # the kind played last
        self._autoplay: Timer | None = None
        self._autoplay_next = 0
        self.history: deque[str] = deque(LOG, maxlen=20)  # the LOG panel, newest last

    def compose(self) -> ComposeResult:
        yield Static(id="bar")
        with Horizontal(id="main"):
            with Vertical(id="side"):
                with Vertical(id="now"):
                    with Horizontal(id="now-top"):
                        yield Static(id="now-name")
                        yield Static(id="now-count")
                    yield Static(id="now-about")
                yield KindList(id="kinds")
            with Grid(id="board"):
                yield DataTable(id="queue", classes="panel", cursor_type="none", zebra_stripes=True)
                with Horizontal(id="gauges", classes="panel"):
                    yield RingGauge("CPU", fraction=0.42, text="42%")
                    yield RingGauge("MEM", fraction=0.67, text="67%")
                yield Static(id="launches", classes="panel")
                yield Static(id="log", classes="panel")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(NAVY)
        self.theme = NAVY.name
        for key, title in PANELS.items():
            self.query_one(f"#{key}").border_title = title
        self.query_one("#now").border_title = "NOW"
        table = self.query_one("#queue", DataTable)
        table.can_focus = False
        table.add_columns("job", "host", "age")
        table.add_rows(QUEUE)
        self.query_one("#launches", Static).update(Content("\n").join(
            Content.assemble((dot, colour), " ", (name, "bold"), " ", (when, "$text-muted"), " ", (state, colour))
            for dot, colour, name, when, state in LAUNCHES))
        kinds = self.query_one(KindList)
        kinds.border_title = "KINDS"
        kinds.set_rows(list(FLOURISHES))
        kinds.focus()
        self._show_target()
        self._show_log()
        self._show_now(None, False)
        self.show_bar()

    # -- what the panels say ----------------------------------------------------------------

    def motion_text(self) -> str:
        glass = getattr(self, "glass", None)
        if glass is None:
            return "plain"
        return glass.motion if glass.active else "off"

    def show_bar(self) -> None:
        motion = self.motion_text()
        tone_of = "$text-success" if motion in ("subtle", "full") else "$text-warning"
        muted = "$text-muted"
        self.query_one("#bar", Static).update(Content.assemble(
            (" ◆ ", "bold $text-primary"), ("flourishes", "bold"),
            ("   motion ", muted), (motion, f"bold {tone_of}"),
            ("   target ", muted), (list(PANELS.values())[self.target], "bold $text-accent"),
            ("   autoplay ", muted), (("on", "bold $text-success") if self._autoplay else ("off", muted))))

    def _why_not(self) -> str:
        glass = getattr(self, "glass", None)
        if glass is None:
            return "plain Textual: nothing plays"
        if not glass.active:
            return "glass is off: nothing plays"
        if glass.motion == "minimal":
            return "minimal motion plays nothing (f9)"
        return f"{MAX_FLOURISHES} are playing already"

    def _show_now(self, name: str | None, played: bool) -> None:
        names = list(FLOURISHES)
        title, count, about = "pick a kind", "", "enter or space plays it at the target"
        if name is not None:
            title, count = name.upper(), f" {names.index(name) + 1}/{len(names)} "
            about = FLOURISHES[name].about if played else self._why_not()
        mark = ("▶ ", "bold $text-primary") if played else ("✕ ", "bold $text-warning") if name else ("", "")
        self.query_one("#now-name", Static).update(Content.assemble(
            mark, (title, "bold $text-primary" if played else "bold $text-muted")))
        self.query_one("#now-count", Static).update(Content.assemble((count, "bold $background on $accent")))
        self.query_one("#now-about", Static).update(Content(about))
        self.query_one("#now").border_title = "NOW · autoplay" if self._autoplay else "NOW"

    def _show_target(self) -> None:
        for i, key in enumerate(PANELS):
            panel = self.query_one(f"#{key}")
            panel.set_class(i == self.target, "-target")
            panel.border_subtitle = "target" if i == self.target else ""

    def _show_log(self) -> None:
        self.query_one("#log", Static).update(Content("\n".join(reversed(self.history))))

    # -- playing ------------------------------------------------------------------------------

    def play(self, name: str) -> None:
        """Play ``name`` at the target panel; a comet flies on to the next panel."""
        panels = [self.query_one(f"#{key}") for key in PANELS]
        at = panels[self.target]
        to = panels[(self.target + 1) % len(panels)] if name == "comet" else None
        glass = getattr(self, "glass", None)
        played = glass is not None and glass.flourish(name, at=at, to=to)
        self.last = name
        self._show_now(name, played)
        where = list(PANELS.values())[self.target]
        self.history.append(f"{datetime.now():%H:%M} {'▶' if played else '✕'} {name} at {where}")
        self._show_log()

    def action_play(self) -> None:
        kinds = self.query_one(KindList)
        if kinds.highlighted is not None:
            self.play(kinds.highlighted)

    @on(SelectList.Selected, "#kinds")
    def _chosen(self, event: SelectList.Selected) -> None:
        self.play(event.row)

    def action_target(self) -> None:
        self.target = (self.target + 1) % len(PANELS)
        self._show_target()
        self.show_bar()

    def action_autoplay(self) -> None:
        if self._autoplay is not None:
            self._autoplay.stop()
            self._autoplay = None
        else:
            self._autoplay_next = 0
            self._autoplay = self.set_interval(AUTOPLAY_GAP, self._autoplay_step)
            self._autoplay_step()
        self.query_one("#now").border_title = "NOW · autoplay" if self._autoplay else "NOW"
        self.show_bar()

    def _autoplay_step(self) -> None:
        names = list(FLOURISHES)
        n = self._autoplay_next % len(names)
        self._autoplay_next = n + 1
        self.query_one(KindList).select_index(n)
        self.play(names[n])

    def action_light(self) -> None:
        self.theme = LIGHT if self.theme == NAVY.name else NAVY.name


class GlassDemo(Glass, Demo):
    """The demo with textual-glass: the flourishes play."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("f9", "glass_motion", "Motion")]

    def action_glass_motion(self, motion: str = "") -> None:
        super().action_glass_motion(motion)
        self.show_bar()

    def action_glass_toggle(self) -> None:
        super().action_glass_toggle()
        self.show_bar()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--plain", action="store_true", help="stock Textual, no glass: nothing plays")
    args = parser.parse_args(argv)
    (Demo if args.plain else GlassDemo)().run()


if __name__ == "__main__":
    main()
