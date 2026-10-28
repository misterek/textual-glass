"""Decide whether the terminal can show the glass layer.

This runs before Textual's driver starts reading input, so a query reply can
never reach Textual's key parser. The order is:

1. ``TEXTUAL_GLASS=on|off`` overrides everything.
2. No tty, a terminal multiplexer, Windows or ``TERM=dumb``: off.
3. Environment hints: ``TERM=xterm-ghostty`` or ``xterm-kitty``,
   ``TERM_PROGRAM=ghostty`` or ``kitty``, or ``KITTY_WINDOW_ID``: on.
4. Otherwise (optionally) ask the terminal: a kitty graphics query (``a=q``)
   followed by a DA1 request as a fence, on ``/dev/tty``, with a short timeout.
   Every terminal answers DA1, so when the DA1 reply arrives without a graphics
   reply first, the answer is no. When nothing at all arrives before the
   timeout (a pty with nobody on the other end), the answer is also no.
   Replies that come late are still read here, never by Textual as key
   presses: with nothing back at the timeout it listens ``DRAIN`` seconds
   longer, and once a reply has started it waits up to ``LATE`` seconds more
   for its fence.
"""

from __future__ import annotations

import os
import re
import select
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass

ON_WORDS = {"1", "on", "yes", "true", "force"}
OFF_WORDS = {"0", "off", "no", "false", "plain"}
QUERY_ID = 31
#: The query: a 1x1 RGB image sent directly (t=d), action query, then DA1.
QUERY = f"\x1b_Gi={QUERY_ID},s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\\x1b[c".encode()
DA1_REPLY = re.compile(rb"\x1b\[\?[0-9;]*c")
GRAPHICS_OK = re.compile(rb"\x1b_Gi=" + str(QUERY_ID).encode() + rb";OK\x1b\\")
DRAIN = 0.1  # after the timeout with nothing back: how much longer to listen for a slow reply
LATE = 1.0   # after the timeout once a reply has started: how much longer to wait for its DA1 fence


@dataclass(frozen=True)
class Support:
    """The outcome of detection."""

    enabled: bool
    reason: str


def env_hint(env: Mapping[str, str]) -> bool:
    """True when the environment names a terminal known to support the protocol."""
    term = env.get("TERM", "")
    program = env.get("TERM_PROGRAM", "").lower()
    return (
        term in ("xterm-ghostty", "xterm-kitty")
        or program in ("ghostty", "kitty")
        or bool(env.get("KITTY_WINDOW_ID"))
    )


def multiplexed(env: Mapping[str, str]) -> bool:
    term = env.get("TERM", "")
    return bool(env.get("TMUX") or env.get("STY")) or term.startswith(("screen", "tmux"))


def detect(
    env: Mapping[str, str] | None = None,
    *,
    query: bool = True,
    timeout: float = 0.3,
    tty_path: str = "/dev/tty",
) -> Support:
    """Work out whether to enable glass. A terminal that never answers costs
    ``timeout`` plus ``DRAIN`` seconds; one that has started to answer is given up
    to ``LATE`` seconds more to finish."""
    env = os.environ if env is None else env
    override = env.get("TEXTUAL_GLASS", "").strip().lower()
    if override in OFF_WORDS:
        return Support(False, f"TEXTUAL_GLASS={override}")
    if sys.platform == "win32":
        return Support(False, "Windows is not supported")
    if not _is_tty():
        return Support(False, "not a terminal")
    if override in ON_WORDS:
        return Support(True, f"TEXTUAL_GLASS={override}")
    if env.get("TERM", "") in ("", "dumb"):
        return Support(False, "TERM is dumb or unset")
    if multiplexed(env):
        return Support(False, "inside tmux or screen (set TEXTUAL_GLASS=on to force)")
    if env_hint(env):
        return Support(True, "environment names a kitty-graphics terminal")
    if not query:
        return Support(False, "no environment hint and the query is disabled")
    answer = query_terminal(tty_path, timeout)
    if answer is None:
        return Support(False, "the terminal did not answer")
    return Support(answer, "the terminal answered the graphics query" if answer else "no graphics support reported")


def _is_tty() -> bool:
    try:
        return sys.__stdin__.isatty() and sys.__stdout__.isatty()  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        return False


def query_terminal(tty_path: str = "/dev/tty", timeout: float = 0.3) -> bool | None:
    """Send the graphics query and the DA1 fence. True/False, or None for no answer."""
    try:
        import termios
    except ImportError:  # pragma: no cover - not POSIX
        return None
    try:
        fd = os.open(tty_path, os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return None
    try:
        try:
            saved = termios.tcgetattr(fd)
        except termios.error:
            return None
        raw = list(saved)
        raw[3] &= ~(termios.ICANON | termios.ECHO)  # lflags: no line buffering, no echo
        raw[6] = list(raw[6])
        raw[6][termios.VMIN] = 0
        raw[6][termios.VTIME] = 0
        try:
            termios.tcsetattr(fd, termios.TCSANOW, raw)
            os.write(fd, QUERY)
            reply = _read_until_da1(fd, timeout)
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
    except OSError:
        return None
    finally:
        os.close(fd)
    match = DA1_REPLY.search(reply)
    if match is None:
        return None
    return GRAPHICS_OK.search(reply[:match.start()]) is not None


def _read_until_da1(fd: int, timeout: float) -> bytes:
    """Everything up to the DA1 reply. Past ``timeout`` it keeps reading for
    ``DRAIN`` seconds more, or ``LATE`` once anything has come back, so a slow
    reply is consumed here and doesn't reach Textual's input as key presses."""
    start = time.monotonic()
    buffer = b""
    while not DA1_REPLY.search(buffer):
        remaining = start + timeout + (LATE if buffer else DRAIN) - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            continue  # the deadline is checked again (it grows once a reply has started)
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        buffer += chunk
    return buffer
