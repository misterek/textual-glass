"""Write to the terminal when the process ends without cleaning up after itself.

A Textual app changes terminal state that outlives it: glass sets the default
background (OSC 11) and keeps images in the terminal, an app may change the
window title. A clean exit restores all of it. A hard exit does not: SIGTERM,
SIGHUP (the window or the SSH session closed), or the interpreter exiting
without the app's own cleanup having run.

:func:`on_hard_exit` registers a string to write to the terminal in those
cases, under a key, so it can be replaced or removed (``None``) later::

    on_hard_exit("my-app.title", "\\x1b]2;\\x07")   # registered when the title changes
    on_hard_exit("my-app.title", None)             # the app restored it itself

What is registered is written, in registration order, to standard error (the
terminal Textual draws on):

* from an ``atexit`` handler, so a clean exit must remove its entries first;
* from SIGTERM and SIGHUP handlers, installed with the first entry. Each one
  writes, then calls the handler that was there before it (if it was a Python
  function), then ends the process the way the default action would: it
  restores the default action and sends itself the signal again. A signal
  that was ignored (``nohup``) is left alone.

It works whether glass is on or off; it needs no app. Handlers are installed
from the main thread only (Python allows nothing else); an entry registered
from another thread still gets the ``atexit`` write.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
from types import FrameType
from typing import Any

__all__ = ["on_hard_exit"]

SIGNALS = (signal.SIGTERM, signal.SIGHUP)

_entries: dict[str, str] = {}
_previous: dict[int, Any] = {}  # signal number -> the handler ours replaced
_atexit_installed = False


def on_hard_exit(key: str, data: str | None) -> None:
    """Write ``data`` to the terminal if the process ends without cleaning up.

    ``key`` names the entry (use your package's name as a prefix); registering
    the same key again replaces its data, and ``data=None`` removes it. Remove
    your entries on a clean exit: whatever is still registered when the
    interpreter exits is written.
    """
    if data is None:
        _entries.pop(key, None)
        return
    _entries[key] = data
    _install()


def _install() -> None:
    global _atexit_installed
    if not _atexit_installed:
        atexit.register(_write_entries)
        _atexit_installed = True
    if threading.current_thread() is not threading.main_thread():
        return  # signal.signal only works on the main thread; a later registration installs them
    for signum in SIGNALS:
        if signum in _previous:
            continue
        previous = signal.getsignal(signum)
        if previous is signal.SIG_IGN or previous is None:  # ignored on purpose, or set outside Python
            continue
        _previous[signum] = previous
        signal.signal(signum, _on_signal)


def _write_entries() -> None:
    """Write every entry once (the atexit handler, and the first step of the signal handlers)."""
    if not _entries:
        return
    data = "".join(_entries.values()).encode()
    _entries.clear()
    _write(data)


def _write(data: bytes) -> None:
    try:
        fd = sys.__stderr__.fileno()  # type: ignore[union-attr]
        while data:
            data = data[os.write(fd, data):]
    except (AttributeError, OSError, ValueError):  # no stderr, or the terminal is gone (SIGHUP)
        pass


def _on_signal(signum: int, frame: FrameType | None) -> None:
    _write_entries()
    previous = _previous.get(signum)
    if callable(previous):
        previous(signum, frame)  # if it raises (SystemExit...), that is how it ends the process
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    os._exit(128 + signum)  # only if the signal could not end us
