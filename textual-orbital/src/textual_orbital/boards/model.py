"""The shared types of boards: a panel's spec (one JSON file per panel) and the data a panel draws (a ``Frame``).

The data side (``store``, ``formats``, ``runner``, ``source``) and the screen side (``widgets``, ``page``) meet here.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, Literal, get_args

__all__ = [
    "BOARD_RE", "DEFAULT_BOARD", "FORMAT", "ID_RE", "KINDS", "MAX_SERIES", "SIZES", "Frame", "Kind", "PanelSpec",
    "Series", "Size", "SpecError", "Table", "accumulate", "fmt_duration", "parse_duration",
]

Kind = Literal["timeseries", "stat", "gauge", "bars", "table", "text"]
KINDS: tuple[str, ...] = get_args(Kind)
#: How much room a panel asks for, relative, never a position: ``auto`` lets its kind decide (stat and gauge
#: small; timeseries, bars, table and text medium). The page turns it into a width and a height for the window.
Size = Literal["auto", "small", "medium", "large"]
SIZES: tuple[str, ...] = get_args(Size)
DEFAULT_BOARD = "main"
#: Board names are folder names: lowercase letters, digits and dashes.
BOARD_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
#: Panel ids are file names: 8 lowercase hex digits.
ID_RE = re.compile(r"[0-9a-f]{8}")
#: A panel draws at most this many series; a parser keeps the first ones.
MAX_SERIES = 8
#: The panel file format's version, written as ``"format"``.
FORMAT = 1


class SpecError(ValueError):
    """A panel file, or a panel's fields, that can't be used."""


# --------------------------------------------------------------------------------------------- durations

_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)([smhd]?)")


def parse_duration(text: str) -> float:
    """Seconds in ``90``, ``90s``, ``15m``, ``2h`` or ``1d``. Raises ``SpecError``."""
    match = _DURATION_RE.fullmatch(text.strip())
    if not match:
        raise SpecError(f"not a duration: {text!r} (like 90s, 15m, 2h or 1d)")
    seconds = float(match.group(1)) * _UNITS[match.group(2) or "s"]
    if seconds <= 0:
        raise SpecError(f"a duration must be above zero: {text!r}")
    return seconds


def fmt_duration(seconds: float) -> str:
    """A short duration: ``45s``, ``15m``, ``1h30m``, ``2h``, ``1d``, ``1d6h``. Whole units, rounded down."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        hours, minutes = divmod(total // 60, 60)
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(total // 3600, 24)
    return f"{days}d{hours}h" if hours else f"{days}d"


# ------------------------------------------------------------------------------------------------ panel


def _when(value: Any, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SpecError(f"{name} must be an ISO 8601 time, not {type(value).__name__}")
    try:
        when = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise SpecError(f"{name}: {error}") from None
    if when.tzinfo is None:
        raise SpecError(f"{name} must have a time zone: {value!r}")
    return when


_TEXT_FIELDS = ("id", "board", "title", "kind", "unit", "added_by", "size")
_OPTIONAL_TEXT = ("cmd", "cwd", "key")
_NUMBERS = ("every", "window", "timeout")
_OPTIONAL_NUMBERS = ("ymin", "ymax", "position")
_TIMES = ("added_at", "expires_at", "updated_at")


@dataclass(frozen=True)
class PanelSpec:
    """One panel: what it shows and where its data comes from. Stored as ``<root>/<board>/<id>.json``.

    Exactly one of ``cmd`` (a shell command, run with ``/bin/sh -c`` in ``cwd``) and ``data`` (inline data:
    any JSON value, read with the same formats as a command's output) is set. Times are timezone-aware.
    """

    id: str
    board: str
    title: str
    kind: Kind
    added_at: datetime
    cmd: str | None = None
    data: Any = field(default=None, hash=False)
    cwd: str | None = None
    unit: str = ""
    every: float = 30.0          # seconds between runs of cmd
    window: float = 900.0        # seconds of history a timeseries shows
    timeout: float = 20.0        # seconds one run of cmd may take
    ymin: float | None = None
    ymax: float | None = None    # also a gauge's full scale
    columns: tuple[str, ...] = ()  # a table's columns, in this order (empty: all)
    key: str | None = None       # the adder's own name for it: the same key on the same board replaces it
    added_by: str = ""           # who added it: the adder's project folder name
    expires_at: datetime | None = None
    updated_at: datetime | None = None  # set when a later add with the same key replaced it
    size: Size = "auto"          # how much room it asks for (``SIZES``); the user can change it on the page
    position: float | None = None  # its place on the board when the user moved it (see ``order_key``)

    def __post_init__(self) -> None:
        if not ID_RE.fullmatch(self.id):
            raise SpecError(f"bad panel id {self.id!r}: 8 lowercase hex digits")
        if not BOARD_RE.fullmatch(self.board):
            raise SpecError(f"bad board name {self.board!r}: lowercase letters, digits and dashes, up to 32")
        if self.kind not in KINDS:
            raise SpecError(f"bad kind {self.kind!r}: one of {', '.join(KINDS)}")
        if self.size not in SIZES:
            raise SpecError(f"bad size {self.size!r}: one of {', '.join(SIZES)}")
        if self.position is not None and not math.isfinite(self.position):
            raise SpecError(f"position must be a finite number, not {self.position!r}")
        if not self.title.strip():
            raise SpecError("a panel needs a title")
        if (self.cmd is None) == (self.data is None):
            raise SpecError("a panel needs exactly one of cmd and data")
        if self.cmd is not None and not self.cmd.strip():
            raise SpecError("cmd is empty")
        for name in _NUMBERS:
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
                raise SpecError(f"{name} must be a number of seconds above zero, not {value!r}")
        for name in _TIMES:
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise SpecError(f"{name} must be timezone-aware")
        object.__setattr__(self, "columns", tuple(self.columns))

    @property
    def changed_at(self) -> datetime:
        """When this panel was last added or replaced. Changing only ``size`` or ``position`` (arranging it)
        leaves it alone: arranging is no reason to run the command again."""
        return self.updated_at or self.added_at

    @property
    def order_key(self) -> tuple[float, str]:
        """Panels on a board sort by this: ``position`` if the user moved it, else when it was added
        (unix seconds), then the id. Moving a panel sets ``position`` between its new neighbours' keys."""
        return (self.position if self.position is not None else self.added_at.timestamp(), self.id)

    def expired(self, at: datetime) -> bool:
        return self.expires_at is not None and at >= self.expires_at

    def to_json(self) -> dict[str, Any]:
        """The panel file's object (``json.dumps`` it). Every field is written, ``format`` first."""
        doc: dict[str, Any] = {"format": FORMAT}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, tuple):
                value = list(value)
            doc[f.name] = value
        return doc

    @classmethod
    def from_json(cls, doc: Any) -> PanelSpec:
        """Read a panel file's object. Unknown keys are ignored. Raises ``SpecError``."""
        if not isinstance(doc, Mapping):
            raise SpecError("a panel file holds one JSON object")
        if doc.get("format", FORMAT) != FORMAT:
            raise SpecError(f"unknown panel format {doc.get('format')!r}")
        args: dict[str, Any] = {}
        for name in _TEXT_FIELDS:
            if name in doc:
                if not isinstance(doc[name], str):
                    raise SpecError(f"{name} must be text")
                args[name] = doc[name]
        for name in _OPTIONAL_TEXT:
            if doc.get(name) is not None:
                if not isinstance(doc[name], str):
                    raise SpecError(f"{name} must be text")
                args[name] = doc[name]
        for name in (*_NUMBERS, *_OPTIONAL_NUMBERS):
            value = doc.get(name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SpecError(f"{name} must be a number")
            args[name] = float(value)
        for name in _TIMES:
            if doc.get(name) is not None:
                args[name] = _when(doc[name], name)
        if "data" in doc:
            args["data"] = doc["data"]
        columns = doc.get("columns") or []
        if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
            raise SpecError("columns must be a list of text")
        args["columns"] = tuple(columns)
        missing = [name for name in ("id", "board", "title", "kind", "added_at") if name not in args]
        if missing:
            raise SpecError(f"missing {', '.join(missing)}")
        return cls(**args)


# ------------------------------------------------------------------------------------------------ frames


@dataclass(frozen=True)
class Series:
    """Numbers over time: ``(unix seconds, value)`` points, oldest first. A NaN value is a gap."""

    name: str
    points: tuple[tuple[float, float], ...]

    @property
    def last(self) -> float | None:
        """The newest value that isn't NaN, or None."""
        for _, value in reversed(self.points):
            if not math.isnan(value):
                return value
        return None


@dataclass(frozen=True)
class Table:
    """Rows of text under column names. Every row is as long as ``columns``."""

    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Frame:
    """What one run of a panel gave: series (numbers over time), a table, or text.

    ``note`` is what the command said on stderr when it printed nothing (a CLI's "No results found").
    ``at`` is when the run happened (unix seconds); it takes no part in equality, so two runs that gave the
    same data are equal and the screen has nothing to redraw (rule 9).
    """

    series: tuple[Series, ...] = ()
    table: Table | None = None
    text: str | None = None
    note: str = ""
    at: float = field(default=0.0, compare=False)

    @property
    def shape(self) -> Literal["series", "table", "text", "empty"]:
        if self.series:
            return "series"
        if self.table is not None:
            return "table"
        if self.text is not None:
            return "text"
        return "empty"

    @property
    def history(self) -> bool:
        """True when any series has more than one point (a range query), False for single readings."""
        return any(len(s.points) > 1 for s in self.series)


def accumulate(previous: Frame | None, new: Frame, window: float) -> Frame:
    """Fold a run's frame into the panel's history, which the screen keeps per panel.

    A run that brought history (``new.history``: a range query) replaces what was there, and so does any
    table, text or empty frame. A run of single readings (a command that prints one number, or ``label
    number`` lines) adds each point to its series' history, by name, and drops points older than ``window``
    seconds before ``new.at``. A series that stops appearing ages out the same way. Order: the series
    already shown first, then new names; at most ``MAX_SERIES``.
    """
    if previous is None or new.shape != "series" or new.history or previous.shape != "series":
        return new
    cutoff = new.at - window
    old = {s.name: s.points for s in previous.series}
    fresh = {s.name: s.points for s in new.series}
    names = [*old, *(name for name in fresh if name not in old)]
    out: list[Series] = []
    for name in names:
        points = tuple(p for p in (*old.get(name, ()), *fresh.get(name, ())) if p[0] >= cutoff)
        if points:
            out.append(Series(name, points))
    return Frame(series=tuple(out[:MAX_SERIES]), note=new.note, at=new.at)
