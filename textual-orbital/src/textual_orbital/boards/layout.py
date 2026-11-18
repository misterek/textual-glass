"""Where a board's panels go: a pure function from what each panel needs to a grid that fits the page.

Nobody places panels by coordinates. Each panel asks for a *size* (``small``, ``medium`` or ``large``; ``auto``
lets its kind decide) and has a minimum height (its kind's, more for a table with many rows or bars with many
series). :func:`plan` packs the panels, in order, into rows of a 12-unit grid and gives each row a height:

* **Width**: under 70 columns everything spans 12. From 70: small 4, medium 6, large 12. From 140: small 3,
  medium 6, large 12.
* **Packing**: row by row (next-fit, so the order on screen is the panels' order): a panel that doesn't fit what's
  left of the row starts a new one. Then each row's leftover units are shared out among its panels by their spans,
  so every row is full: one panel fills the page, and a lone panel at the end of a board stretches across.
* **Height**: a row is as tall as its tallest minimum. Spare page height goes to the rows by the largest weight in
  each (small 0, medium 1, large 2; a page of only weightless rows shares it evenly). When the minimums don't fit,
  rows keep their minimums and the page scrolls.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, get_args

__all__ = ["LADDER", "MIN_HEIGHT", "UNITS", "WEIGHT", "Need", "Plan", "min_height", "need", "plan", "reposition",
           "resolve", "span", "step_size"]

#: A size the page can lay out (``auto`` resolved): smallest first.
Room = Literal["small", "medium", "large"]
LADDER: tuple[str, ...] = get_args(Room)
#: The grid's width in units.
UNITS = 12
#: Under this many columns, everything spans the page; from :data:`WIDE`, smalls get narrower.
NARROW = 70
WIDE = 140
#: What ``auto`` means for each kind.
KIND_SIZE: dict[str, str] = {"stat": "small", "gauge": "small", "timeseries": "medium", "bars": "medium",
                             "table": "medium", "text": "medium"}
#: Each kind's minimum height in rows, its border included (bars and tables add their rows: see :func:`min_height`).
MIN_HEIGHT: dict[str, int] = {"stat": 6, "gauge": 8, "timeseries": 9, "bars": 5, "table": 5, "text": 6}
#: Bars: their border, then a row per series. Tables: their border and header, then a row per row, up to
#: :data:`TABLE_ROWS`.
BARS_BASE = 2
TABLE_BASE = 3
TABLE_ROWS = 5
#: A large panel's minimum is this much taller than its kind's.
LARGE_EXTRA = 3
#: How much of the spare page height a row asks for, by its largest size.
WEIGHT: dict[str, int] = {"small": 0, "medium": 1, "large": 2}


def resolve(kind: str, size: str) -> str:
    """The size a panel gets: its own, or its kind's for ``auto``."""
    return KIND_SIZE.get(kind, "medium") if size == "auto" else size


def step_size(kind: str, size: str, step: int) -> str | None:
    """The next size up (``step`` 1) or down (-1) from ``size`` (``auto``: from its kind's), or None at the end."""
    at = LADDER.index(resolve(kind, size)) + step
    return LADDER[at] if 0 <= at < len(LADDER) else None


def min_height(kind: str, size: str, count: int = 0) -> int:
    """A panel's minimum height in rows. ``count`` is what its kind grows with: the series of a ``bars`` panel,
    the rows of a ``table``."""
    if kind == "bars":
        base = max(MIN_HEIGHT["bars"], BARS_BASE + count)
    elif kind == "table":
        base = max(MIN_HEIGHT["table"], TABLE_BASE + min(count, TABLE_ROWS))
    else:
        base = MIN_HEIGHT.get(kind, MIN_HEIGHT["text"])
    return base + (LARGE_EXTRA if resolve(kind, size) == "large" else 0)


def span(size: str, width: int) -> int:
    """The units a ``small``, ``medium`` or ``large`` panel spans on a page ``width`` columns wide."""
    if width < NARROW or size == "large":
        return UNITS
    if size == "medium":
        return UNITS // 2
    return UNITS // 4 if width >= WIDE else UNITS // 3


@dataclass(frozen=True)
class Need:
    """What one panel needs: its size (``small``, ``medium`` or ``large``) and its minimum height in rows."""

    size: str
    height: int


def need(kind: str, size: str, count: int = 0) -> Need:
    """A panel's :class:`Need` from its kind, its size (``auto`` too) and its count (see :func:`min_height`)."""
    return Need(resolve(kind, size), min_height(kind, size, count))


@dataclass(frozen=True)
class Plan:
    """Rows of ``(panel index, span)``, left to right, and each row's height. The spans of a row add up to
    :data:`UNITS`."""

    rows: tuple[tuple[tuple[int, int], ...], ...]
    heights: tuple[int, ...]

    def spans(self) -> list[int]:
        """Each panel's span, by panel index."""
        out = [0] * sum(len(row) for row in self.rows)
        for row in self.rows:
            for index, units in row:
                out[index] = units
        return out

    def place(self, index: int) -> tuple[int, int, int]:
        """``(row, first unit, units)`` of a panel."""
        for r, row in enumerate(self.rows):
            x = 0
            for i, units in row:
                if i == index:
                    return r, x, units
                x += units
        raise IndexError(index)

    def vertical(self, index: int, dy: int) -> int:
        """The panel ``dy`` rows up (-1) or down (1) from panel ``index``: the one that overlaps it most (the
        earlier one on a tie), or ``index`` itself when there is no such row."""
        r, x, units = self.place(index)
        target = r + dy
        if not 0 <= target < len(self.rows):
            return index
        best, most, at = index, -1, 0
        for i, w in self.rows[target]:
            overlap = min(x + units, at + w) - max(x, at)
            if overlap > most:
                best, most = i, overlap
            at += w
        return best


def _shares(amount: int, weights: Sequence[int]) -> list[int]:
    """``amount`` split by ``weights`` into whole numbers that add up to it (the largest remainders round up;
    ties go to the earlier one)."""
    total = sum(weights)
    if amount <= 0 or total <= 0:
        return [0] * len(weights)
    exact = [amount * w / total for w in weights]
    out = [int(e) for e in exact]
    order = sorted(range(len(weights)), key=lambda k: (-(exact[k] - out[k]), k))
    for k in order[: amount - sum(out)]:
        out[k] += 1
    return out


def plan(items: Sequence[Need], width: int, height: int) -> Plan:
    """Lay out ``items`` (in order) on a page ``width`` by ``height`` cells. See the module docstring."""
    rows: list[list[tuple[int, int]]] = []
    used = UNITS
    for index, item in enumerate(items):
        units = span(item.size, width)
        if used + units > UNITS:
            rows.append([])
            used = 0
        rows[-1].append((index, units))
        used += units
    filled: list[tuple[tuple[int, int], ...]] = []
    for row in rows:
        extra = _shares(UNITS - sum(units for _, units in row), [units for _, units in row])
        filled.append(tuple((index, units + more) for (index, units), more in zip(row, extra, strict=True)))
    mins = [max(items[index].height for index, _ in row) for row in filled]
    weights = [max(WEIGHT.get(items[index].size, 1) for index, _ in row) for row in filled]
    if not any(weights):
        weights = [1] * len(filled)  # nothing asks for the spare height: the rows share it
    spare = _shares(height - sum(mins), weights)
    return Plan(tuple(filled), tuple(m + s for m, s in zip(mins, spare, strict=True)))


def reposition(keys: Sequence[float], at: int, to: int, gap: float = 1.0) -> dict[int, float]:
    """New positions that move item ``at`` to index ``to`` in a list ordered by ``keys`` (a board's panels by
    ``order_key``). Usually only the moved item's: halfway between its new neighbours, or ``gap`` before the
    first or after the last. When there's no room between them (equal keys), every item is spaced out again,
    ``gap`` apart from the smallest key. Returns ``{index: position}`` for the items that change."""
    if not 0 <= at < len(keys) or not 0 <= to < len(keys) or at == to:
        return {}
    order = [k for k in range(len(keys)) if k != at]
    order.insert(to, at)
    before = keys[order[to - 1]] if to > 0 else None
    after = keys[order[to + 1]] if to + 1 < len(order) else None
    if before is None:
        assert after is not None  # two items at least
        position = after - gap
    elif after is None:
        position = before + gap
    else:
        position = before + (after - before) / 2
    if (before is None or before < position) and (after is None or position < after):
        return {at: position}
    base = min(keys)
    return {k: base + n * gap for n, k in enumerate(order) if keys[k] != base + n * gap}
