"""Selectable lists built on :class:`textual_glass.widgets.SelectList`.

* :class:`PriorityList`: the needs-attention queue, then a foldable "be aware"
  section. Each row: a signal-strength meter for the score (coloured by
  urgency), the source badge, why, the title and the age.
* :class:`TaskBoard`: tasks grouped by list, with a meter for days in status.
* :class:`EventLog`: a timeline of the week's events on a glowing spine.
* :class:`DoneList`: recently finished tasks.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from textual.binding import Binding, BindingType

from textual_glass import PixelContext
from textual_glass.items import Dot, Meter, Rule, Signal, Spine
from textual_glass.render import FRect, Item
from textual_glass.widgets import Part, SelectList, fit

from ..model import AttentionItem, DoneTask, Event, Task
from ..theme import PALETTE, ellipsize, fmt_age, now, source_color, urgency_color, why_color


@dataclass(frozen=True)
class Section:
    """A non-selectable header row inside a list."""

    key: str
    label: str
    count: int = 0
    collapsed: bool = False
    color: str = PALETTE["text_dim"]


def section_rule(row: Section, x: float, y: float, w: float, ident: str, alpha: float = 0.3,
                 prefix: int = 0) -> tuple[Item, ...]:
    """The glowing line after a section's label (``prefix`` cells come before the label)."""
    label = prefix + len(f"{row.label} {row.count}") + 1
    return (Rule((x + label, y, w - label, 1.0), row.color, alpha=alpha, ident=ident),)


def signal_glyphs(score: float, colour: str) -> list[Part]:
    """Text fallback for the signal meter: five rising bars, two points each."""
    out: list[Part] = []
    for k, ch in enumerate("▂▃▅▆█"):
        level = score - 2 * k
        out.append((ch, colour if level >= 2 else (f"{colour} dim" if level >= 1 else PALETTE["text_ghost"])))
    return out


def age_of(since: datetime | None, at: datetime) -> str:
    return fmt_age(at - since) if since else ""


# =============================================================== priority ==


class PriorityList(SelectList[Any]):
    """The needs-attention queue plus a foldable be-aware section.

    ``set_items(attention, aware)``; ``space`` (or ``a``) folds the be-aware
    section. Posts ``Highlighted``/``Selected`` with the :class:`AttentionItem`.

    Args:
        two_line: Two lines per row (the second shows where and the detail).
            None (default): two lines when the list is at least 60 columns wide
            and either at least 30 rows tall or tall enough for every row
            (section headers included) on two lines. Override :attr:`row_lines`
            for any other rule.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        *SelectList.BINDINGS,
        Binding("space,a", "toggle_aware", "be aware", show=False),
    ]

    #: Shorter "why" labels for narrow queues (the source badge says the rest).
    SHORT_WHY: ClassVar[Mapping[str, str]] = {"claude waiting": "waiting", "claude error": "errored"}

    def __init__(self, *, two_line: bool | None = None, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.attention: list[AttentionItem] = []
        self.aware: list[AttentionItem] = []
        self.aware_collapsed = False
        self.two_line_setting = two_line
        self.at = now()

    @property
    def row_lines(self) -> int:  # type: ignore[override]
        """Lines per row, 1 or 2 (see ``two_line``)."""
        if self.two_line_setting is not None:
            return 2 if self.two_line_setting else 1
        w, h = self.size
        return 2 if w >= 60 and (h >= 30 or 2 * len(self.rows) <= h) else 1

    def set_items(self, attention: Sequence[AttentionItem], aware: Sequence[AttentionItem] = (),
                  at: datetime | None = None) -> None:
        self.attention, self.aware = list(attention), list(aware)
        self.at = at or now()
        self._rebuild()

    def tick(self, at: datetime | None = None) -> None:
        self.at = at or now()
        self.refresh()

    def _rebuild(self) -> None:
        rows: list[Any] = list(self.attention)
        if self.aware:
            top = max(i.score for i in self.aware)
            rows.append(Section("aware", "BE AWARE", len(self.aware), self.aware_collapsed, urgency_color(top)))
            if not self.aware_collapsed:
                rows.extend(self.aware)
        self.set_rows(rows)

    def action_toggle_aware(self) -> None:
        self.aware_collapsed = not self.aware_collapsed
        self._rebuild()

    def selectable(self, row: Any) -> bool:
        return isinstance(row, AttentionItem)

    def row_key(self, row: Any) -> str:
        return str(row.key)

    def pill_color(self, row: Any) -> str:
        return urgency_color(row.score) if isinstance(row, AttentionItem) else PALETTE["accent"]

    def empty_text(self) -> str:
        return "all clear: nothing needs you"

    def render_row(self, row: Any, line: int, width: int, selected: bool) -> list[Part]:
        if isinstance(row, Section):
            if line:
                return []
            label = f"{'▸ ' if row.collapsed else '▾ '}{row.label} {row.count}"
            parts: list[Part] = [(label, f"bold {PALETTE['text_dim']}")]
            if not self.glass_on:
                parts.append((" " + "─" * max(0, width - len(label) - 1), PALETTE["text_ghost"]))
            return parts
        item: AttentionItem = row
        aware = item.section != "attention"
        if line == 1:
            detail = " · ".join(x for x in (item.where, item.detail) if x)
            return [("      ", ""), (ellipsize(detail, width - 7), PALETTE["text_dim"])]
        why_w = 15 if width >= 64 else 12
        why = item.why if why_w >= 15 else self.SHORT_WHY.get(item.why, item.why)
        sig: list[Part] = [("     ", "")] if self.glass_on else signal_glyphs(item.score, urgency_color(item.score))
        title_style = PALETTE["text_bright"] if selected else (PALETTE["text_soft"] if aware else PALETTE["text"])
        if selected or item.score >= 9:
            title_style = f"bold {title_style}"
        age = age_of(item.since, self.at)
        parts = [*sig, (" ", ""), (item.source.ljust(2)[:2], f"bold {source_color(item.source)}"), (" ", ""),
                 (ellipsize(why, why_w - 1).ljust(why_w), why_color(item.why) + ("" if aware else " bold")),
                 (item.title, title_style)]
        if width >= 100 and self.lines_per_row == 1 and item.where:
            parts.append((f"  {ellipsize(item.where, 18)}", PALETTE["text_dim"]))
        parts.append((f" {age:>4}", PALETTE["text_dim"] if aware else PALETTE["text_soft"]))
        return fit(parts, width, len(sig) + 4)

    def row_pixels(self, row: Any, rect: FRect, selected: bool, ctx: PixelContext) -> Iterable[Item]:
        x, y, w, _ = rect
        if isinstance(row, Section):
            return section_rule(row, x, y, w, f"rule:{id(self)}:{row.key}", prefix=2)  # after "▾ "
        return (Signal((x, y, 5.0, 1.0), float(row.score), urgency_color(row.score), ident=f"sig:{row.key}"),)


# ================================================================== tasks ==

TASK_COLORS = {
    "in progress": PALETTE["accent"],
    "in review": PALETTE["amber"],
    "blocked": PALETTE["red"],
    "ready": PALETTE["teal"],
    "done": PALETTE["green"],
    "closed": PALETTE["slate"],
}


def task_color(status: str) -> str:
    return TASK_COLORS.get(status.lower(), PALETTE["text_soft"])


class TaskBoard(SelectList[Any]):
    """Tasks in progress grouped by list ("Projects", "Ongoing"...). The meter
    shows days in the current status (a full meter is a month)."""

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.at = now()

    def set_tasks(self, tasks: Sequence[Task], groups: Sequence[str] = (), at: datetime | None = None) -> None:
        """Group ``tasks`` by ``group``: the ``groups`` named first, in that order, then the rest by name."""
        self.at = at or now()
        order = list(groups) + sorted({t.group for t in tasks} - set(groups))
        rows: list[Any] = []
        for group in order:
            members = [t for t in tasks if t.group == group]
            if members:
                rows.append(Section(f"group:{group}", group.upper() or "OTHER", len(members), color=PALETTE["accent"]))
                rows.extend(sorted(members, key=lambda t: t.status_since or self.at))
        self.set_rows(rows)

    def tick(self, at: datetime | None = None) -> None:
        self.at = at or now()
        self.refresh()

    def selectable(self, row: Any) -> bool:
        return isinstance(row, Task)

    def row_key(self, row: Any) -> str:
        return str(row.key if isinstance(row, Section) else row.id)

    def pill_color(self, row: Any) -> str:
        return task_color(row.status) if isinstance(row, Task) else PALETTE["accent"]

    def empty_text(self) -> str:
        return "nothing in flight"

    def days(self, task: Task) -> int:
        return max(0, (self.at - task.status_since).days) if task.status_since else 0

    def render_row(self, row: Any, line: int, width: int, selected: bool) -> list[Part]:
        if isinstance(row, Section):
            return [(f"{row.label} {row.count}", f"bold {row.color}")]
        task: Task = row
        colour = task_color(task.status)
        meter_w = 8 if width >= 60 else 0
        meter: Part = (" " * meter_w, "")
        if meter_w and not self.glass_on:
            filled = max(1, round(min(1.0, self.days(task) / 30) * (meter_w - 1)))
            meter = ("▰" * filled + "▱" * (meter_w - 1 - filled) + " ", colour)
        parts: list[Part] = [("  " if self.glass_on else "● ", colour),
                             (task.title, f"bold {PALETTE['text_bright']}" if selected else PALETTE["text"])]
        if width >= 56:
            parts.append((f" {ellipsize(task.assignee, 7):<7}", PALETTE["text_dim"]))
        parts += [(f" {task.status:<11}", colour), (" ", ""), meter, (f"{self.days(task):>3}d", PALETTE["text_soft"])]
        return fit(parts, width, 1)

    def row_pixels(self, row: Any, rect: FRect, selected: bool, ctx: PixelContext) -> Iterable[Item]:
        x, y, w, _ = rect
        if isinstance(row, Section):
            return section_rule(row, x, y, w, f"rule:task:{row.key}", alpha=0.25)
        task: Task = row
        colour = task_color(task.status)
        items: list[Item] = [Dot(x + 0.6, y + 0.5, colour, radius=0.2, ident=f"taskdot:{task.id}")]
        if w >= 60:
            items.append(Meter((x + w - 12.0, y, 7.0, 1.0), min(1.0, self.days(task) / 30), colour, colour,
                               ident=f"taskmeter:{task.id}"))
        return items


# ================================================================== events ==

#: Dot colours for event kinds no ``colors`` entry names (picked by the kind's name).
EVENT_PALETTE = (PALETTE["violet"], PALETTE["amber"], PALETTE["accent"], PALETTE["green"], PALETTE["teal"],
                 PALETTE["blue"], PALETTE["magenta"], PALETTE["orange"])


class EventLog(SelectList[Event]):
    """Events as a timeline: a glowing spine with a dot per event, coloured
    by kind, newest at the top.

    Args:
        colors: A colour per event kind; other kinds get a stable colour of their own.
    """

    def __init__(self, *, colors: Mapping[str, str] | None = None, id: str | None = None,
                 classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.kind_colors = dict(colors or {})

    def kind_color(self, kind: str) -> str:
        found = self.kind_colors.get(kind)
        return found if found else EVENT_PALETTE[zlib.crc32(kind.encode()) % len(EVENT_PALETTE)]

    def row_key(self, row: Event) -> str:
        return f"{row.when.isoformat()}:{row.kind}:{row.title}"

    def pill_color(self, row: Event) -> str:
        return self.kind_color(row.kind)

    def empty_text(self) -> str:
        return "a quiet week so far"

    def render_row(self, row: Event, line: int, width: int, selected: bool) -> list[Part]:
        colour = self.kind_color(row.kind)
        when = row.when.astimezone().strftime("%a %H:%M")
        dot: Part = ("   ", "") if self.glass_on else (" ● ", colour)
        parts: list[Part] = [(when, PALETTE["text_soft"]), dot, (f"{ellipsize(row.kind, 13):<14}", colour),
                             (row.source.ljust(3)[:3], f"bold {source_color(row.source)}"),
                             (row.title, f"bold {PALETTE['text_bright']}" if selected else PALETTE["text"])]
        if width >= 70 and row.where:
            parts.append((f"  {ellipsize(row.where, 18):<18}", PALETTE["text_dim"]))
        return fit(parts, width, 4)

    def row_pixels(self, row: Event, rect: FRect, selected: bool, ctx: PixelContext) -> Iterable[Item]:
        x, y, _, _ = rect
        return (Dot(x + 10.5, y + 0.5, self.kind_color(row.kind), radius=0.22, ident=f"ev:{self.row_key(row)}"),)

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        items = list(super().pixel_items(ctx))
        if self.rows:
            r = self.content_region
            shown = min(len(self.rows) - self.scroll, self.visible_rows)
            more = 1.5 if shown == self.visible_rows else 0.0  # the spine runs on past the last row shown
            items.insert(0, Spine(r.x + 10.5, r.y + 0.5, r.y + shown - 0.5 + more, ctx.palette.primary,
                                  ctx.palette.secondary, ident=f"spine:{id(self)}", clip=self.visible_clip()))
        return items


class DoneList(SelectList[DoneTask]):
    """Recently finished tasks, newest first."""

    def row_key(self, row: DoneTask) -> str:
        return row.id

    def pill_color(self, row: DoneTask) -> str:
        return task_color(row.status)

    def empty_text(self) -> str:
        return "nothing finished this week yet"

    def render_row(self, row: DoneTask, line: int, width: int, selected: bool) -> list[Part]:
        when = row.done_at.astimezone()
        colour = task_color(row.status)
        return fit([(f"{when:%b} {when.day:<2} ", PALETTE["text_soft"]), ("✓ ", colour),
                    (row.title, f"bold {PALETTE['text_bright']}" if selected else PALETTE["text"]),
                    (f" {row.status}", colour)], width, 2)
