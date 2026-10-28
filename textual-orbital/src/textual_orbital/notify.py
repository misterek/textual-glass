"""Getting attention when the window is half hidden: desktop notifications,
the window title badge and the bell.

What Ghostty 1.4 does with each (checked in its source, tag ``v1.4.0``):

* **OSC 777** (``ESC ] 777 ; notify ; title ; body BEL``) and **OSC 9**
  (``ESC ] 9 ; body BEL``, iTerm2 style) both become a desktop notification
  (``src/terminal/osc/parsers/rxvt_extension.zig`` and ``osc9.zig``), unless
  ``desktop-notifications = false``. Ghostty rate-limits them to one per
  second, and drops an identical one within five seconds (``Surface.zig``).
  On macOS the first one asks for notification permission; a notification
  that arrives while the window is focused is removed after a few seconds.
  An OSC 9 body that starts with a digit and ``;`` is read as a ConEmu
  sequence instead, so bodies here never start with a digit.
* **OSC 99** (kitty's protocol) is parsed but not acted on, so it is not used.
* **OSC 2** sets the window title: the badge ("◉ 2 waiting · My App").
  Title push and pop (``CSI 22 t`` / ``CSI 23 t``) are parsed but not
  implemented, so the original title cannot be restored. On exit the title
  is set to empty, which Ghostty treats as "never set": it shows the working
  directory (or its default) and the shell's own titles take over again.
* **BEL**: with Ghostty's default ``bell-features`` (``attention`` and
  ``title``), an unfocused window bounces the dock icon once and gets a bell
  emoji in its title until it is focused.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from textual.app import App

    from .model import Alert

#: Ghostty shows at most one notification a second; space them a bit more.
MIN_SPACING = 1.2
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _clean(text: str, limit: int = 200) -> str:
    return _CONTROL.sub(" ", text).strip()[:limit]


def notification_mode(env: Mapping[str, str] | None = None) -> str:
    """Pick a notification escape for the terminal: ``osc777``, ``osc9`` or ``off``.

    ``TEXTUAL_ORBITAL_NOTIFY`` (``osc777``, ``osc9`` or ``off``) overrides the guess."""
    env = os.environ if env is None else env
    forced = env.get("TEXTUAL_ORBITAL_NOTIFY", "").strip().lower()
    if forced in ("osc777", "osc9", "off"):
        return forced
    if env.get("TMUX") or env.get("STY"):
        return "off"
    term = env.get("TERM", "")
    program = env.get("TERM_PROGRAM", "").lower()
    if term == "xterm-ghostty" or program == "ghostty":
        return "osc777"
    if program in ("iterm.app", "wezterm") or term == "xterm-kitty" or env.get("KITTY_WINDOW_ID"):
        return "osc9"
    return "off"


def osc777(title: str, body: str) -> str:
    return f"\x1b]777;notify;{_clean(title, 80).replace(';', ',')};{_clean(body)}\x07"


def osc9(title: str, body: str) -> str:
    text = _clean(f"{title}: {body}" if title else body)
    if text[:1].isdigit():
        text = "· " + text
    return f"\x1b]9;{text}\x07"


def set_title(title: str) -> str:
    return f"\x1b]2;{_clean(title, 120)}\x07"


class Notifier:
    """Desktop notifications, a title badge and the bell for ``app``.

    The app's ``title`` names it in notifications and in the window title.

    Args:
        app: The running app (its ``title``, and ``set_timer`` to space notifications).
        write: Sends raw text to the terminal through the app's driver; returns
            False if nothing was sent.
        mode: ``"auto"`` (by terminal), ``"osc777"``, ``"osc9"`` or ``"off"``.
        bell: Ring the bell with each notification (dock bounce in Ghostty).
        title: Keep a badge in the window title while things are waiting.
    """

    def __init__(self, app: App[Any], write: Callable[[str], bool], mode: str = "auto", bell: bool = True,
                 title: bool = True) -> None:
        self.app = app
        self._send = write
        self.mode = notification_mode() if mode == "auto" else mode
        self.bell = bell
        self.title = title
        self._badge: str | None = None
        self._touched = False  # we have set the title (so restore clears it)
        self._last = 0.0
        self._backlog: list[str] = []
        self.sent: list[str] = []  # what was sent, for tests and the health screen

    def _write(self, data: str) -> None:
        if self._send(data):
            self.sent.append(data)

    def notify(self, alert: Alert) -> None:
        """A desktop notification (and the bell) for ``alert``."""
        if not alert.notify:
            return
        body = " · ".join([alert.title, *alert.body[:1]])
        heading = alert.heading.capitalize()
        out = ""
        if self.mode == "osc777":
            out = osc777(f"{self.app.title}: {heading}", body)
        elif self.mode == "osc9":
            out = osc9(f"{self.app.title}: {heading}", body)
        if self.bell:
            out += "\x07"
        if out:
            self._backlog.append(out)
            self._drain()

    def _drain(self) -> None:
        if not self._backlog:
            return
        wait = self._last + MIN_SPACING - time.monotonic()
        if wait > 0:
            try:
                self.app.set_timer(wait, self._drain)
            except Exception:  # noqa: BLE001 - no running app (tests)
                pass
            return
        self._last = time.monotonic()
        self._write(self._backlog.pop(0))
        if self._backlog:
            self._drain()

    def badge(self, text: str | None) -> None:
        """Show ``text`` in front of the window title (None or "" clears it)."""
        text = text or None
        if not self.title or (text == self._badge and self._touched):
            return
        self._badge = text
        self._touched = True
        self._write(set_title(f"{text} · {self.app.title}" if text else self.app.title))

    def restore(self) -> None:
        """On exit: clear the title. Ghostty 1.4 treats an empty OSC 2 as "no
        title was ever set" (``windowTitle`` in ``stream_handler.zig``): it
        shows the working directory or its default again, and the shell's own
        titles take over. Title pop (``CSI 23 t``) is not implemented there."""
        if self.title and self._touched:
            self._write(set_title(""))
            self._badge = None
            self._touched = False
