"""``Track``: things moving through stages, one lane per stage; and
``TrackTable``: the same items in a table.

Each :class:`~textual_orbital.model.TrackItem` is a glowing dot in the lane of
its stage:

* **colour** is its health: red failing, magenta needs work, amber pending,
  green clear, hollow grey parked;
* **trail** (the comet tail) is how long it has been in this stage, on a log
  scale (a few hours is a stub, two weeks fills the slot);
* **pulsing** marks a stuck item (failing, or longer than ``stuck_after_days``
  in one stage). The pulse is an animation the terminal plays, so it costs
  nothing;
* dots are ordered oldest first, so the left of each lane is what has waited
  longest; the count by each lane label is coloured by its worst dot.

The caption under the lanes describes the selected item. With focus,
left/right move along a lane, up/down (or j/k) between lanes, and Enter posts
:class:`Track.Selected`. With fewer rows than stages it collapses to a one-line
summary.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any, ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.strip import Strip

from textual_glass import PixelContext
from textual_glass.items import Dot, Lane
from textual_glass.render import FRect, Item
from textual_glass.sprites import pulse_frames
from textual_glass.widgets import Part, PixelWidget, SelectList, fit, placeholder_parts, strip

from ..model import TrackHealth, TrackItem
from ..theme import PALETTE, ellipsize, fmt_age, now, source_color
from .lists import Section, age_of, section_rule

HEALTH_COLORS = {
    TrackHealth.FAIL: PALETTE["red"],
    TrackHealth.WARN: PALETTE["magenta"],
    TrackHealth.PENDING: PALETTE["amber"],
    TrackHealth.OK: PALETTE["green"],
    TrackHealth.IDLE: PALETTE["grey"],
}
HEALTH_WORDS = {
    TrackHealth.FAIL: "failing",
    TrackHealth.WARN: "needs work",
    TrackHealth.PENDING: "pending",
    TrackHealth.OK: "clear",
    TrackHealth.IDLE: "parked",
}
SEVERITY = {TrackHealth.FAIL: 4, TrackHealth.WARN: 3, TrackHealth.PENDING: 2, TrackHealth.OK: 1, TrackHealth.IDLE: 0}
#: Lane colours, first stage to the last (the last stage is always green).
STAGE_COLORS = (PALETTE["blue"], PALETTE["accent"], PALETTE["teal"], PALETTE["violet"], PALETTE["magenta"])
TWO_WEEKS_H = 24 * 14


def status_word(item: TrackItem) -> str:
    """What the item is doing: its ``detail``, or a word for its health."""
    return item.detail or HEALTH_WORDS[item.health]


def stage_colors(count: int) -> tuple[str, ...]:
    """Default lane colours for ``count`` stages."""
    if count <= 1:
        return (PALETTE["green"],)[:count]
    return (*(STAGE_COLORS[i % len(STAGE_COLORS)] for i in range(count - 1)), PALETTE["green"])


class Track(PixelWidget, can_focus=True):
    """Track items on one lane per stage.

    Args:
        stages: The stage names, first to last (an item's ``stage`` indexes them).
        stuck_after_days: An item this long in one stage pulses.
        labels: Write each item's ``label`` after its dot (needs wider slots);
            None (default) does it when the track is at least 50 cells wide.
        label_width: Characters of the label shown after a dot.
        colors: One lane colour per stage (default: blue to violet, the last green).
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "move(-1)", show=False),
        Binding("right", "move(1)", show=False),
        Binding("up,k", "lane(-1)", show=False),
        Binding("down,j", "lane(1)", show=False),
        Binding("enter", "choose", "open", show=False),
    ]

    DEFAULT_CSS = """
    Track { height: 1fr; min-height: 1; }
    """

    #: The shortest the caption's tail gets before the item's name gives up room too.
    CAPTION_TAIL_MIN: ClassVar[int] = 10

    class Selected(Message):
        """Enter (or a click on the selected dot) chose ``item``."""

        def __init__(self, item: TrackItem) -> None:
            super().__init__()
            self.item = item

    def __init__(self, stages: Sequence[str], *, stuck_after_days: float = 2.0, labels: bool | None = None,
                 label_width: int = 5, colors: Sequence[str] | None = None, id: str | None = None,
                 classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        if not stages:
            raise ValueError("a track needs at least one stage")
        self.stages = tuple(stages)
        self.stuck_after = timedelta(days=stuck_after_days)
        self.labels = labels
        self.label_width = max(1, label_width)
        self.lane_colors = tuple(colors) if colors else stage_colors(len(self.stages))
        self.items: list[TrackItem] = []
        self.lanes: list[list[TrackItem]] = [[] for _ in self.stages]
        self.selected_key: str | None = None
        self.at = now()

    # -- data ---------------------------------------------------------------------------

    def stage_of(self, item: TrackItem) -> int:
        return max(0, min(len(self.stages) - 1, item.stage))

    def set_items(self, items: Sequence[TrackItem], at: datetime | None = None) -> None:
        self.at = at or now()
        self.items = list(items)
        self.lanes = [[] for _ in self.stages]
        for item in items:
            self.lanes[self.stage_of(item)].append(item)
        for lane in self.lanes:
            lane.sort(key=lambda t: (t.stage_since or t.opened_at or self.at))
        keys = [t.key for t in self.order]
        if self.selected_key not in keys:
            urgent = sorted(self.items, key=lambda t: (-SEVERITY[t.health] - 2 * self.is_stuck(t), t.stage))
            self.selected_key = urgent[0].key if urgent else None
        self.refresh()

    def tick(self, at: datetime | None = None) -> None:
        self.at = at or now()
        self.refresh()

    @property
    def order(self) -> list[TrackItem]:
        return [t for lane in self.lanes for t in lane]

    @property
    def selected(self) -> TrackItem | None:
        return next((t for t in self.items if t.key == self.selected_key), None)

    def select_key(self, key: str) -> None:
        if any(t.key == key for t in self.items) and key != self.selected_key:
            self.selected_key = key
            self.refresh()

    def is_stuck(self, item: TrackItem) -> bool:
        return item.stuck(self.at, self.stuck_after)

    def age_hours(self, item: TrackItem) -> float:
        since = item.stage_since or item.opened_at or self.at
        return max(0.0, (self.at - since).total_seconds() / 3600)

    # -- layout ------------------------------------------------------------------------------

    @property
    def summary_mode(self) -> bool:
        return self.size.height < len(self.stages)

    def geometry(self) -> tuple[list[int], int, int, int, int]:
        """(lane rows, label width, track start column, slot pitch, caption row or -1)."""
        h, w = self.size.height, self.size.width
        n = len(self.stages)
        caption = 2 if h >= n + 7 and w < 60 else (1 if h >= n + 1 else 0)
        gap = 2 if h - caption >= 2 * n else 1
        used = n + (n - 1) * (gap - 1)
        top = max(0, (h - caption - used) // 2) if gap == 1 else 0
        rows = [top + k * gap for k in range(n)]
        longest = max(len(s) for s in self.stages)
        label_w = longest + 3 if w >= 28 else 4
        wants_labels = self.labels if self.labels is not None else w >= 50
        pitch = self.label_width + 4 if wants_labels and w >= 50 else 4
        caption_row = h - caption if caption else -1
        return rows, label_w, label_w + 1, pitch, caption_row

    def slots(self, stage: int) -> tuple[list[TrackItem], int]:
        """The items that fit in a lane, and how many did not."""
        _, _, x0, pitch, _ = self.geometry()
        capacity = max(1, (self.size.width - x0 - 1) // pitch)
        lane = self.lanes[stage]
        if len(lane) <= capacity:
            return lane, 0
        return lane[: capacity - 1], len(lane) - capacity + 1

    def dot_x(self, slot: int) -> int:
        """Left column of a dot's two cells."""
        _, _, x0, pitch, _ = self.geometry()
        return x0 + 2 + slot * pitch

    def trail(self, item: TrackItem) -> float:
        _, _, _, pitch, _ = self.geometry()
        hours = self.age_hours(item)
        frac = math.log2(1 + hours) / math.log2(1 + TWO_WEEKS_H)
        # quantized to eighths, so time passing changes a trail only every few hours (no redraws)
        frac = round(max(0.15, min(1.0, frac)) * 8) / 8
        return frac * (min(pitch, 4) - 1.0)

    # -- actions ----------------------------------------------------------------------------------

    def _visible_order(self) -> list[TrackItem]:
        return [t for s in range(len(self.stages)) for t in self.slots(s)[0]]

    def action_move(self, step: int) -> None:
        order = self._visible_order()
        if not order:
            return
        keys = [t.key for t in order]
        i = keys.index(self.selected_key) if self.selected_key in keys else 0
        self.selected_key = keys[(i + step) % len(keys)]
        self.refresh()

    def action_lane(self, step: int) -> None:
        current = self.selected
        stage = self.stage_of(current) if current else 0
        n = len(self.stages)
        for k in range(1, n + 1):
            lane = self.slots((stage + step * k) % n)[0]
            if lane:
                self.selected_key = lane[0].key
                self.refresh()
                return

    def action_choose(self) -> None:
        if self.selected is not None:
            self.post_message(self.Selected(self.selected))

    def on_click(self, event: events.Click) -> None:
        rows, _, _, _, _ = self.geometry()
        if self.summary_mode or event.y not in rows:
            return
        for i, item in enumerate(self.slots(rows.index(event.y))[0]):
            x = self.dot_x(i)
            if x - 2 <= event.x <= x + 2:
                if item.key == self.selected_key:
                    self.action_choose()
                self.selected_key = item.key
                self.refresh()
                return

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    # -- rendering ------------------------------------------------------------------------------------

    def _pulse(self, colour: str) -> int | None:
        engine = self.glass
        if engine is None or not engine.active:
            return None
        return engine.sprite(("pulse", colour), 2, 1, lambda w, h: pulse_frames(w, h, colour), waiter=self)

    def lane_color(self, stage: int) -> str:
        lane = self.lanes[stage]
        if not lane:
            return PALETTE["text_ghost"]
        return HEALTH_COLORS[max(lane, key=lambda t: SEVERITY[t.health]).health]

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if self.summary_mode:
            return self._summary(y, width)
        rows, label_w, _, pitch, caption_row = self.geometry()
        if caption_row >= 0 and y >= caption_row:
            lines = self._caption(width, two=self.size.height - caption_row >= 2)
            return strip(lines[y - caption_row] if y - caption_row < len(lines) else [], width)
        if y not in rows:
            return Strip.blank(width)
        stage = rows.index(y)
        lane, hidden = self.slots(stage)
        count = len(self.lanes[stage])
        name = self.stages[stage]
        label = name if label_w > 4 else name[:3]
        parts: list[Part] = [
            (label.ljust(label_w - 3), f"bold {PALETTE['text_soft']}" if count else PALETTE["text_ghost"]),
            (f"{count:>2} " if count else "   ", f"bold {self.lane_color(stage)}"),
        ]
        cursor = label_w
        cells: dict[int, Part] = {}
        glass = self.glass_on
        for i, item in enumerate(lane):
            x = self.dot_x(i)
            colour = HEALTH_COLORS[item.health]
            selected = item.key == self.selected_key
            stuck = self.is_stuck(item)
            if glass:
                if stuck:
                    sprite = self._pulse(colour)
                    if sprite is not None:
                        cells[x] = placeholder_parts(sprite, 0, 2)
            else:
                trail = max(0, round(self.trail(item)) - 1)
                if trail:
                    cells[x - trail] = ("╌" * trail, colour)
                glyph = "◉" if stuck else ("○" if item.health is TrackHealth.IDLE else "●")
                st = f"bold {colour}" + (" reverse" if selected and self.has_focus else "")
                cells[x] = (glyph + ("◂" if selected else " "), st)
            label = self.dot_label(item) if pitch > 4 else ""
            if label:
                cells[x + 2] = (label[: pitch - 4], f"bold {colour}" if selected else PALETTE["text_dim"])
        if hidden:
            cells[self.dot_x(len(lane))] = (f"+{hidden}", PALETTE["text_dim"])
        for x in sorted(cells):
            if x < cursor:
                continue
            parts.append((" " * (x - cursor), ""))
            text, spec = cells[x]
            parts.append((text, spec))
            cursor = x + len(text) if not text.startswith("\U0010eeee") else x + 2
        return strip(parts, width)

    def dot_label(self, item: TrackItem) -> str:
        """The text after an item's dot when there is room (``label_width``
        characters of it show): the item's ``label``. Override it to show a
        shorter handle ("#412" for "repo#412")."""
        return item.label

    def caption_tail(self, item: TrackItem) -> str:
        """The caption's text after the item's name: its status, time in stage
        and "stuck". Keep an override short: it is cut before the name is."""
        age = fmt_age(self.at - (item.stage_since or item.opened_at or self.at))
        stuck = " · stuck" if self.is_stuck(item) else ""
        return f"{status_word(item)} · {age} in {self.stages[self.stage_of(item)].lower()}{stuck}"

    def _caption(self, width: int, two: bool = False) -> list[list[Part]]:
        """One or two lines about the selected item."""
        item = self.selected
        if item is None:
            return [[("  nothing on the track", PALETTE["text_faint"])]]
        colour = HEALTH_COLORS[item.health]
        marker = "▸ " if self.has_focus else "· "
        tail = self.caption_tail(item)
        label = f"{item.label} " if item.label else ""
        if two:
            return [[(marker, f"bold {colour}"), (label, f"bold {colour}"),
                     (ellipsize(item.title, max(6, width - len(marker) - len(label))), PALETTE["text"])],
                    [("  " + ellipsize(tail, width - 2), PALETTE["text_dim"])]]
        # The name comes first: the tail gives up its room down to CAPTION_TAIL_MIN characters, then the
        # name shrinks too (down to 6); narrower than that the tail goes.
        room = width - len(marker) - len(label)
        tail_w = min(len(tail), max(self.CAPTION_TAIL_MIN, room - len(item.title) - 3))
        if room - tail_w - 3 < 6:
            tail_w = 0
        title_w = room - tail_w - 3 if tail_w else room
        parts: list[Part] = [(marker, f"bold {colour}"), (label, f"bold {colour}"),
                             (ellipsize(item.title, max(6, title_w)), PALETTE["text"])]
        if tail_w:
            parts.append((" · " + ellipsize(tail, tail_w), PALETTE["text_dim"]))
        return [parts]

    def _summary(self, y: int, width: int) -> Strip:
        if y:
            return Strip.blank(width)
        full = sum(len(s) + 7 for s in self.stages) <= width
        parts: list[Part] = []
        for stage, name in enumerate(self.stages):
            count = len(self.lanes[stage])
            parts.append((name if full else name[:3], PALETTE["text_soft"] if count else PALETTE["text_ghost"]))
            parts.append((f" {count}", f"bold {self.lane_color(stage)}"))
            stuck = sum(1 for t in self.lanes[stage] if self.is_stuck(t))
            if stuck:
                parts.append((f" ◉{stuck}", f"bold {PALETTE['red']}"))
            if stage < len(self.stages) - 1:
                parts.append(("  ▸ " if full else " ▸ ", PALETTE["text_ghost"]))
        return strip(parts, width)

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        if self.summary_mode:
            return ()
        region = self.content_region
        rows, _, x0, _, _ = self.geometry()
        clip = self.visible_clip()
        last = len(self.stages) - 1
        items: list[Item] = []
        for stage in range(len(self.stages)):
            y = region.y + rows[stage]
            items.append(Lane((float(region.x + x0), float(y), float(region.width - x0), 1.0),
                              self.lane_colors[stage % len(self.lane_colors)], chevron=stage < last,
                              ident=f"lane:{id(self)}:{stage}", clip=clip))
            for i, item in enumerate(self.slots(stage)[0]):
                cx = region.x + self.dot_x(i) + 1.0
                items.append(Dot(cx, y + 0.5, HEALTH_COLORS[item.health], trail=self.trail(item), radius=0.27,
                                 ring=item.key == self.selected_key and ctx.focused is self,
                                 hollow=item.health is TrackHealth.IDLE, body=not self.is_stuck(item),
                                 ident=f"dot:{item.key}", clip=clip))
        return items


class TrackTable(SelectList[Any]):
    """Track items in a table: a health dot, the source, the stage, the title,
    the label, the status (``detail``) and the age. ``set_groups`` splits the
    rows into titled sections.

    Args:
        stages: The stage names (as given to :class:`Track`).
    """

    def __init__(self, stages: Sequence[str], *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.stages = tuple(stages)
        self.at = now()

    def set_items(self, items: Sequence[TrackItem], at: datetime | None = None) -> None:
        self.at = at or now()
        self.set_rows(list(items))

    def set_groups(self, groups: Sequence[tuple[str, Sequence[TrackItem]]], at: datetime | None = None) -> None:
        """Sections of rows: ``[("IN FLIGHT", items), ("SHIPPED", others)]``; empty ones are left out."""
        self.at = at or now()
        rows: list[Any] = []
        for i, (label, items) in enumerate(groups):
            if items:
                colour = PALETTE["text_dim"] if i == 0 else PALETTE["accent"]
                rows += [Section(f"group:{label}", label, len(items), color=colour), *items]
        self.set_rows(rows)

    def tick(self, at: datetime | None = None) -> None:
        self.at = at or now()
        self.refresh()

    def selectable(self, row: Any) -> bool:
        return isinstance(row, TrackItem)

    def row_key(self, row: Any) -> str:
        return str(row.key)

    def pill_color(self, row: Any) -> str:
        return HEALTH_COLORS[row.health] if isinstance(row, TrackItem) else PALETTE["accent"]

    def empty_text(self) -> str:
        return "nothing on the track"

    def render_row(self, row: Any, line: int, width: int, selected: bool) -> list[Part]:
        if isinstance(row, Section):
            return [(f"{row.label} {row.count}", f"bold {row.color}")]
        item: TrackItem = row
        colour = HEALTH_COLORS[item.health]
        dot: Part = ("   ", "") if self.glass_on else (" ○ " if item.health is TrackHealth.IDLE else " ● ", colour)
        stage = self.stages[max(0, min(len(self.stages) - 1, item.stage))] if self.stages else ""
        stage_w = max((len(s) for s in self.stages), default=0) + 1
        age = age_of(item.stage_since or item.opened_at, self.at)
        wide = width >= 96
        parts: list[Part] = [dot, (item.source.ljust(2)[:2], f"bold {source_color(item.source)}"), (" ", ""),
                             (stage.lower().ljust(stage_w), PALETTE["text_dim"]),
                             (item.title, f"bold {PALETTE['text_bright']}" if selected else PALETTE["text"])]
        label_w = 22 if wide else 9
        parts.append((f"  {ellipsize(item.label, label_w):<{label_w}}", PALETTE["text_soft"]))
        status_w = 20 if wide else 16
        parts += [(f" {ellipsize(status_word(item), status_w):<{status_w}}", colour),
                  (f" {age:>4}", PALETTE["text_soft"])]
        return fit(parts, width, 4)

    def row_pixels(self, row: Any, rect: FRect, selected: bool, ctx: PixelContext) -> Iterable[Item]:
        x, y, w, _ = rect
        if isinstance(row, Section):
            return section_rule(row, x, y, w, f"rule:track:{row.key}")
        item: TrackItem = row
        return (Dot(x + 1.5, y + 0.5, HEALTH_COLORS[item.health], radius=0.24,
                    hollow=item.health is TrackHealth.IDLE, ident=f"tdot:{item.key}"),)
