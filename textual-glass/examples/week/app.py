"""textual-glass: the week in review, the space scene (``textual_glass.scenes.space``) over a page.

Run it from the repository root, in Ghostty::

    uv run python textual-glass/examples/week/app.py
    uv run python textual-glass/examples/week/app.py --look nightfall --items 150
    uv run python textual-glass/examples/week/app.py --plain      # stock Textual: the tally, no show

A made-up week of work plays over a full-page panel: a ship for each piece of
work warps in, flies to its place and lands, and its light comes on. The
caption says what just landed and the tally counts it. Then the summary holds:
one light per piece of work, its shape and colour the kind, its place the time
of the week. Only the world's loop moves (the terminal plays it) and glass
sends nothing. On a light theme the show brings its own night.

Keys: ``1`` ``2`` ``3`` the look (harbour, nightfall, halo), ``+`` ``-`` a
busier or quieter week (12, 25, 60 or 150 pieces of work), ``r`` plays it
again, ``f9`` the motion level (``minimal`` shows the summary as a still),
``l`` a light theme and back, ``q`` quits. A resize or a new theme ends the
show, and the page puts the summary straight back (``start=math.inf``).
"""

from __future__ import annotations

import argparse
import math
import os
import random
from collections import Counter
from collections.abc import Hashable
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.content import Content
from textual.theme import Theme
from textual.widgets import Footer, Static

from textual_glass import Glass
from textual_glass.player import Showing
from textual_glass.scenes import Scene, Stage, space

NAVY = Theme(name="week-navy", primary="#38e1ff", secondary="#a46bff", accent="#ffc24b", background="#070b18",
             surface="#0d1430", panel="#16203d", foreground="#c9d6f2", success="#6dffa8", warning="#ffc24b",
             error="#ff4d6d", dark=True,
             variables={"glass-backdrop": "nebula", "glass-scrim-alpha": "0.62", "glass-nebula-strength": "0.8"})
DAYLIGHT = Theme(name="week-daylight", primary="#0369d4", secondary="#7c3aed", accent="#d97706",
                 background="#f4f6fb", surface="#e9edf6", panel="#dde3ef", foreground="#1e2433", success="#15803d",
                 warning="#d97706", error="#be123c", dark=False)

#: How busy the week can be, quietest first.
COUNTS = (12, 25, 60, 150)
LOOKS = {"1": "harbour", "2": "nightfall", "3": "halo"}
END = space.END  # the show's last cue: the summary holds

# -- a made-up week -------------------------------------------------------------------------------

#: Each kind's mark and theme colour, as the tally shows them (the show draws the same shapes).
MARKS = {"merged": ("●", "$text-secondary"), "review": ("◎", "$text-success"), "deploy": ("◆", "$text-primary"),
         "ticket": ("•", "$foreground"), "release": ("✦", "$text-accent")}
NAMES = {"merged": ("merged", "merged"), "review": ("review", "reviews"), "deploy": ("deploy", "deploys"),
         "ticket": ("ticket", "tickets"), "release": ("release", "releases")}
CHANGES = ["fix the flaky test", "retry uploads on timeout", "cache the font metrics", "drop the old config loader",
           "speed up the search index", "add a dark theme", "log slow queries", "pin the build image",
           "split the settings page", "batch the email sends", "trim the docker layers", "guard against empty pages",
           "move the cron jobs", "paginate the audit log", "warn before quota runs out", "tidy the error messages"]
SERVICES = ["api", "web", "billing", "search", "worker", "gateway", "reports"]
CHORES = ["rotate the signing keys", "tidy the alert rules", "renew the staging certs", "clean up old buckets",
          "update the runbook", "archive last quarter's logs", "resize the build agents", "triage the backlog"]
NAMESAKES = ["Comet", "Kestrel", "Lantern", "Meridian", "Orbit"]
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def make_week(count: int, seed: int = 7) -> list[space.Work]:
    """A made-up week of ``count`` pieces of work, with one release (two in a big week)."""
    return space.sample_week(count, seed=seed, releases=2 if count >= 100 else 1)


def title(work: space.Work, i: int) -> str:
    """A made-up title for a piece of work, the same every time."""
    rnd = random.Random(i * 7919 + int(work.hour * 60))
    if work.kind == "merged":
        return f"merged #{rnd.randint(300, 999)} {rnd.choice(CHANGES)}"
    if work.kind == "review":
        return f"reviewed #{rnd.randint(300, 999)} {rnd.choice(CHANGES)}"
    if work.kind == "deploy":
        return f"deployed {rnd.choice(SERVICES)} {rnd.randint(2, 9)}.{rnd.randint(0, 40)} to production"
    if work.kind == "ticket":
        return f"closed #{rnd.randint(1000, 4999)} {rnd.choice(CHORES)}"
    return f"released v{rnd.randint(2, 6)}.{rnd.randint(0, 9)} “{rnd.choice(NAMESAKES)}”"


def when(work: space.Work) -> str:
    hour = work.hour % 24
    return f"{DAYS[work.day]} {int(hour):02d}:{int(hour % 1 * 60):02d}"


# -- the page ---------------------------------------------------------------------------------------


class WeekDemo(Glass, App[None]):
    """The week over a page: the show's panel, a caption that follows its cues, and the tally."""

    TITLE = "textual-glass: the week in review"
    CSS_PATH = "week.tcss"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "look('harbour')", "Harbour", show=False),
        Binding("2", "look('nightfall')", "Nightfall", show=False),
        Binding("3", "look('halo')", "Halo", show=False),
        Binding("plus,equals_sign", "busier", "Busier"),
        Binding("minus", "quieter", "Quieter"),
        Binding("r", "replay", "Replay"),
        Binding("f9", "glass_motion", "Motion"),
        Binding("l", "light", "Light"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, look: str = "halo", items: int = 25, seed: int = 7, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.look = look if look in space.LOOKS else "halo"
        self.count = min(COUNTS, key=lambda c: abs(c - items))
        self.seed = seed
        self.week = make_week(self.count, seed)
        self.showing: Showing | None = None
        #: The current show's cues, as they came, and every end heard, oldest first.
        self.heard: list[Hashable] = []
        self.ends: list[str] = []
        self.tally: Counter[str] = Counter()
        self.done = False  # the summary is up: the tally is the week's

    def compose(self) -> ComposeResult:
        yield Static(id="bar")
        with Vertical(id="panel"):
            yield Static(id="stage")
            yield Static(id="caption")
            yield Static(id="tally")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(NAVY)
        self.register_theme(DAYLIGHT)
        self.theme = NAVY.name
        panel = self.query_one("#panel")
        panel.border_title = "THIS WEEK"
        panel.border_subtitle = "1 2 3 look · + − busier · r again"
        self.show_text()
        self.call_after_refresh(self.play)

    # -- playing -----------------------------------------------------------------------------------

    def play(self, start: float = 0.0) -> None:
        """Play the week over the stage; ``math.inf`` puts the summary up at once (its cues never come)."""
        week, look = self.week, self.look
        self.heard = []
        self.done = start == math.inf
        self.tally = Counter(w.kind for w in week) if self.done else Counter()

        def make(stage: Stage) -> Scene:  # in glass's worker thread
            return space.make(stage, week, look)

        self.showing = None
        if getattr(self, "glass", None) is not None and self.glass.active:
            self.showing = self.glass.play_scene(make, at=self.query_one("#stage"), start=start,
                                                 on_cue=self.cue, on_end=self.ended)
        if self.showing is None:  # glass is off: the week is the tally
            self.done = True
            self.tally = Counter(w.kind for w in week)
        self.show_text()

    def cue(self, data: Hashable) -> None:
        self.heard.append(data)
        if data == END:  # the summary holds: the tally is the week's (a still at minimal motion has only this cue)
            self.done = True
            self.tally = Counter(w.kind for w in self.week)
        elif isinstance(data, tuple) and data[0] == "landed":
            _, i, kind = data
            self.tally[kind] += 1
            self.show_text(landed=i)
            return
        self.show_text()

    def ended(self, reason: str) -> None:
        self.ends.append(reason)
        if reason in ("resized", "theme", "moved"):  # the same week, at its end, at the new size or colours
            self.call_after_refresh(self.play, math.inf)
        self.show_text()

    # -- keys ----------------------------------------------------------------------------------------

    def action_look(self, look: str) -> None:
        self.look = look
        self.play()

    def action_busier(self) -> None:
        self._resize_week(+1)

    def action_quieter(self) -> None:
        self._resize_week(-1)

    def _resize_week(self, step: int) -> None:
        k = min(len(COUNTS) - 1, max(0, COUNTS.index(self.count) + step))
        if COUNTS[k] != self.count:
            self.count = COUNTS[k]
            self.week = make_week(self.count, self.seed)
        self.play()

    def action_replay(self) -> None:
        self.play()

    def action_light(self) -> None:
        self.theme = DAYLIGHT.name if self.theme == NAVY.name else NAVY.name

    def action_glass_motion(self, motion: str = "") -> None:
        super().action_glass_motion(motion)
        self.show_text()

    # -- what the page says ----------------------------------------------------------------------------

    def state(self) -> str:
        show = self.showing
        if show is None:
            return "off" if self.done else "none"
        if show.ended is not None:
            return show.ended
        return "holding" if self.done else "playing"

    def caption_text(self) -> str:
        return str(self.query_one("#caption", Static).content)

    def show_text(self, landed: int | None = None) -> None:
        muted = "$text-muted"
        glass = getattr(self, "glass", None)
        motion = glass.motion if glass is not None and glass.active else "off"
        self.query_one("#bar", Static).update(Content.assemble(
            (" ◆ ", "bold $text-primary"), ("week in review", "bold"),
            ("   look ", muted), (self.look, "bold $text-accent"),
            ("   week ", muted), (str(self.count), "bold"),
            ("   motion ", muted), (motion, "bold $text-success" if motion != "off" else "bold $text-warning"),
            ("   ", ""), (self.state(), "bold $text-secondary")))
        caption = self.query_one("#caption", Static)
        if landed is not None:
            work = self.week[landed]
            mark, colour = MARKS[work.kind]
            caption.update(Content.assemble((f"{mark} ", f"bold {colour}"), (title(work, landed), "bold"),
                                            (f"   {when(work)}", muted),
                                            (f"   {sum(self.tally.values())} of {len(self.week)}", muted)))
        elif self.done:
            days = Counter(w.day for w in self.week)
            busiest = max(days, key=lambda d: days[d]) if days else 0
            caption.update(Content.assemble(("That was the week: ", "bold"),
                                            (f"{len(self.week)} pieces of work", "bold $text-accent"),
                                            (f"   busiest on {DAYS[busiest]} ({days[busiest]})", muted)))
        elif not self.heard:
            caption.update(Content.assemble(("Incoming: ", muted), (f"{len(self.week)} pieces of work", "bold")))
        parts: list[tuple[str, str]] = []
        for kind in space.KINDS:
            mark, colour = MARKS[kind]
            n = self.tally[kind]
            parts += [(f"{mark} ", colour), (f"{n} ", "bold" if self.done else ""),
                      (NAMES[kind][n != 1], muted), ("   ", "")]
        if self.done:
            parts.insert(0, (f"{len(self.week)} in all   ", "bold"))
        self.query_one("#tally", Static).update(Content.assemble(*parts[:-1]))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--look", choices=space.LOOKS, default="halo")
    parser.add_argument("--items", type=int, choices=COUNTS, default=25, help="how busy the week is")
    parser.add_argument("--plain", action="store_true", help="stock Textual, no glass: the tally, no show")
    args = parser.parse_args(argv)
    if args.plain:
        os.environ["TEXTUAL_GLASS"] = "off"
    WeekDemo(look=args.look, items=args.items).run()


if __name__ == "__main__":
    main()
