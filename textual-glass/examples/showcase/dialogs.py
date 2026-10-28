"""Popups: the "ready to ship" modal with its pixel art, and a service's details.

Both are stock ``ModalScreen``s. With glass, the screen below gets a pixel
scrim and the dialog its own glass; the art is a ``GlassImage`` (real pixels
the terminal holds, half blocks without glass).
"""

from __future__ import annotations

from typing import ClassVar

from art import rocket_frames
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.events import Click
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label, Markdown, Static
from world import RELEASE, Service, Tones

from textual_glass import GlassImage, LoadedImage


def art_widget(image: LoadedImage | None) -> GlassImage:
    """The modal's picture: ``--image`` if given, else the rocket drawn in code."""
    if image is not None:
        return GlassImage.from_image(image, id="art", cache_key="ship-art-file")
    frames, gap = rocket_frames()
    first, *rest = frames
    return GlassImage(first, first.shape[1], first.shape[0], frames=rest, gap_ms=gap, id="art", cache_key="ship-art")


def history(version: str, count: int) -> list[str]:
    """``version`` and the releases before it, newest first."""
    major, minor, patch = (int(p) for p in version.lstrip("v").split("."))
    out = []
    for _ in range(count):
        out.append(f"v{major}.{minor}.{patch}")
        if patch:
            patch -= 1
        elif minor:
            minor, patch = minor - 1, 9
        else:
            break
    return out


class ShipScreen(ModalScreen[bool]):
    """Claude Code asks to ship the release; enter ships it, escape doesn't."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "dismiss(True)", "Ship it"),
        Binding("escape", "dismiss(False)", "Not now"),
    ]

    def __init__(self, image: LoadedImage | None, tones: Tones) -> None:
        super().__init__()
        self.image = image
        self.tones = tones

    def compose(self) -> ComposeResult:
        service, version = RELEASE
        t = self.tones
        ok = ("✓ ", t.text_success)
        with Horizontal(id="ship-dialog"):
            yield art_widget(self.image)
            with Vertical(id="ship-text"):
                yield Label(Content.assemble(("Ready for launch", f"bold {t.waiting}")), id="ship-title")
                yield Label(Content.assemble((f"{service} {version}", "bold"), " → production"), id="ship-what")
                yield Label(Content.assemble(("Claude Code", f"bold {t.accent}"), " is waiting in ",
                                             ("ship-checkout", "bold")))
                yield Static(f"$ deploy {service} {version} --env prod", id="ship-command", markup=False)
                yield Label(Content.assemble(ok, "canary healthy for 30 min"))
                yield Label(Content.assemble(ok, "14 of 14 checks passed on #491"))
                yield Label(Content.assemble(ok, "approved by priya"))
                yield Label(Content.assemble(("enter", f"bold {t.text_primary}"), " ship it   ",
                                             ("esc", f"bold {t.text_primary}"), " not now"), id="ship-keys")

    def on_click(self, event: Click) -> None:
        if event.widget is self:
            self.dismiss(False)


class ServiceScreen(ModalScreen[None]):
    """Everything about one service: its numbers, notes and recent deploys."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss", "Close"),
        Binding("enter", "dismiss", "Close", show=False),
    ]

    def __init__(self, service: Service, tones: Tones, notes: str) -> None:
        super().__init__()
        self.service = service
        self.tones = tones
        self.notes = notes

    def compose(self) -> ComposeResult:
        s, t = self.service, self.tones
        colour = t.state_text(s.state)
        with Vertical(id="service-dialog"):
            yield Label(Content.assemble((s.key, "bold"), " ", (s.version, t.muted), "  ", (f"● {s.label}", colour)),
                        id="service-title")
            yield Label(Content.assemble((s.team, t.muted), " · ", s.host, " · ", (s.region, t.muted)),
                        classes="service-line")
            p95 = f"{s.p95 / 1000:.2f} s" if s.p95 >= 1000 else f"{s.p95} ms" if s.p95 else "–"
            yield Label(Content.assemble("p95 ", (p95, "bold"), "   errors ", (f"{s.errors:.1f}%", "bold"),
                                         "   load ", (f"{s.cpu:.0%}", "bold"), "   req/s ", (str(s.rps), "bold")),
                        classes="service-line")
            with Horizontal(id="service-lower"):
                with VerticalScroll(id="service-notes"):
                    yield Markdown(self.notes)
                yield DataTable(id="service-deploys", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.query_one("#service-dialog").border_title = f"{self.service.key}"
        self.query_one("#service-notes").border_title = "Notes"
        table = self.query_one("#service-deploys", DataTable)
        table.border_title = "Recent deploys"
        t = self.tones
        table.add_columns("Version", "When", "By", "Result")
        people = ("dana", "sam", "priya", "lee", "Claude Code", "alex")
        for k, version in enumerate(history(self.service.version, 8)):
            result = Text("reverted", style=t.text_error) if k == 3 else Text("shipped", style=t.text_success)
            table.add_row(Text(version, style="bold"), f"{k * 2 + 1}d ago", people[k % len(people)], result)
        table.focus()

    def on_click(self, event: Click) -> None:
        if event.widget is self:
            self.dismiss()
