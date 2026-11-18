"""Run one panel: its command, with the time range in the environment and a deadline, read into a ``Frame``.

``run(spec)`` blocks for up to ``spec.timeout`` seconds (plus a moment to reap the process), so call it from
a thread with a deadline of its own. It raises ``PanelError``, whose message is one line, for every failure.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from .formats import ANSI, parse, parse_data
from .model import Frame, Kind, PanelSpec, fmt_duration

__all__ = ["MESSAGE_LIMIT", "PanelError", "one_line", "panel_env", "run", "run_command", "run_data"]

#: The longest error message kept, in characters.
MESSAGE_LIMIT = 300
#: Seconds to wait for a killed command's pipes to close before giving up on its output.
REAP_TIMEOUT = 2.0


class PanelError(Exception):
    """A panel's run failed. ``str(error)`` is one line: what the command said, or what went wrong."""

    def __init__(self, message: str) -> None:
        super().__init__(one_line(message))


def one_line(text: str) -> str:
    """The last non-empty line of text, without escapes, cut to ``MESSAGE_LIMIT`` characters."""
    lines = [line.strip() for line in ANSI.sub("", text).splitlines() if line.strip()]
    line = lines[-1] if lines else ""
    return line if len(line) <= MESSAGE_LIMIT else line[: MESSAGE_LIMIT - 1] + "…"


def _now(now: datetime | float | None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def _iso(t: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def panel_env(window: float, now: datetime | float | None = None) -> dict[str, str]:
    """The variables a panel's command gets: its time window, in several spellings."""
    end = int(_now(now))
    start = int(end - window)
    return {
        "PANEL_START": str(start),
        "PANEL_END": str(end),
        "PANEL_START_ISO": _iso(start),
        "PANEL_END_ISO": _iso(end),
        "PANEL_STEP": str(max(15, round(window / 240))),
        "PANEL_WINDOW": fmt_duration(window),
    }


def _kill(proc: subprocess.Popen[str]) -> None:
    """Kill the command and everything it started (its process group), and reap it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.communicate(timeout=REAP_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Something left the group and still holds the pipes: stop reading them.
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        try:
            proc.wait(timeout=REAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass


def run_command(cmd: str, *, cwd: str | None = None, timeout: float = 20.0, window: float = 900.0,
                kind: Kind | str | None = None, columns: Sequence[str] = (),
                now: datetime | float | None = None) -> Frame:
    """Run ``cmd`` with ``/bin/sh -c`` in ``cwd`` (the current folder if it's None or gone), read its output
    and return the frame. Raises ``PanelError``."""
    t = _now(now)
    folder = cwd if cwd and os.path.isdir(cwd) else None
    env = {**os.environ, **panel_env(window, t)}
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", cmd], cwd=folder, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", start_new_session=True)
    except OSError as error:
        raise PanelError(f"can't start /bin/sh: {error.strerror or error}") from None
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        raise PanelError(f"timed out after {fmt_duration(timeout)}") from None
    except BaseException:
        _kill(proc)
        raise
    said = one_line(err)
    if proc.returncode:
        if said:
            raise PanelError(said)
        if proc.returncode < 0:
            raise PanelError(f"killed by signal {-proc.returncode}")
        raise PanelError(f"exited with status {proc.returncode}")
    if not out.strip():
        return Frame(note=said, at=t)
    try:
        return parse(out, kind=kind, columns=columns, at=t)
    except ValueError as error:
        raise PanelError(str(error) or "output that can't be read") from None


def run_data(data: Any, *, kind: Kind | str | None = None, columns: Sequence[str] = (),
             now: datetime | float | None = None) -> Frame:
    """Read a panel's inline data. Raises ``PanelError``."""
    try:
        return parse_data(data, kind=kind, columns=columns, at=_now(now))
    except ValueError as error:
        raise PanelError(str(error) or "data that can't be read") from None


def run(spec: PanelSpec, now: datetime | float | None = None) -> Frame:
    """Run one panel now and return what it gave. Raises ``PanelError`` with a one-line message."""
    if spec.cmd is None:
        return run_data(spec.data, kind=spec.kind, columns=spec.columns, now=now)
    return run_command(spec.cmd, cwd=spec.cwd, timeout=spec.timeout, window=spec.window, kind=spec.kind,
                       columns=spec.columns, now=now)
