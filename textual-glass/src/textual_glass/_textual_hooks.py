"""Every use of Textual's private API lives in this module.

Tested with Textual 8.2.x (the package pins ``textual>=8.2,<9``). If a Textual
upgrade breaks glass, this is the file to look at. Each hook degrades to
"do nothing" rather than raising, so a broken hook means a plainer app, not a
crash.

Hooks:

* ``App._driver`` and ``App._driver.write``: graphics go through Textual's
  single writer thread, so they never interleave with Textual's own output.
* ``App._end_update``: wrapped per instance, so graphics are written inside
  Textual's synchronized-update frame, just before ``SYNC_END``.
* ``Driver._writer_thread._queue``: its size, to avoid flooding the writer.
* ``textual._border.BORDER_CHARS`` and ``get_box``: border glyphs are made
  blank while glass is on (Textual still draws border titles).
* ``Widget.get_line_filters``: patched on the class so each widget gets its
  own glass line filter (the public ``App.get_line_filters`` cannot tell
  which widget it is filtering).
* ``Screen._compositor.visible_widgets``: what is on screen, in paint order,
  and how much of one widget shows (``visible_region``).
* ``Widget._border_title`` / ``_border_subtitle``: rendered label lengths.
* ``OptionList._lines``: which screen line shows the highlighted option.
* ``DataTable._row_label_column_width``: x offset of the first column.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any

from textual.geometry import Region
from textual.widget import Widget

if TYPE_CHECKING:
    from textual.app import App
    from textual.filter import LineFilter
    from textual.screen import Screen
    from textual.widgets import DataTable, OptionList

TESTED_TEXTUAL = ">=8.2,<9"


# -- output -------------------------------------------------------------------


def has_driver(app: App) -> bool:
    """Is Textual's driver running (False before it starts and after it stops)?"""
    try:
        return getattr(app, "_driver", None) is not None
    except Exception:  # noqa: BLE001
        return False


def driver_write(app: App, data: str) -> bool:
    """Queue ``data`` on Textual's writer thread. False if there is no driver."""
    driver = getattr(app, "_driver", None)
    if driver is None or not data:
        return False
    try:
        driver.write(data)
    except Exception:  # noqa: BLE001 - the driver may be shutting down
        return False
    return True


def writer_backlog(app: App) -> int:
    """How many writes are waiting in Textual's writer thread (0 if unknown)."""
    thread = getattr(getattr(app, "_driver", None), "_writer_thread", None)
    queue = getattr(thread, "_queue", None)
    try:
        return int(queue.qsize()) if queue is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def wrap_end_update(app: App, before: Callable[[], None]) -> Callable[[], None]:
    """Call ``before()`` at the end of every Textual frame, before ``SYNC_END``.

    Returns a function that removes the wrapper. Without the private method
    nothing is wrapped: glass then writes in its own synchronized updates only.
    """
    original = getattr(app, "_end_update", None)  # bound method
    if original is None:
        return lambda: None

    def _end_update() -> None:
        try:
            before()
        finally:
            original()

    app._end_update = _end_update  # type: ignore[method-assign]

    def unwrap() -> None:
        try:
            if app.__dict__.get("_end_update") is _end_update:
                del app.__dict__["_end_update"]
        except Exception:  # noqa: BLE001
            pass

    return unwrap


# -- border glyphs ---------------------------------------------------------------


class BorderGlyphs:
    """Blank the glyphs of some border types for the whole process, reversibly."""

    def __init__(self) -> None:
        self._saved: dict[str, Any] = {}

    @property
    def active(self) -> bool:
        return bool(self._saved)

    def blank(self, types: Iterable[str]) -> None:
        """Without the private table, borders keep their glyphs (drawn over the glass)."""
        try:
            from textual import _border

            blank_row = (" ", " ", " ")
            for name in types:
                if name in _border.BORDER_CHARS and name not in self._saved:
                    self._saved[name] = _border.BORDER_CHARS[name]
                    _border.BORDER_CHARS[name] = (blank_row, blank_row, blank_row)
            _border.get_box.cache_clear()
        except Exception:  # noqa: BLE001
            return

    def restore(self) -> None:
        if not self._saved:
            return
        saved = dict(self._saved)
        self._saved.clear()
        try:
            from textual import _border

            _border.BORDER_CHARS.update(saved)  # type: ignore[arg-type]
            _border.get_box.cache_clear()
        except Exception:  # noqa: BLE001
            return


# -- per-widget line filters ---------------------------------------------------------

FilterProvider = Callable[[Widget], "LineFilter | None"]
_providers: weakref.WeakKeyDictionary[Any, FilterProvider] = weakref.WeakKeyDictionary()
_original_get_line_filters: Callable[[Widget], Any] | None = None


def _get_line_filters(self: Widget) -> Sequence[LineFilter]:
    assert _original_get_line_filters is not None
    filters = _original_get_line_filters(self)
    try:
        provider = _providers.get(self.app)
        extra = provider(self) if provider is not None else None
    except Exception:  # noqa: BLE001 - no active app, or a broken provider
        return filters
    return [*filters, extra] if extra is not None else filters


def add_filter_provider(app: App, provider: FilterProvider) -> None:
    """Give every widget of ``app`` the filter ``provider(widget)`` (last in the chain).

    Without ``Widget.get_line_filters`` there is no filter: cells keep their
    backgrounds and the glass only shows where Textual leaves the default."""
    global _original_get_line_filters
    original = getattr(Widget, "get_line_filters", None)
    if original is None:
        return
    _providers[app] = provider
    if _original_get_line_filters is None:
        _original_get_line_filters = original
        Widget.get_line_filters = _get_line_filters  # type: ignore[method-assign]


def remove_filter_provider(app: App) -> None:
    global _original_get_line_filters
    _providers.pop(app, None)
    if not _providers and _original_get_line_filters is not None:
        Widget.get_line_filters = _original_get_line_filters  # type: ignore[method-assign,assignment]
        _original_get_line_filters = None


# -- geometry -------------------------------------------------------------------------


def visible_widgets(screen: Screen) -> list[tuple[Widget, Region]]:
    """Widgets on screen in paint order (bottom first), with their clipped regions."""
    mapping = None
    compositor = getattr(screen, "_compositor", None)
    if compositor is not None:
        try:
            mapping = compositor.visible_widgets  # topmost first
        except Exception:  # noqa: BLE001
            mapping = None
    if mapping is None:
        return [(w, w.region) for w in screen.walk_children(Widget) if w.region]
    out = []
    for widget, (region, clip) in reversed(list(mapping.items())):
        visible = region.intersection(clip)
        if visible:
            out.append((widget, visible))
    return out


def visible_region(widget: Widget) -> Region:
    """The part of ``widget`` that is on screen (its whole region if that can't be told)."""
    try:
        region, clip = widget.screen._compositor.visible_widgets[widget]
        return region.intersection(clip)
    except Exception:  # noqa: BLE001 - not on screen, or no compositor
        return widget.region


def border_label_length(widget: Widget, title: bool = True) -> int:
    """Cell length of the border title (or subtitle) as Textual renders it."""
    content = getattr(widget, "_border_title" if title else "_border_subtitle", None)
    if content is not None:
        try:
            return int(content.cell_length)
        except Exception:  # noqa: BLE001
            pass
    text = widget.border_title if title else widget.border_subtitle
    if not text:
        return 0
    from rich.text import Text

    return Text.from_markup(str(text)).cell_len


def option_lines(option_list: OptionList) -> list[int]:
    """For each rendered line of an OptionList, the index of its option."""
    try:
        return [index for index, _ in option_list._lines]
    except Exception:  # noqa: BLE001
        return []


def row_label_width(table: DataTable) -> int:
    try:
        return int(table._row_label_column_width)
    except Exception:  # noqa: BLE001
        return 0
