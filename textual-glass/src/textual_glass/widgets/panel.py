"""``GlassPanel``: a titled container with a rounded border.

It is a stock bordered ``Vertical``, so glass draws it like any other bordered
widget: a glass panel with a gradient border in the border's colours, the
title in a notch cut into the top line, and a glow while focus is inside.
Without glass it is a plain rounded Textual border with the title in it.
Either way the layout is identical.

.. code-block:: python

    with GlassPanel(title="QUEUE", id="queue"):
        yield MyList()

    panel.set_title("QUEUE", ("8", "bold #ff8a3d"), ("2 stale", "#8a93a6"))   # extras show while they fit
    panel.set_accent("#ff8a3d", "#ff4d6d")    # border colours, e.g. to match the most urgent row
"""

from __future__ import annotations

from typing import Any

from textual import events
from textual.color import Color
from textual.containers import Vertical
from textual.markup import escape
from textual.widget import Widget

from ..render import Colour
from .base import Part, hex_colour

_SIDES = ("border_top", "border_right", "border_bottom", "border_left")


class GlassPanel(Vertical):
    """A titled container: a glass panel with glass, a rounded border without.

    Args:
        title: The title on the top line (plain text: brackets are shown as typed).
        accent, accent_2: Border colours (see :meth:`set_accent`); default the
            theme's ``$primary`` from the CSS.
        dialog: A popup's panel: adds the ``-dialog`` class (an opaque
            ``$panel`` background without glass). Glass draws every top-level
            panel of a popup as a dialog anyway.
    """

    DEFAULT_CSS = """
    GlassPanel {
        height: auto;
        padding: 0 1;
        border: round $primary;
        border-title-color: $foreground;
        border-title-style: bold;
    }
    GlassPanel.-dialog { background: $panel; }
    """

    def __init__(self, *children: Widget, title: str = "", accent: Colour | None = None,
                 accent_2: Colour | None = None, dialog: bool = False, name: str | None = None,
                 id: str | None = None, classes: str | None = None, disabled: bool = False) -> None:
        super().__init__(*children, name=name, id=id, classes=classes, disabled=disabled)
        self._title_parts: tuple[str, tuple[Part, ...]] = (title, ())
        self._title_markup: str | None = None
        self._accent: tuple[str, str] | None = None
        if dialog:
            self.add_class("-dialog")
        if accent is not None:
            self.set_accent(accent, accent_2)
        self._fit_title()

    def set_title(self, name: str, *extras: Part) -> None:
        """Set the title: ``name`` always shows (Textual ellipsizes it if the panel
        is too narrow); each ``(text, style)`` extra (a count, a warning) follows
        it while it fits, and is dropped when it doesn't. Text is shown as typed:
        markup in it is escaped."""
        self._title_parts = (name, tuple(extras))
        self._fit_title()

    def on_resize(self, event: events.Resize) -> None:
        self._fit_title()

    def _fit_title(self) -> None:
        name, extras = self._title_parts
        room = (self.outer_size.width or 80) - 6  # Textual gives the label the edge minus both corners, and pads it
        used = len(name)
        markup = escape(name)
        for text, spec in extras:
            if text and used + 2 + len(text) <= room:
                markup += f"  [{spec}]{escape(text)}[/]" if spec else "  " + escape(text)
                used += 2 + len(text)
        if markup != self._title_markup:
            self._title_markup = markup
            self.border_title = markup

    def set_accent(self, accent: Colour | None, accent_2: Colour | None = None) -> None:
        """Border colours: ``accent`` on the left and top, ``accent_2`` on the right
        and bottom. Glass draws the border as a gradient from one to the other;
        without ``accent_2`` it shades ``accent`` by itself. ``None`` goes back to
        the CSS border colour."""
        if accent is None:
            if self._accent is not None:
                self._accent = None
                for side in _SIDES:
                    self.styles.clear_rule(side)
                self.refresh(layout=True)
            return
        pair = (hex_colour(accent), hex_colour(accent_2 if accent_2 is not None else accent))
        if pair != self._accent:
            self._accent = pair
            if self.is_attached:  # before that, on_mount applies it (the CSS border type is known by then)
                self._apply_accent()

    def on_mount(self) -> None:
        self._apply_accent()

    def _apply_accent(self) -> None:
        if self._accent is None:
            return
        styles: Any = self.styles
        kind = styles.border_top[0] or "round"  # keep the border type the CSS chose
        c1, c2 = Color.parse(self._accent[0]), Color.parse(self._accent[1])
        styles.border_top = (kind, c1)
        styles.border_left = (kind, c1)
        styles.border_right = (kind, c2)
        styles.border_bottom = (kind, c2)
