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

Under tmux, the notification escapes are wrapped in tmux's DCS passthrough
(``ESC P tmux; ... ESC \\``, every ESC inside doubled), so they reach the
terminal tmux runs in. Since tmux 3.3 that needs ``set -g allow-passthrough
on`` in ``tmux.conf``; without it tmux drops them. The bell and the title badge
are not wrapped: tmux handles those itself. GNU screen gets no notifications.
"""

from __future__ import annotations

import os
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from textual_glass import on_hard_exit

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

    Under tmux, ``TERM`` (and, from tmux 3.3, ``TERM_PROGRAM``) are tmux's own,
    so the guess goes by the variables the outer terminal set and tmux kept
    (``GHOSTTY_RESOURCES_DIR``, ``KITTY_WINDOW_ID``...). Under GNU screen it is
    ``off``. ``TEXTUAL_ORBITAL_NOTIFY`` (``osc777``, ``osc9`` or ``off``)
    overrides the guess."""
    env = os.environ if env is None else env
    forced = env.get("TEXTUAL_ORBITAL_NOTIFY", "").strip().lower()
    if forced in ("osc777", "osc9", "off"):
        return forced
    if env.get("STY"):
        return "off"
    tmux = bool(env.get("TMUX"))
    term = "" if tmux else env.get("TERM", "")
    program = env.get("TERM_PROGRAM", "").lower()
    if term == "xterm-ghostty" or program == "ghostty" or (tmux and env.get("GHOSTTY_RESOURCES_DIR")):
        return "osc777"
    if (program in ("iterm.app", "wezterm") or term == "xterm-kitty" or env.get("KITTY_WINDOW_ID")
            or (tmux and (env.get("ITERM_SESSION_ID") or env.get("WEZTERM_PANE")))):
        return "osc9"
    return "off"


def tmux_passthrough(data: str) -> str:
    """``data`` wrapped in tmux's DCS passthrough, so tmux hands it to the outer
    terminal (it needs ``allow-passthrough on``)."""
    return "\x1bPtmux;" + data.replace("\x1b", "\x1b\x1b") + "\x1b\\"


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
    Under tmux (``TMUX`` set when it is made; see ``passthrough``) notifications
    go through tmux's DCS passthrough.

    Args:
        app: The running app (its ``title``, and ``set_timer`` to space notifications).
        write: Sends raw text to the terminal through the app's driver; returns
            False if nothing was sent.
        mode: ``"auto"`` (by terminal), ``"osc777"``, ``"osc9"`` or ``"off"``.
        bell: Ring the bell with each notification (dock bounce in Ghostty).
        title: Keep a badge in the window title while things are waiting.
    """

    #: How many writes ``sent`` keeps.
    SENT_KEPT = 50
    #: The title restore's entry in glass's ``on_hard_exit`` (rule 24).
    EXIT_KEY = "textual-orbital.title"

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
        #: Wrap notifications in tmux's DCS passthrough.
        self.passthrough = bool(os.environ.get("TMUX"))
        #: The last ``SENT_KEPT`` writes that went out, oldest first (for tests and the health screen).
        self.sent: deque[str] = deque(maxlen=self.SENT_KEPT)

    def _write(self, data: str) -> bool:
        if not self._send(data):
            return False
        self.sent.append(data)
        return True

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
        if out and self.passthrough:
            out = tmux_passthrough(out)
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
        if self._write(set_title(f"{text} · {self.app.title}" if text else self.app.title)):
            on_hard_exit(self.EXIT_KEY, set_title(""))  # killed with a badge up: clear it anyway

    def restore(self) -> None:
        """On exit: clear the title. Ghostty 1.4 treats an empty OSC 2 as "no
        title was ever set" (``windowTitle`` in ``stream_handler.zig``): it
        shows the working directory or its default again, and the shell's own
        titles take over. Title pop (``CSI 23 t``) is not implemented there.
        A hard exit (SIGTERM, SIGHUP, an interpreter exit without it) clears the
        title too, through glass's ``on_hard_exit``; this removes that entry."""
        on_hard_exit(self.EXIT_KEY, None)
        if self.title and self._touched:
            self._write(set_title(""))
            self._badge = None
            self._touched = False
