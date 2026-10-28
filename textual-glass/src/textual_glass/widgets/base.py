"""Building blocks for widgets with pixels: text strips, a tween,
:class:`PixelWidget` and :class:`SelectList`, a keyboard-driven list with a
sliding selection pill.

Text is drawn in ``render_line`` from *parts*, ``(text, rich_style)`` pairs,
with :func:`strip`. Parts hold single-width characters only (``strip`` counts
characters, not cells), apart from :func:`placeholder_parts`, which ``strip``
measures by counting placeholders.
"""

from __future__ import annotations

import time
import weakref
from collections.abc import Iterable, Sequence
from dataclasses import replace
from functools import lru_cache
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar, cast

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from .. import _textual_hooks as hooks
from .. import kitty
from ..items import Pill
from ..palette import Palette, hex_of, mix
from ..render import Colour, FRect, Item
from ..scene import PixelContext, PixelMixin

if TYPE_CHECKING:
    from textual.app import App

Part = tuple[str, str]  # (text, Rich style), e.g. ("12", "bold #38e1ff")

#: How far text fades towards the background (:func:`tone`): soft, dim and
#: faint text; ``BRIGHT`` goes the other way, past the text colour.
SOFT, DIM, FAINT, BRIGHT = 0.18, 0.42, 0.6, -0.45


@lru_cache(maxsize=1024)
def style(spec: str) -> Style:
    """A Rich style from its text form, cached."""
    return Style.parse(spec) if spec else Style()


def hex_colour(c: Colour) -> str:
    """A colour (``"#rrggbb"`` or an RGB tuple) as ``"#rrggbb"``, for text styles."""
    return c if isinstance(c, str) else hex_of(c)


def tone(palette: Palette, fade: float = 0.0) -> str:
    """The theme's text colour ``fade`` of the way to its background (negative:
    brighter than the text), as ``"#rrggbb"``."""
    if fade >= 0:
        return hex_of(mix(palette.foreground, palette.background, fade))
    far = (255, 255, 255) if palette.dark else (0, 0, 0)
    return hex_of(mix(palette.foreground, far, -fade))


def ellipsize(text: str, width: int) -> str:
    """``text`` cut to ``width`` characters, ending in an ellipsis when cut."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def strip(parts: Iterable[Part], width: int, bg: str | None = None) -> Strip:
    """A line of styled text parts, cropped or padded to exactly ``width`` cells.

    A placeholder run (:func:`placeholder_parts`) is one cell per placeholder;
    it is drawn whole or not at all, never cut (a cut run would show the wrong
    columns of its image). ``bg`` is a background for the whole line.
    """
    segments: list[Segment] = []
    used = 0
    base = Style(bgcolor=bg) if bg else None
    for text, spec in parts:
        if used >= width or not text:
            continue
        if text[0] == kitty.PLACEHOLDER:
            cells = text.count(kitty.PLACEHOLDER)  # one cell each; the diacritics take no space
            if used + cells > width:
                continue
        else:
            text = text[: width - used]
            cells = len(text)
        st = style(spec)
        segments.append(Segment(text, base + st if base is not None else st))
        used += cells
    if used < width:
        segments.append(Segment(" " * (width - used), base))
    return Strip(segments, width)


def fit(parts: Sequence[Part], width: int, flex: int) -> list[Part]:
    """Shrink (with an ellipsis) or pad part ``flex`` so all ``parts`` fill
    exactly ``width`` cells; parts after it end up right-aligned."""
    out = list(parts)
    fixed = sum(len(t) for i, (t, _) in enumerate(out) if i != flex)
    room = max(0, width - fixed)
    text, spec = out[flex]
    out[flex] = (ellipsize(text, room).ljust(room), spec)
    return out


def placeholder_parts(image_id: int, row: int, cols: int) -> Part:
    """Row ``row`` of a terminal-held image ``cols`` cells wide (the id is in the
    foreground colour, which nothing may change)."""
    return (kitty.placeholder_row(row, cols), kitty.id_color(image_id))


def text_meter(fraction: float, width: int) -> str:
    """A fraction as a row of filled and empty blocks (the text form of a meter)."""
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return "▰" * filled + "▱" * (width - filled)


class Tween:
    """A value that eases to a target over ``duration`` seconds."""

    def __init__(self, value: float, duration: float = 0.14) -> None:
        self.a = self.b = value
        self.t0 = 0.0
        self.duration = duration

    def set(self, value: float, now: float | None = None, animate: bool = True) -> None:
        t = time.monotonic() if now is None else now
        if not animate:
            self.a = self.b = value
            self.t0 = 0.0
            return
        if value == self.b:
            return
        self.a, self.b, self.t0 = self.value(t), value, t

    def value(self, now: float) -> float:
        k = min(1.0, max(0.0, (now - self.t0) / self.duration))
        k = 1 - (1 - k) ** 3
        return self.a + (self.b - self.a) * k

    def busy(self, now: float) -> bool:
        return now - self.t0 < self.duration and self.a != self.b


_PALETTES: weakref.WeakKeyDictionary[App[Any], tuple[tuple[Any, ...], Palette]] = weakref.WeakKeyDictionary()


def _intersect(a: FRect, b: FRect | None) -> FRect:
    if b is None:
        return a
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


class PixelWidget(PixelMixin, Widget):
    """Base for widgets with pixels: a transparent background (the glass shows
    through), the theme's colours for text, and the visible part of the widget
    for clipping items.

    Subclasses draw text in ``render_line`` (always a sensible fallback, with
    the same layout in both modes: :attr:`glass_on` says which is current) and
    yield items from ``pixel_items(ctx)`` in screen cells. Dimming under popups
    is done by glass's line filter; nothing here needs to know about popups.
    """

    DEFAULT_CSS = """
    PixelWidget { background: transparent; }
    """

    @property
    def theme_palette(self) -> Palette:
        """The app's theme colours (RGB), for text styles in ``render_line``.

        Read from the theme, cached per app and theme. In ``pixel_items`` use
        ``ctx.palette``: it holds the same theme colours.
        """
        app = self.app
        key = (app.theme, id(app.current_theme), app.ansi_color)  # what the engine keys its palette on
        found = _PALETTES.get(app)
        if found is None or found[0] != key:
            found = _PALETTES[app] = (key, Palette.from_app(app))
        return found[1]

    def visible_clip(self) -> FRect | None:
        """The part of the widget on screen as a ``clip=`` rectangle for items,
        or None when all of it shows. A widget in a scrolling container passes
        it so its pixels are cut where its text is."""
        region = self.region
        visible = hooks.visible_region(self)
        if visible == region:
            return None
        return (float(visible.x), float(visible.y), float(visible.width), float(visible.height))


T = TypeVar("T")


class SelectList(PixelWidget, Generic[T], can_focus=True):
    """A list of rows with a keyboard cursor and a glowing pill that slides
    between rows (a highlighted background without glass).

    Subclasses implement :meth:`render_row` (text) and may add
    :meth:`row_pixels` (pixel items per row). Rows for which
    :meth:`selectable` is False (section headers) are skipped by the cursor,
    and scroll into view with the row under them. The list pages by itself
    (it is not a scrolling container), so it takes ``height: 1fr`` by default.

    Args:
        accent: Colour of the pill (default: the theme's primary colour);
            :meth:`pill_color` may pick one per row instead.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up,k", "move(-1)", "up", show=False),
        Binding("down,j", "move(1)", "down", show=False),
        Binding("home,g", "first", show=False),
        Binding("end,G", "last", show=False),
        Binding("pageup", "page(-1)", show=False),
        Binding("pagedown", "page(1)", show=False),
        Binding("enter", "choose", "open", show=False),
    ]

    DEFAULT_CSS = """
    SelectList { height: 1fr; }
    """

    class Highlighted(Message):
        """The cursor moved to ``row``."""

        def __init__(self, sender: SelectList[Any], row: Any) -> None:
            super().__init__()
            self.list = sender
            self.row = row

        @property
        def control(self) -> SelectList[Any]:
            return self.list

    class Selected(Message):
        """Enter (or a click on the highlighted row) chose ``row``."""

        def __init__(self, sender: SelectList[Any], row: Any) -> None:
            super().__init__()
            self.list = sender
            self.row = row

        @property
        def control(self) -> SelectList[Any]:
            return self.list

    #: Lines per row (2 shows a second, dimmer detail line). May be set per instance.
    row_lines: int = 1

    def __init__(self, *, accent: Colour | None = None, name: str | None = None, id: str | None = None,
                 classes: str | None = None, disabled: bool = False) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self.rows: list[T] = []
        self.index = 0
        self.scroll = 0  # the first row shown
        self.accent = accent
        self._pill = Tween(0.0)

    # -- data --------------------------------------------------------------------

    def row_key(self, row: T) -> str:
        """A stable key per row (``row.key``, ``row.id`` or the row itself): the
        cursor stays on the same key when the rows are replaced."""
        return str(getattr(row, "key", getattr(row, "id", row)))

    def selectable(self, row: T) -> bool:
        return True

    def set_rows(self, rows: Sequence[T]) -> None:
        """Replace the rows, keeping the cursor on the same row (by key) if it is still there."""
        current = self.highlighted
        old_key = self.row_key(current) if current is not None else None
        self.rows = list(rows)
        index = next((i for i, r in enumerate(self.rows) if old_key is not None and self.row_key(r) == old_key), None)
        if index is None:
            index = min(self.index, max(0, len(self.rows) - 1))
        self.index = self._nearest_selectable(index, 1)
        self._pill.set(float(self.index), animate=False)
        self._keep_visible()
        self.refresh()
        if self.highlighted is not None and (old_key is None or self.row_key(self.highlighted) != old_key):
            self.post_message(self.Highlighted(self, self.highlighted))

    @property
    def highlighted(self) -> T | None:
        if 0 <= self.index < len(self.rows) and self.selectable(self.rows[self.index]):
            return self.rows[self.index]
        return None

    def _nearest_selectable(self, index: int, step: int) -> int:
        if not self.rows:
            return 0
        index = max(0, min(index, len(self.rows) - 1))
        for direction in (step, -step):
            i = index
            while 0 <= i < len(self.rows):
                if self.selectable(self.rows[i]):
                    return i
                i += direction
        return index

    # -- geometry ---------------------------------------------------------------------

    @property
    def lines_per_row(self) -> int:
        return max(1, self.row_lines)

    @property
    def visible_rows(self) -> int:
        return max(1, self.size.height // self.lines_per_row)

    def _keep_visible(self) -> None:
        """Scroll as little as possible to show the cursor, with the headers right above it."""
        if not self.size.height:
            return  # no size yet (or a hidden page): scroll once there is one (on_resize)
        visible = self.visible_rows
        top = self.index  # the cursor never lands on a header, so scrolling up takes them along
        while top > 0 and self.index - top + 1 < visible and not self.selectable(self.rows[top - 1]):
            top -= 1
        if top < self.scroll:
            self.scroll = top
        elif self.index >= self.scroll + visible:
            self.scroll = self.index - visible + 1
        self.scroll = max(0, min(self.scroll, max(0, len(self.rows) - visible)))

    def on_resize(self, event: events.Resize) -> None:
        self._keep_visible()
        self._pill.set(float(self.index), animate=False)

    # -- actions --------------------------------------------------------------------------

    def select_index(self, index: int, animate: bool = True) -> None:
        if not self.rows:
            return
        step = 1 if index >= self.index else -1
        index = self._nearest_selectable(index, step)
        if index == self.index:
            return
        self.index = index
        old_scroll = self.scroll
        self._keep_visible()
        self._pill.set(float(index), animate=animate and old_scroll == self.scroll)
        self.refresh()
        if self.highlighted is not None:
            self.post_message(self.Highlighted(self, self.highlighted))

    def select_key(self, key: str) -> bool:
        for i, row in enumerate(self.rows):
            if self.row_key(row) == key and self.selectable(row):
                self.select_index(i, animate=False)
                return True
        return False

    def action_move(self, step: int) -> None:
        i = self.index + step
        while 0 <= i < len(self.rows) and not self.selectable(self.rows[i]):
            i += step
        if 0 <= i < len(self.rows):
            self.select_index(i)

    def action_first(self) -> None:
        self.select_index(0)

    def action_last(self) -> None:
        self.select_index(len(self.rows) - 1)

    def action_page(self, step: int) -> None:
        self.select_index(self.index + step * self.visible_rows, animate=False)

    def action_choose(self) -> None:
        if self.highlighted is not None:
            self.post_message(self.Selected(self, self.highlighted))

    def on_click(self, event: events.Click) -> None:
        index = self.scroll + event.y // self.lines_per_row
        if 0 <= index < len(self.rows) and self.selectable(self.rows[index]):
            if index == self.index:
                self.action_choose()
            else:
                self.select_index(index)

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    # -- rendering ----------------------------------------------------------------------------

    def render_row(self, row: T, line: int, width: int, selected: bool) -> list[Part]:
        """The text parts of line ``line`` (0, or 1 with ``row_lines = 2``) of ``row``."""
        if line:
            return []
        pal = self.theme_palette
        return [(str(row), f"bold {tone(pal, BRIGHT)}" if selected else tone(pal))]

    def row_pixels(self, row: T, rect: FRect, selected: bool, ctx: PixelContext) -> Iterable[Item]:
        """Pixel items for a row (``rect`` is its screen cells). Give each a stable
        ``ident`` that includes the row's key."""
        return ()

    def pill_color(self, row: T) -> Colour | None:
        """The pill's colour on ``row`` (None: ``accent``, or the theme's primary colour)."""
        return self.accent

    def empty_text(self) -> str:
        return "nothing here"

    def selected_background(self) -> str:
        """The selected row's background without glass (with glass the pill shows it)."""
        pal = self.theme_palette
        return hex_of(mix(pal.background, pal.primary, 0.3 if self.has_focus else 0.15))

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if not self.rows:
            if y == 0:
                return strip([("  " + self.empty_text(), tone(self.theme_palette, FAINT))], width)
            return Strip.blank(width)
        per = self.lines_per_row
        index = self.scroll + y // per
        if index >= len(self.rows):
            return Strip.blank(width)
        row = self.rows[index]
        selected = index == self.index and self.selectable(row)
        bg = self.selected_background() if selected and not self.glass_on else None
        return strip(self.render_row(row, y % per, width, selected), width, bg)

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        if not self.rows:
            return ()
        region = self.content_region
        per = self.lines_per_row
        visible = self.visible_clip()
        items: list[Item] = []
        for i in range(self.scroll, min(len(self.rows), self.scroll + self.visible_rows)):
            y = region.y + (i - self.scroll) * per
            rect = (float(region.x), float(y), float(region.width), float(per))
            for item in self.row_pixels(self.rows[i], rect, i == self.index, ctx):
                if visible is not None:  # partly scrolled out of view: cut where the text is cut
                    item = replace(cast(Any, item), clip=_intersect(item.clip or visible, visible))
                items.append(item)
        row = self.highlighted
        if row is not None:
            if not ctx.animate:
                self._pill.set(float(self.index), animate=False)
            pos = self._pill.value(ctx.now)
            if self._pill.busy(ctx.now):
                ctx.animating()
            top = region.y + (pos - self.scroll) * per
            if region.y - 0.5 <= top <= region.bottom - per + 0.5:
                # the pill reaches into the padding; its glow is cut at the list's top and bottom
                clip = _intersect((region.x - 2.0, float(region.y), region.width + 4.0, float(region.height)), visible)
                items.append(Pill((region.x - 1.0, top, region.width + 1.6, float(per)),
                                  self.pill_color(row) or ctx.palette.primary, focused=ctx.focused is self,
                                  ident=f"pill:{id(self)}", clip=clip))
        return items
