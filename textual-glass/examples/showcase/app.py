"""textual-glass showcase: one ops console, two views, made-up data.

Run it from ``textual-glass/`` under the repository root (in Ghostty, for the glass)::

    uv run python examples/showcase/app.py            # with glass
    uv run python examples/showcase/app.py --plain    # stock Textual, for comparison
    uv run python examples/showcase/app.py --image ~/Pictures/cat.gif   # your picture in the modal

Keys: ``1`` stock widgets, ``2`` textual-glass's pixel widgets, ``enter``
opens the row under the cursor, ``m`` the "ready to ship" modal (``enter``
there ships the release), ``r`` restarts a service, ``ctrl+p`` the command
palette, ``f9`` the motion level, ``shift+f9`` glass off and on, ``ctrl+z``
suspends to the shell (``fg`` comes back), ``q`` quits.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, ClassVar

from dialogs import ServiceScreen, ShipScreen
from textual import on
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.content import Content
from textual.screen import Screen
from textual.theme import Theme
from textual.timer import Timer
from textual.widgets import ContentSwitcher, DataTable, Footer, OptionList, Static
from views import PixelView, StockView, View, fmt_ms
from world import INCIDENTS, RELEASE, RUNBOOK, STATES, Tones, World

from textual_glass import Glass, LoadedImage, SelectList, load_image

VIEWS = {"stock": ("Stock", "Stock widgets"), "pixels": ("Pixels", "Pixel widgets")}
URGENT = ("down", "degraded", "approval")  # the states the top bar counts


class TopBar(Horizontal):
    """The app's name, the views (the current one lit) and what needs attention."""

    def compose(self) -> ComposeResult:
        yield Static(id="brand")
        yield Static(id="summary")

    def on_mount(self) -> None:
        self._shown: tuple[World, Tones, str | None] | None = None

    def on_resize(self) -> None:
        if self._shown is not None:
            self.show(*self._shown)

    def show(self, world: World, tones: Tones, current: str | None) -> None:
        self._shown = (world, tones, current)
        t = tones
        wide = self.size.width >= 120
        brand: list[Any] = [("◆ ", f"bold {t.text_primary}"), ("textual-glass", "bold"), "  "]
        for n, (key, (short, name)) in enumerate(VIEWS.items(), 1):
            on_it = key == current
            brand += [(f" {n} ", f"bold {t.background} on {t.text_primary}" if on_it else f"bold {t.muted}"),
                      (f" {name if wide else short} ", "bold" if on_it else t.muted), " "]
        self.query_one("#brand", Static).update(Content.assemble(*brand))
        counts = world.counts
        summary: list[Any] = []
        for state in (*URGENT, "deploying", "restarting") if wide else URGENT:
            if counts.get(state):
                summary += [("  ● ", t.state(state)), (f"{counts[state]} {STATES[state][0]}", t.muted)]
        if wide:
            summary += [("  ▲ ", t.text_warning), (f"{len(INCIDENTS)} incidents", f"bold {t.text_warning}")]
        self.query_one("#summary", Static).update(Content.assemble(*summary))


class Showcase(App[None]):
    """Services, incidents and deploys, shown with stock widgets (1) and with
    textual-glass's pixel widgets (2)."""

    TITLE = "textual-glass showcase"
    CSS_PATH = "showcase.tcss"
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-narrow"), (120, "-wide")]
    VERTICAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-short"), (36, "-tall")]
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "view('stock')", "Stock"),
        Binding("2", "view('pixels')", "Pixels"),
        Binding("m", "ship", "Ship"),
        Binding("r", "restart", "Restart"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+z", "suspend_process", "Suspend", show=False),  # Textual's own action: SIGTSTP
    ]

    def __init__(self, image: LoadedImage | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.image = image
        self.world = World()
        self.tones = Tones({})
        self._shipping: Timer | None = None

    def compose(self) -> ComposeResult:
        yield TopBar(id="bar")
        with ContentSwitcher(initial="stock", id="views"):
            yield StockView(id="stock")
            yield PixelView(id="pixels")
        yield Footer()

    def on_mount(self) -> None:
        self.theme_changed_signal.subscribe(self, self._theme_changed)
        self.redraw()
        self.view.focus_default()

    def _theme_changed(self, theme: Theme) -> None:
        self.redraw()

    # -- drawing ----------------------------------------------------------------------

    @property
    def view(self) -> View:
        switcher = self.query_one("#views", ContentSwitcher)
        return self.query_one(f"#{switcher.current}", View)

    def redraw(self) -> None:
        self.tones = Tones(self.get_css_variables())
        for view in self.query(View):
            view.show(self.world, self.tones)
        self._bar()

    def _bar(self) -> None:
        self.query_one(TopBar).show(self.world, self.tones, self.query_one("#views", ContentSwitcher).current)

    # -- actions --------------------------------------------------------------------------

    def action_view(self, name: str) -> None:
        self.query_one("#views", ContentSwitcher).current = name
        self.view.focus_default()
        self._bar()

    def action_ship(self) -> None:
        if self.world.release is None and len(self.screen_stack) == 1:
            self.push_screen(ShipScreen(self.image, self.tones), self._shipped)
        elif self.world.release is not None:
            self.notify(f"{' '.join(RELEASE)} is already {'live' if self.world.release >= 1 else 'shipping'}")

    def _shipped(self, ship: bool | None) -> None:
        if not ship:
            return
        self.world.release = 0.0
        self.world.update(RELEASE[0], state="deploying")
        self.notify(f"Shipping {' '.join(RELEASE)} to production", title="Ship")
        self._shipping = self.set_interval(0.25, self._ship_step)
        self.redraw()

    def _ship_step(self) -> None:
        release = min(1.0, (self.world.release or 0.0) + 0.05)
        self.world.release = release
        if release >= 1:
            if self._shipping is not None:
                self._shipping.stop()
            self.world.update(RELEASE[0], state="ok", version=RELEASE[1], p95=212, errors=0.1, attention=1,
                              note=f"{RELEASE[1]} shipped a minute ago; p95 is back under 250 ms.")
            self.world.services.sort(key=lambda s: -s.attention)
            self.notify(f"{' '.join(RELEASE)} is live", title="Ship", severity="information")
        self.redraw()

    def action_restart(self) -> None:
        key = self.view.current()
        if key is None:
            return
        s = self.world.service(key)
        if s.state == "paused":
            self.notify(f"{key} is scaled to zero", severity="warning")
            return
        self.world.update(key, state="restarting", note="Restarting…")
        self.notify(f"Restarting {key} on {s.host}", title="Restart", severity="warning")
        self.set_timer(3.0, lambda: self._restarted(key))
        self.redraw()

    def _restarted(self, key: str) -> None:
        before = self.world.service(key)
        s = self.world.update(key, state="ok", attention=1, errors=min(0.1, before.errors), p95=before.latency[0],
                              note="Restarted a moment ago; back to its usual latency.")
        self.world.services.sort(key=lambda svc: -svc.attention)
        self.notify(f"{key} is healthy again (p95 {fmt_ms(s.p95)})", title="Restart")
        self.redraw()

    def open_service(self, key: str | None) -> None:
        if key is None:
            return
        s = self.world.service(key)
        notes = RUNBOOK if key == RELEASE[0] else f"## {key}\n\n{s.note}\n\nOwned by **{s.team}**, runs on `{s.host}`."
        self.push_screen(ServiceScreen(s, self.tones, notes))

    # -- events ------------------------------------------------------------------------------

    @on(DataTable.RowSelected, "#services, #incidents")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        self.open_service(self.view.current())

    @on(SelectList.Selected)
    def _service_selected(self, event: SelectList.Selected) -> None:
        self.open_service(event.row.key)

    @on(OptionList.OptionSelected, "#deploys")
    def _deploy_selected(self, event: OptionList.OptionSelected) -> None:
        service, _, version = str(event.option.id).partition("@")
        if (service, version) == RELEASE and self.world.release is None:
            self.action_ship()
        else:
            self.notify(f"{service} {version}: {self.world.service(service).note}", title="Deploy")

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        for key, (_, name) in VIEWS.items():
            yield SystemCommand(f"Show: {name}", f"Switch to the {name.lower()} view",
                                lambda key=key: self.action_view(key))
        yield SystemCommand(f"Ship {' '.join(RELEASE)}", "Open the ready-to-ship modal", self.action_ship)


class GlassShowcase(Glass, Showcase):
    """The showcase with textual-glass: the only difference is this class."""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plain", action="store_true", help="stock Textual, no glass")
    parser.add_argument("--image", metavar="PATH", help="a PNG, JPEG or (animated) GIF for the modal")
    args = parser.parse_args(argv)
    image = None
    if args.image:
        try:
            image = load_image(Path(args.image).expanduser())
        except (ImportError, OSError) as error:
            sys.exit(f"--image {args.image}: {error}")
    (Showcase if args.plain else GlassShowcase)(image=image).run()


if __name__ == "__main__":
    main()
