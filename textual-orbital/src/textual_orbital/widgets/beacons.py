"""``BeaconRow``: one beacon per :class:`~textual_orbital.model.Entity`, most urgent first.

A beacon is a small animation the terminal plays by itself (uploaded once,
shared by every beacon in the same state), with the entity's name and its
state next to it:

=========  ===========================================  =============
state      beacon                                       text
=========  ===========================================  =============
waiting    bright amber core, pulsing, rings pinging    WAITING 3m
error      red core with a slow throb and a cross       error · reason
working    cyan core with a satellite on a slow orbit   working 12m
idle       dim dot in a faint ring, still               idle 1h
done       green dot, still                             done 5m
=========  ===========================================  =============

Without glass the beacon is a coloured glyph (◉ ✕ ◌ ○ ✓). Arrows (or j/k)
move the selection; Enter posts :class:`BeaconRow.Selected`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.strip import Strip

from textual_glass import PixelContext
from textual_glass.items import Pill
from textual_glass.palette import hex_of, mix
from textual_glass.render import Item
from textual_glass.sprites import beacon_frames
from textual_glass.widgets import Part, PixelWidget, Tween, placeholder_parts, strip

from ..model import ENTITY_URGENCY, Entity, EntityState
from ..theme import PALETTE, ellipsize, fmt_age, now

STATE_COLORS = {
    EntityState.WAITING: PALETTE["amber"],
    EntityState.ERROR: PALETTE["red"],
    EntityState.WORKING: PALETTE["accent"],
    EntityState.IDLE: PALETTE["slate"],
    EntityState.DONE: PALETTE["green"],
}
STATE_GLYPHS = {
    EntityState.WAITING: "◉",
    EntityState.ERROR: "✕",
    EntityState.WORKING: "◌",
    EntityState.IDLE: "○",
    EntityState.DONE: "✓",
}


def order_entities(entities: Iterable[Entity]) -> list[Entity]:
    """Waiting first (longest wait first), then errors, working, idle, done."""
    def key(e: Entity) -> tuple[int, float]:
        since = e.since.timestamp() if e.since else 0.0
        return (ENTITY_URGENCY.get(e.state, 9), since)
    return sorted(entities, key=key)


def fit_parts(parts: list[Part], room: int) -> list[Part]:
    """The first part (ellipsized to ``room``), then each following part only if it fits whole."""
    out: list[Part] = []
    used = 0
    for i, (text, spec) in enumerate(parts):
        if i == 0:
            text = ellipsize(text, room)
        elif used + len(text) > room:
            break
        out.append((text, spec))
        used += len(text)
    return out


def state_text(entity: Entity, at: datetime, short: bool = False) -> list[Part]:
    """The state line ("WAITING 3m · permission prompt"); ``short`` is just the
    timer in the state's colour (the beacon itself shows the state)."""
    age = fmt_age(at - entity.since) if entity.since else ""
    colour = STATE_COLORS.get(entity.state, PALETTE["text_dim"])
    if short:
        word = "err" if entity.state is EntityState.ERROR else age
        bold = entity.state in (EntityState.WAITING, EntityState.ERROR)
        return [(word, f"bold {colour}" if bold else colour)]
    if entity.state is EntityState.WAITING:
        out: list[Part] = [(f"WAITING {age}".strip(), f"bold {colour}")]
        if entity.reason:
            out.append((f" · {entity.reason}", PALETTE["text_soft"]))
        return out
    if entity.state is EntityState.ERROR:
        return [("error" + (f" · {entity.reason}" if entity.reason else ""), colour)]
    word = entity.state.value
    return [(f"{word} {age}".strip(), colour if entity.state is EntityState.WORKING else PALETTE["text_dim"])]


class BeaconRow(PixelWidget, can_focus=True):
    """Entities as beacons. Two lines per beacon when there is room (name,
    then state), one line otherwise; idle ones fold into a "+N" slot when
    space runs out.

    Args:
        empty: What to say when there are no entities.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left,k,up", "move(-1)", show=False),
        Binding("right,j,down", "move(1)", show=False),
        Binding("enter", "choose", "open", show=False),
    ]

    DEFAULT_CSS = """
    BeaconRow { height: 2; }
    """

    class Selected(Message):
        """Enter (or a click on the selected beacon) chose ``entity``."""

        def __init__(self, entity: Entity) -> None:
            super().__init__()
            self.entity = entity

    def __init__(self, *, empty: str = "nothing running", id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.empty = empty
        self.entities: list[Entity] = []
        self.index = 0
        self._pill = Tween(0.0)
        self.at = now()

    # -- data ----------------------------------------------------------------------

    def set_entities(self, entities: Sequence[Entity], at: datetime | None = None) -> None:
        current = self.entities[self.index].id if 0 <= self.index < len(self.entities) else None
        self.entities = order_entities(entities)
        self.at = at or now()
        ids = [e.id for e in self.entities]
        self.index = ids.index(current) if current in ids else 0
        self._pill.set(float(self.index), animate=False)
        self.refresh()

    def tick(self, at: datetime | None = None) -> None:
        """Time passed (ages changed): re-render the text."""
        self.at = at or now()
        self.refresh()

    @property
    def highlighted(self) -> Entity | None:
        return self.entities[self.index] if 0 <= self.index < len(self.entities) else None

    # -- layout --------------------------------------------------------------------------

    @property
    def two_line(self) -> bool:
        return self.size.height >= 2

    @property
    def sprite_size(self) -> tuple[int, int]:
        return (4, 2) if self.two_line else (2, 1)

    def slots(self) -> tuple[int, int, int]:
        """(slot width, beacons with a full slot, width of the "+n" slot for the rest)."""
        width = max(1, self.size.width)
        n = max(1, len(self.entities))
        minimum = 19 if self.two_line else 17
        if n * minimum <= width:
            return min(30, width // n), n, 0
        rest_w = 9 if self.two_line else 10
        count = max(1, (width - rest_w) // minimum)
        slot = min(30, (width - rest_w) // count)
        return slot, count, width - slot * count

    # -- actions ----------------------------------------------------------------------------

    def action_move(self, step: int) -> None:
        if not self.entities:
            return
        _, count, _ = self.slots()
        limit = min(len(self.entities), count) - 1
        index = max(0, min(limit, self.index + step))
        if index != self.index:
            self.index = index
            self._pill.set(float(index))
            self.refresh()

    def action_choose(self) -> None:
        if self.highlighted is not None:
            self.post_message(self.Selected(self.highlighted))

    def on_click(self, event: events.Click) -> None:
        slot, count, _ = self.slots()
        index = event.x // max(1, slot)
        if 0 <= index < min(count, len(self.entities)):
            if index == self.index:
                self.action_choose()
            self.index = index
            self._pill.set(float(index))
            self.refresh()

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    # -- rendering ------------------------------------------------------------------------------

    def _sprite(self, entity: Entity) -> int | None:
        engine = self.glass
        if engine is None or not engine.active:
            return None
        cols, rows = self.sprite_size
        colour = STATE_COLORS.get(entity.state, PALETTE["text_dim"])
        state = entity.state.value
        return engine.sprite(("beacon", state, colour), cols, rows,
                             lambda w, h: beacon_frames(w, h, state, colour), waiter=self)

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if not self.entities:
            return strip([("  " + self.empty if y == 0 else "", PALETTE["text_faint"])], width)
        slot, count, rest_w = self.slots()
        cols, rows = self.sprite_size
        parts: list[Part] = []
        selected_bg = None if self.glass_on else self.selected_background()
        for i, entity in enumerate(self.entities[:count]):
            parts.extend(self._sprite_parts(entity, y, cols, rows))
            room = slot - cols - 2
            if self.two_line:
                if y == 0:
                    name_style = f"bold {PALETTE['text_bright']}" if entity.state is EntityState.WAITING \
                        else f"bold {PALETTE['text']}"
                    line: list[Part] = [(entity.name, name_style)]
                else:
                    line = state_text(entity, self.at)
            else:
                st = state_text(entity, self.at, short=True)
                name_style = f"bold {STATE_COLORS[EntityState.WAITING]}" if entity.state is EntityState.WAITING \
                    else f"bold {PALETTE['text']}"
                line = [(ellipsize(entity.name, max(4, room - 1 - sum(len(t) for t, _ in st))), name_style),
                        (" ", ""), *st]
            body = [(" ", ""), *fit_parts(line, room)]
            body.append((" " * (slot - cols - sum(len(t) for t, _ in body)), ""))
            if i == self.index and self.has_focus and selected_bg is not None:
                body = [(t, f"{s} on {selected_bg}") for t, s in body]
            parts.extend(body)
        rest = self.entities[count:]
        if rest and rest_w:
            parts.extend(self._sprite_parts(rest[0], y, cols, rows))
            states = {e.state for e in rest}
            word = rest[0].state.value if len(states) == 1 else "more"
            if self.two_line:
                text = f" +{len(rest)}" if y == 0 else f" {word}"
            else:
                text = f" +{len(rest)} {word}"
            parts.append((text[: rest_w - cols], PALETTE["text_dim"]))
        return strip(parts, width)

    def selected_background(self) -> str:
        """The selected beacon's background without glass (with glass the pill shows it)."""
        pal = self.theme_palette
        return hex_of(mix(pal.background, pal.primary, 0.3))

    def _sprite_parts(self, entity: Entity, y: int, cols: int, rows: int) -> list[Part]:
        colour = STATE_COLORS.get(entity.state, PALETTE["text_dim"])
        if y >= rows:
            return [(" " * cols, "")]
        image_id = self._sprite(entity)
        if image_id is not None:
            return [placeholder_parts(image_id, y, cols)]
        glyph = STATE_GLYPHS.get(entity.state, "●")
        text = (" " + glyph if rows == 2 else glyph) if y == 0 else ""
        return [(text.ljust(cols)[:cols], f"bold {colour}")]

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        if not self.entities or ctx.focused is not self:
            return ()
        slot, _, _ = self.slots()
        region = self.content_region
        if not ctx.animate:
            self._pill.set(float(self.index), animate=False)
        pos = self._pill.value(ctx.now)
        if self._pill.busy(ctx.now):
            ctx.animating()
        return (Pill((region.x + pos * slot - 0.4, float(region.y), float(slot) - 0.2, float(region.height)),
                     STATE_COLORS.get(self.entities[self.index].state, PALETTE["accent"]),
                     ident=f"pill:{id(self)}", clip=self.visible_clip()),)
