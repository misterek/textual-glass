"""The ``Glass`` mixin: ``class MyApp(Glass, App)``.

Widgets add pixels of their own with :class:`PixelMixin` (``pixel_items(ctx)``
gets a :class:`PixelContext`); both are exported here and from ``textual_glass``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from functools import partial
from typing import Any, ClassVar

from textual import events, on
from textual.app import App, SystemCommand
from textual.binding import Binding, BindingType
from textual.screen import Screen

from . import _textual_hooks as hooks
from .engine import MOTIONS, GlassEngine, Options
from .scene import PixelContext, PixelMixin

__all__ = ["GLASS_CSS", "Glass", "PixelContext", "PixelMixin", "terminal_write"]


def terminal_write(app: App[Any], data: str) -> bool:
    """Send raw escape sequences (a notification, a window title, the bell) to the
    app's terminal, with glass on or off.

    The data goes through Textual's own writer thread, in order with the app's
    output, so it never interleaves with a frame. Returns False if nothing was
    sent: the app has no driver yet (or any more), or it is headless.
    Never send graphics this way: glass owns the kitty protocol (rules 1 to 3).
    """
    return hooks.driver_write(app, data)

#: Added as *user* CSS, so it outranks the app's own rules. Scoped to the
#: ``-textual-glass`` class, so turning glass off restores the app's look.
#: Modal screens normally tint the screen below by blending colours, which
#: would also hide the glass; with glass on they are transparent, and the
#: dimming is a pixel scrim plus a line filter.
GLASS_CSS = """
App.-textual-glass ModalScreen, App.-textual-glass CommandPalette {
    background: transparent !important;
}
"""


class Glass(App):
    """Mix this in before ``App`` (or before your own App subclass) to get the
    pixel glass look in terminals that support the kitty graphics protocol::

        class MyApp(Glass, App):
            GLASS_MOTION = "subtle"

    Everything else about the app stays stock Textual. In a terminal without
    kitty graphics (or with ``TEXTUAL_GLASS=off``) nothing changes at all.

    Options can be class attributes or keyword arguments
    (``MyApp(glass_motion="full")``):

    * ``GLASS``: ``None`` to detect (default), ``False`` to never use glass,
      ``True`` to skip the terminal query when the environment gives no hint.
    * ``GLASS_MOTION``: ``"minimal"``, ``"subtle"`` (default) or ``"full"``.
      The ``TEXTUAL_GLASS_MOTION`` environment variable overrides it.
    * ``GLASS_BACKGROUND``: ``"auto"`` (from the Textual theme), a colour, or
      a ``(top, bottom)`` pair of colours.
    * ``GLASS_SKIP``: widget class names whose cells glass never touches
      (default ``("Button",)``).
    * ``GLASS_QUERY``: ask the terminal when the environment gives no hint
      (default ``True``).
    * ``GLASS_TERMINAL_BACKGROUND``: set the terminal's default background
      (OSC 11) to match the glass, so the window padding blends in; it is
      reset (OSC 111) on exit (default ``True``).

    Keys: ``f9`` steps through the motion levels, ``shift+f9`` turns glass off
    and on. The command palette has "Glass: ..." commands for both.
    """

    GLASS: ClassVar[bool | None] = None
    GLASS_MOTION: ClassVar[str] = "subtle"
    GLASS_BACKGROUND: ClassVar[Any] = "auto"
    GLASS_QUERY: ClassVar[bool] = True
    GLASS_SKIP: ClassVar[tuple[str, ...]] = ("Button",)
    GLASS_TERMINAL_BACKGROUND: ClassVar[bool] = True

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f9", "glass_motion", "Glass motion", show=False),
        Binding("shift+f9", "glass_toggle", "Glass on/off", show=False),
    ]

    def __init__(
        self,
        *args: Any,
        glass: bool | None = None,
        glass_motion: str | None = None,
        glass_background: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        enabled = self.GLASS if glass is None else glass
        motion = glass_motion or os.environ.get("TEXTUAL_GLASS_MOTION", "").strip().lower() or None
        if motion is None:
            motion = "minimal" if self.animation_level == "none" else self.GLASS_MOTION
        if motion not in MOTIONS:
            motion = "subtle"
        options = Options(
            enabled=enabled,
            motion=motion,
            background=self.GLASS_BACKGROUND if glass_background is None else glass_background,
            query=self.GLASS_QUERY and enabled is not True,
            skip=tuple(self.GLASS_SKIP),
            terminal_background=self.GLASS_TERMINAL_BACKGROUND,
        )
        self.glass = GlassEngine(self, options)
        """The glass engine (``app.glass.active``, ``app.glass.stats()``, ...)."""
        self.stylesheet.add_source(GLASS_CSS, read_from=("textual_glass", "GLASS_CSS"), is_default_css=False)

    async def run_async(self, *, headless: bool = False, inline: bool = False, **kwargs: Any) -> Any:
        # Detect before Textual's driver starts reading input: a reply to the
        # terminal query can then never be read as key presses.
        if not headless and not inline:
            self.glass.detect()
        return await super().run_async(headless=headless, inline=inline, **kwargs)

    @on(events.Mount)
    def _glass_mount(self) -> None:
        self.glass.start()

    def exit(self, *args: Any, **kwargs: Any) -> None:
        # Delete the images while the alternate screen is still active: Textual
        # leaves it before the App's Unmount event runs.
        self.glass.shutdown()
        super().exit(*args, **kwargs)

    @on(events.Unmount)
    def _glass_unmount(self) -> None:
        self.glass.shutdown()  # after an exception, exit() may not have run

    # -- controls -------------------------------------------------------------------

    def action_glass_motion(self, motion: str = "") -> None:
        """Set the motion level, or step to the next one."""
        if not self.glass.active:
            return
        if motion not in MOTIONS:
            motion = MOTIONS[(MOTIONS.index(self.glass.motion) + 1) % len(MOTIONS)]
        self.glass.set_motion(motion)
        self.notify(f"Glass motion: {motion}", timeout=2)

    def action_glass_toggle(self) -> None:
        """Turn glass off, or back on (only where the terminal supports it)."""
        if self.glass.active:
            self.glass.deactivate()
            self.notify("Glass off", timeout=2)
        elif self.glass.supported:
            self.glass.activate()
            self.notify("Glass on", timeout=2)

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        if not self.glass.supported:
            return
        if self.glass.active:
            for motion, help_text in (
                ("minimal", "Static glass, no animation"),
                ("subtle", "Sliding selection, focus glow and gentle transitions"),
                ("full", "Adds shimmer, glints and a pulsing focus halo"),
            ):
                yield SystemCommand(f"Glass: motion {motion}", help_text, partial(self.action_glass_motion, motion))
            yield SystemCommand("Glass: off", "Back to plain Textual (shift+f9)", self.action_glass_toggle)
        else:
            yield SystemCommand("Glass: on", "Draw the pixel glass layer (shift+f9)", self.action_glass_toggle)
