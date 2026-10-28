"""The header strip (brand, page tabs, a waiting badge, source health, clock)
and the key bar at the bottom."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from textual import events
from textual.containers import Horizontal
from textual.geometry import Size
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from textual_glass import PixelContext, PixelMixin
from textual_glass.items import HeaderBar
from textual_glass.render import Item
from textual_glass.widgets import Part, PixelWidget, strip

from ..model import Health, SourceStatus
from ..theme import PALETTE, now, source_color

HEALTH_COLORS = {
    Health.FRESH: PALETTE["green"],
    Health.STALE: PALETTE["amber"],
    Health.FAILING: PALETTE["red"],
    Health.PENDING: PALETTE["text_ghost"],
}
HEALTH_GLYPHS = {Health.FRESH: "●", Health.STALE: "●", Health.FAILING: "✕", Health.PENDING: "○"}


class SourceHealth(PixelWidget):
    """One dot per data source: green when fresh, amber when stale, a red cross
    when the last fetch failed, hollow until the first fetch. ``compact`` drops
    the source keys. A click posts :class:`SourceHealth.Clicked` (bind it to
    the sources screen for the reasons)."""

    DEFAULT_CSS = """
    SourceHealth { width: auto; height: 1; }
    """

    class Clicked(Message):
        pass

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.statuses: tuple[SourceStatus, ...] = ()
        self.health: tuple[Health, ...] = ()
        self.compact = False

    def set_statuses(self, statuses: Sequence[SourceStatus], at: datetime | None = None) -> bool:
        """Update; returns True if anything visible changed."""
        t = at or now()
        health = tuple(s.health(t) for s in statuses)
        keys = tuple(s.key for s in statuses)
        if keys == tuple(s.key for s in self.statuses) and health == self.health:
            self.statuses = tuple(statuses)
            return False
        width_changed = len(statuses) != len(self.statuses)
        self.statuses, self.health = tuple(statuses), health
        self.refresh(layout=width_changed)
        return True

    def set_compact(self, compact: bool) -> None:
        if compact != self.compact:
            self.compact = compact
            self.refresh(layout=True)

    def parts(self) -> list[Part]:
        out: list[Part] = []
        for status, health in zip(self.statuses, self.health, strict=True):
            if not self.compact:
                colour = PALETTE["red"] if health is Health.FAILING else source_color(status.key)
                out.append((status.key, PALETTE["text_faint"] if health is Health.PENDING else colour))
            out.append((HEALTH_GLYPHS[health], f"bold {HEALTH_COLORS[health]}"))
            out.append((" ", ""))
        return out[:-1]

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return sum(len(t) for t, _ in self.parts())

    def render_line(self, y: int) -> Strip:
        return strip(self.parts() if y == 0 else [], self.size.width)

    def on_click(self, event: events.Click) -> None:
        self.post_message(self.Clicked())


class Brand(PixelWidget):
    """Callsign, page tabs and an optional badge, shortened to fit."""

    DEFAULT_CSS = """
    Brand { width: 1fr; height: 1; }
    """

    def __init__(self, callsign: str = "ORBITAL", pages: Sequence[str] = (), *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.callsign = callsign
        self.pages = list(pages)
        self.current = 0
        self.badge: tuple[str, str] | None = None

    def set_page(self, index: int) -> None:
        if index != self.current:
            self.current = index
            self.refresh()

    def set_badge(self, text: str | None, color: str = PALETTE["amber"]) -> None:
        """Show ``text`` after the tabs with a ``◉`` in front (None hides it). Text that
        already starts with ``◉``, such as ``OrbitalApp.badge_changed``'s, gets no second one."""
        badge = (text if text.startswith("◉") else f"◉ {text}", color) if text else None
        if badge != self.badge:
            self.badge = badge
            self.refresh()

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if y:
            return Strip.blank(width)
        acc, faint, dim, bright = PALETTE["accent"], PALETTE["text_faint"], PALETTE["text_dim"], PALETTE["text_bright"]
        badge: list[Part] = []
        if self.badge:
            badge = [(f"  {self.badge[0]}", f"bold {self.badge[1]}")]
        badge_w = sum(len(t) for t, _ in badge)
        for variant in range(4):  # everything, short tab names, only the current tab named, no callsign
            parts: list[Part] = [("◉ ", acc)]
            if variant < 2:
                parts += [(self.callsign, f"bold {bright}"), ("  ", "")]
            for i, page in enumerate(self.pages):
                current = i == self.current
                if variant == 0 or (variant < 3 and current):
                    label = page.upper()
                elif variant == 1:
                    label = page.upper()[:4]
                else:
                    label = page.upper() if current else ""
                parts.append((f"{i + 1}", f"bold {acc}" if current else faint))
                if label:
                    parts.append((f" {label}", f"bold {bright}" if current else dim))
                parts.append(("  " if variant < 2 else " ", ""))
            if sum(len(t) for t, _ in parts) + badge_w <= width:
                return strip(parts + badge, width)
        name = self.pages[self.current].upper() if self.pages else self.callsign
        return strip([("◉ ", acc), (name, f"bold {bright}"), *badge], width)


class Clock(PixelWidget):
    """Local time at minute resolution (so it changes at most once a minute)."""

    DEFAULT_CSS = """
    Clock { width: 6; height: 1; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.text = datetime.now().strftime("%H:%M")

    def tick(self, at: datetime | None = None) -> None:
        text = datetime.now().strftime("%H:%M")
        if text != self.text:
            self.text = text
            self.refresh()

    def minute_tick(self, at: datetime) -> None:
        """``OrbitalApp`` calls this on every minute boundary, wherever the clock sits."""
        self.tick(at)

    def render_line(self, y: int) -> Strip:
        return strip([(" " + self.text, PALETTE["text"])] if y == 0 else [], self.size.width)


class HeaderStrip(PixelMixin, Horizontal):
    """Brand and tabs on the left, source health and the clock on the right,
    over a darker band with a glowing hairline.

    Args:
        callsign: The app's name, in capitals.
        pages: The page names, for the tabs (keys ``1``, ``2``...).
    """

    DEFAULT_CSS = """
    HeaderStrip { height: 1; padding: 0 1; background: transparent; }
    """

    #: Below this width the source health dots drop their keys.
    COMPACT_BELOW = 84

    def __init__(self, callsign: str = "ORBITAL", pages: Sequence[str] = (), *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.brand = Brand(callsign, pages)
        self.health = SourceHealth()
        self.clock = Clock()

    def compose(self) -> Iterable[Widget]:
        yield self.brand
        yield self.health
        yield self.clock

    def on_resize(self, event: events.Resize) -> None:
        self.health.set_compact(event.size.width < self.COMPACT_BELOW)

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        r = self.region
        yield HeaderBar((float(r.x), float(r.y), float(r.width), float(r.height)), ctx.palette.primary,
                        ctx.palette.secondary, ident=f"header:{id(self)}")


class KeyBar(PixelWidget):
    """Key hints on the left (dropped from the right as space runs out) and a
    status text on the right."""

    DEFAULT_CSS = """
    KeyBar { height: 1; padding: 0 1; dock: bottom; }
    """

    def __init__(self, keys: Sequence[tuple[str, str]] = (), *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.keys = list(keys)
        self.status: list[Part] = []

    def set_keys(self, keys: Sequence[tuple[str, str]]) -> None:
        if list(keys) != self.keys:
            self.keys = list(keys)
            self.refresh()

    def set_status(self, parts: Sequence[Part]) -> None:
        if list(parts) != self.status:
            self.status = list(parts)
            self.refresh()

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if y:
            return Strip.blank(width)
        status_w = sum(len(t) for t, _ in self.status)
        room = width - status_w - 1
        parts: list[Part] = []
        used = 0
        for key, label in self.keys:
            chunk = len(key) + 1 + len(label) + 2
            if used + chunk > room:
                break
            parts += [(key, f"bold {PALETTE['accent']}"), (f" {label}  ", PALETTE["text_dim"])]
            used += chunk
        parts.append((" " * max(0, width - used - status_w), ""))
        return strip(parts + self.status, width)
