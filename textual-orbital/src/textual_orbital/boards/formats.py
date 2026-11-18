"""Command output (or inline data) -> ``Frame``, the kind a frame suggests, and a one-line description of it.

The readers, in the order ``parse`` tries them:

1. JSON: Prometheus-style query results (a whole API response, its ``data``, or a bare list of series), a
   list of objects (or one object per line, or ``{"items": [...]}``) as a table, a number, or an object of
   ``name: number``.
2. One number, with an optional unit after it (``42``, ``4.2 ms``, ``99%``).
3. ``label number`` lines, one reading per label.
4. Text in columns under a header, the way ``ps``, ``df`` and most CLIs print lists, or tab-separated.
5. Anything else is text.

A ``ValueError`` from here is a one-line message for the person or agent who wrote the command.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .model import MAX_SERIES, Frame, Kind, Series, Table

__all__ = ["ANSI", "describe", "fmt_number", "guess_kind", "parse", "parse_columns", "parse_data", "series_names"]

#: Colour and other terminal escapes, in case a command prints them anyway.
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_NUM = r"[-+]?(?:\d{1,3}(?:,\d{3})+(?:\.\d*)?|\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
#: One number and an optional unit: ``42``, ``4.2 ms``, ``99%``, ``1,234 req/s``.
_ONE_NUMBER = re.compile(rf"({_NUM})\s*([A-Za-z%µ/][A-Za-z%µ/]{{0,11}})?")
#: A ``label number`` line: the number is the last word (a ``%`` may follow it); the label is the rest.
_LABEL_LINE = re.compile(rf"(?:(.*?\S)\s+)?({_NUM})\s*%?")
_NUMERIC_CELL = re.compile(rf"{_NUM}%?")


def _now(at: float | datetime | None) -> float:
    if at is None:
        return time.time()
    if isinstance(at, datetime):
        return at.timestamp()
    return float(at)


def _number(text: str) -> float:
    return float(text.replace(",", ""))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def fmt_number(value: float | None) -> str:
    """Three significant figures, compact past 10,000: ``4``, ``12.5``, ``0.031``, ``212``, ``12.9K``."""
    if value is None or not math.isfinite(value):
        return "–"
    for above, div, suffix in ((1e9, 1e9, "G"), (1e6, 1e6, "M"), (1e4, 1e3, "K")):
        if abs(value) >= above:
            return f"{value / div:.3g}{suffix}"
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:.3g}"


def _with_unit(text: str, unit: str) -> str:
    if not unit:
        return text
    return f"{text}{unit}" if unit == "%" else f"{text} {unit}"


def _clean(text: str) -> str:
    return ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def _text_frame(text: str, at: float) -> Frame:
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return Frame(text="\n".join(lines), at=at) if lines else Frame(at=at)


def _readings(pairs: Sequence[tuple[str, float]], at: float) -> Frame:
    values: dict[str, float] = {}
    for name, value in pairs:
        values[name] = value if math.isfinite(value) else math.nan
    series = tuple(Series(name, ((at, value),)) for name, value in list(values.items())[:MAX_SERIES])
    return Frame(series=series, at=at)


# ------------------------------------------------------------------------------------------------ tables


def parse_columns(text: str) -> tuple[list[str], list[list[str]]]:
    """Text laid out in columns under a header line -> (header, rows), every row as long as the header.

    Columns are cut where every line is blank, so a value can have spaces in it ("Up 2 hours"). A stretch
    with nothing above it in the header belongs to the column before it. Tab-separated text is split on
    the tabs.
    """
    # Only spaces come off the end: a trailing tab is an empty last cell.
    lines = [line.rstrip(" ") for line in _clean(text).splitlines()]
    lines = [line for line in lines if line.strip()]
    if not lines:
        return [], []
    if all("\t" in line for line in lines) and len({line.count("\t") for line in lines}) == 1:
        cells = [[c.strip() for c in line.split("\t")] for line in lines]
        return cells[0], cells[1:]
    lines = [line.expandtabs() for line in lines]
    head, body = lines[0], lines[1:]
    width = max(map(len, lines))
    blank = [all(p >= len(line) or line[p] == " " for line in lines) for p in range(width)]
    if body and not body[0].strip("-= ") and len(body[0]) >= len(head.rstrip()):
        body = body[1:]  # a rule under the header, not a row

    spans: list[list[int]] = []  # runs of positions where some line has something
    start: int | None = None
    for p, is_blank in enumerate([*blank, True]):
        if not is_blank and start is None:
            start = p
        elif is_blank and start is not None:
            spans.append([start, p])
            start = None

    def cut(line: str, s: int, e: int) -> str:
        return line[s:e].strip()

    merged: list[list[int]] = []
    for s, e in spans:
        if merged and not cut(head, s, e):
            merged[-1][1] = e  # nothing in the header here: the column before runs on
        else:
            merged.append([s, e])

    def empty(s: int, e: int) -> bool:
        return not any(cut(line, s, e) for line in body)

    # Two header words one space apart, one of them with nothing under it, are one name ("LAST SEEN").
    out = merged[:1]
    for s, e in merged[1:]:
        if s - out[-1][1] == 1 and (empty(s, e) or empty(*out[-1])):
            out[-1][1] = e
        else:
            out.append([s, e])
    return [cut(head, s, e) for s, e in out], [[cut(line, s, e) for s, e in out] for line in body]


def _looks_like_table(header: list[str], rows: list[list[str]]) -> bool:
    """Whether columns found in free text are a real table: a header that names two or more columns, and
    rows under it."""
    if len(header) < 2 or not rows:
        return False
    if any(not name or not (name[0].isalpha() or name[0] in "%#(_") for name in header):
        return False  # a header names its columns: not "42", not "[INFO]"
    return all(row[0] for row in rows)


def _pick(columns: Sequence[str], rows: Sequence[Sequence[str]], wanted: Sequence[str]) -> Table:
    """Just the columns asked for, in that order; names match whatever their case."""
    columns = list(columns)
    if wanted:
        lower = [c.lower() for c in columns]
        for name in wanted:
            if name.lower() not in lower:
                there = ", ".join(columns) or "none"
                raise ValueError(f"no column {name!r}; the columns are {there}"[:300])
        index = [lower.index(name.lower()) for name in wanted]
        return Table(tuple(columns[i] for i in index), tuple(tuple(row[i] for i in index) for row in rows))
    return Table(tuple(columns), tuple(tuple(row) for row in rows))


def _flatten(obj: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """``{"metadata": {"name": "x"}}`` -> ``{"metadata.name": "x"}``."""
    out: dict[str, Any] = {}
    for key, value in obj.items():
        if isinstance(value, Mapping) and value:
            out.update(_flatten(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else f"{value:.6g}"
    if isinstance(value, list) and all(not isinstance(x, (list, dict)) for x in value):
        return ", ".join(map(_cell, value))
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _object_rows(doc: Any) -> list[Mapping[str, Any]] | None:
    """The rows in a list of objects, ``{"items": [...]}`` or one object, or None if doc isn't that."""
    if isinstance(doc, Mapping):
        items = doc.get("items")
        if isinstance(items, list) and all(isinstance(item, Mapping) for item in items):
            return items
        return [doc]
    if isinstance(doc, list) and all(isinstance(item, Mapping) for item in doc):
        return doc
    return None


def _json_table(rows: Sequence[Mapping[str, Any]], wanted: Sequence[str], at: float) -> Frame:
    flat = [_flatten(row) for row in rows]
    columns = list(dict.fromkeys(key for row in flat for key in row))
    return Frame(table=_pick(columns, [[_cell(row.get(c)) for c in columns] for row in flat], wanted), at=at)


# ------------------------------------------------------------------------------------- Prometheus-style


def _is_series_list(doc: Any) -> bool:
    return isinstance(doc, list) and bool(doc) and all(
        isinstance(s, Mapping) and isinstance(s.get("metric", {}), Mapping) and ("value" in s or "values" in s)
        for s in doc)


#: The keys of a query API's response around its data.
_RESPONSE_KEYS = {"status", "data", "warnings", "infos"}


def _prom(doc: Any) -> tuple[str, Any] | None:
    """``(resultType, result)`` from Prometheus-style JSON (a whole API response, its ``data``, or a bare
    list of series), or None if doc isn't that. A response with ``"status": "error"`` raises."""
    if isinstance(doc, Mapping):
        if doc.get("status") == "error":
            raise ValueError(f"query failed: {doc.get('error') or doc.get('errorType') or 'error'}"[:300])
        data = doc.get("data")
        if isinstance(data, Mapping) and ("resultType" in data or "result" in data):
            doc = data
        elif isinstance(data, list) and (_is_series_list(data) or (not data and set(doc) <= _RESPONSE_KEYS)):
            doc = {"result": data}  # the series straight under "data"
        if "resultType" in doc:
            kind, result = doc["resultType"], doc.get("result")
            if kind in ("scalar", "string"):
                return kind, result
            if kind not in ("matrix", "vector") or not isinstance(result, list):
                raise ValueError(f"Prometheus-style JSON with a resultType of {kind!r} and no result list")
            return kind, result
        result = doc.get("result", ())
        if result is None or (isinstance(result, list) and (not result or _is_series_list(result))):
            doc = result or []
        else:
            return None
    if isinstance(doc, list) and (not doc or _is_series_list(doc)):
        return ("matrix" if any("values" in s for s in doc) else "vector"), doc
    return None


def _prom_value(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"not Prometheus-style JSON: a value of {value!r}"[:300]) from None
    return number if math.isfinite(number) else math.nan


def _point(pair: Any) -> tuple[float, float]:
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        raise ValueError(f"not Prometheus-style JSON: a point of {pair!r}"[:300])
    try:
        when = float(pair[0])
    except (TypeError, ValueError):
        raise ValueError(f"not Prometheus-style JSON: a time of {pair[0]!r}"[:300]) from None
    return when, _prom_value(pair[1])


def series_names(metrics: Sequence[Mapping[str, Any]]) -> list[str]:
    """Names for series by their labels: the labels that differ between them (``a``, or ``a, api`` for
    several), or for one series its ``__name__``, or its only label. Names are made unique."""
    if len(metrics) == 1:
        metric = metrics[0]
        if metric.get("__name__"):
            names = [str(metric["__name__"])]
        elif len(metric) == 1:
            names = [str(next(iter(metric.values())))]
        else:
            names = [""]
    else:
        keys = sorted(k for k in set().union(*metrics) if len({str(m.get(k)) for m in metrics}) > 1)
        names = [", ".join(str(m[k]) for k in keys if k in m) for m in metrics]
    seen: dict[str, int] = {}
    unique = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        unique.append(name if seen[name] == 1 else f"{name} ({seen[name]})".strip())
    return unique


def _prom_table(result: Sequence[Mapping[str, Any]], wanted: Sequence[str], at: float) -> Frame:
    """A row per series: a column for each label that tells them apart (every label, for one series), and
    the value, unless it's the same in every row (as with ``== 1``)."""
    if not result:
        return Frame(at=at)
    metrics = [s.get("metric") or {} for s in result]
    labels = list(dict.fromkeys(k for m in metrics for k in m))
    if len(result) > 1:
        labels = [k for k in labels if len({str(m.get(k)) for m in metrics}) > 1]

    def value(s: Mapping[str, Any]) -> str:
        points = s.get("values") or [s.get("value")]
        return fmt_number(_point(points[-1])[1]) if points and points[-1] is not None else ""

    values = [value(s) for s in result]
    show = len(result) < 2 or len(set(values)) > 1
    columns = ["metric" if k == "__name__" else k for k in labels] + (["value"] if show else [])
    rows = [[str(m.get(k, "")) for k in labels] + ([v] if show else []) for m, v in zip(metrics, values, strict=True)]
    return Frame(table=_pick(columns, rows, wanted), at=at)


def _prom_frame(result_type: str, result: Any, kind: str | None, wanted: Sequence[str], at: float) -> Frame:
    if result_type == "string":
        text = result[1] if isinstance(result, list) and len(result) == 2 else result
        return _text_frame(str(text), at)
    if result_type == "scalar":
        if kind == "table":
            return Frame(table=_pick(["value"], [[fmt_number(_point(result)[1])]], wanted), at=at)
        return Frame(series=(Series("", ((at, _point(result)[1]),)),), at=at)
    if not all(isinstance(s, Mapping) for s in result):
        raise ValueError("not Prometheus-style JSON: a result that isn't a list of series")
    if kind == "table":
        return _prom_table(result, wanted, at)
    result = result[:MAX_SERIES]
    names = series_names([s.get("metric") or {} for s in result])
    series = []
    for name, s in zip(names, result, strict=True):
        if result_type == "matrix":
            if not isinstance(s.get("values"), list):
                raise ValueError("not Prometheus-style JSON: a matrix series without values")
            points = tuple(_point(p) for p in s["values"])
        else:
            # A single reading gets the run's time, like every other single reading.
            points = ((at, _point(s.get("value"))[1]),)
        series.append(Series(name, points))
    return Frame(series=tuple(series), at=at)


# ------------------------------------------------------------------------------------------------- JSON


def _from_json(doc: Any, kind: str | None, wanted: Sequence[str], at: float, original: str | None) -> Frame:
    prom = _prom(doc)
    if prom is not None:
        return _prom_frame(*prom, kind, wanted, at)
    if kind != "table":
        if _is_number(doc):
            return _readings([("", float(doc))], at)
        if isinstance(doc, Mapping) and doc and all(_is_number(v) for v in doc.values()):
            return _readings([(str(k), float(v)) for k, v in doc.items()], at)
    if isinstance(doc, list) and not doc:
        return Frame(at=at)
    rows = _object_rows(doc)
    if rows is not None:
        return _json_table(rows, wanted, at)
    if kind == "table":
        raise ValueError("for a table, the JSON is a list of objects, one per row")
    return _text_frame(original if original is not None else json.dumps(doc, indent=2), at)


def _load_json(text: str) -> Any:
    """The JSON value in text, or a list of the values on its lines (one object per line)."""
    try:
        return json.loads(text)
    except ValueError as error:
        first = error
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        try:
            return [json.loads(line) for line in lines]
        except ValueError:
            pass
    raise ValueError(f"bad JSON: {first}"[:300])


def _looks_like_json(text: str) -> bool:
    """An object (or objects, one per line), or a list. Text in brackets that only starts with ``[`` is a
    log line ("[INFO] ..."), not JSON."""
    return text[:1] == "{" or (text[:1] == "[" and text[-1:] == "]")


# ------------------------------------------------------------------------------------------------ parse


def parse(text: str, kind: Kind | str | None = None, columns: Sequence[str] = (),
          at: float | datetime | None = None) -> Frame:
    """Read a command's output. ``kind`` "text" keeps it as text; "table" reads only JSON and columns.
    ``columns`` picks a table's columns by name (any case), in that order. Single readings get the time
    ``at`` (unix seconds or a datetime; default now). Raises ``ValueError`` with a one-line message."""
    when = _now(at)
    laid_out = _clean(text)  # keeps the first line's indentation: a right-aligned first column lines up with it
    text = laid_out.strip()
    if not text:
        return Frame(at=when)
    if kind == "text":
        return _text_frame(laid_out, when)
    if _looks_like_json(text):
        return _from_json(_load_json(text), kind, columns, when, text)
    if kind != "table":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) == 1:
            match = _ONE_NUMBER.fullmatch(lines[0])
            if match:
                return _readings([("", _number(match.group(1)))], when)
        matches = [_LABEL_LINE.fullmatch(line) for line in lines]
        if all(matches):
            return _readings([(m.group(1) or "", _number(m.group(2))) for m in matches if m], when)
    header, rows = parse_columns(laid_out)
    if kind == "table":
        if not header or any(_NUMERIC_CELL.fullmatch(name) for name in header):
            raise ValueError("for a table, print JSON or text in columns under a header line")
        return Frame(table=_pick(header, rows, columns), at=when)
    if _looks_like_table(header, rows):
        return Frame(table=_pick(header, rows, columns), at=when)
    return _text_frame(laid_out, when)


def parse_data(value: Any, kind: Kind | str | None = None, columns: Sequence[str] = (),
               at: float | datetime | None = None) -> Frame:
    """Read inline data: any JSON value, the same way as JSON output. A string is read like output."""
    if isinstance(value, str):
        return parse(value, kind=kind, columns=columns, at=at)
    when = _now(at)
    if value is None:
        return Frame(at=when)
    if kind == "text":
        return _text_frame(json.dumps(value, indent=2), when)
    return _from_json(value, kind, columns, when, None)


# ------------------------------------------------------------------------------------- kind, describe


def guess_kind(frame: Frame) -> Kind:
    """The kind that shows a frame best: a table, text, a time series for history, a stat for one
    reading, bars for several readings. An empty frame is a stat."""
    if frame.shape == "table":
        return "table"
    if frame.shape == "text":
        return "text"
    if frame.history:
        return "timeseries"
    if len(frame.series) > 1:
        return "bars"
    return "stat"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def describe(frame: Frame, unit: str = "") -> str:
    """One line about a frame, for a CLI to print: ``3 series, 240 points, last 212 ms``, ``1 reading: 42``,
    ``table: 5 columns, 12 rows``, ``text: 8 lines``, ``nothing (No results found.)``."""
    if frame.shape == "series":
        if frame.history:
            points = sum(len(s.points) for s in frame.series)
            last = _with_unit(fmt_number(frame.series[0].last), unit)
            return f"{len(frame.series)} series, {_plural(points, 'point')}, last {last}"
        readings = [
            f"{s.name} {_with_unit(fmt_number(s.last), unit)}".strip() for s in frame.series]
        shown = ", ".join(readings[:6]) + (", …" if len(readings) > 6 else "")
        return f"{_plural(len(readings), 'reading')}: {shown}"
    if frame.shape == "table" and frame.table is not None:
        table = frame.table
        return f"table: {_plural(len(table.columns), 'column')}, {_plural(len(table.rows), 'row')}"
    if frame.shape == "text" and frame.text is not None:
        return f"text: {_plural(len(frame.text.splitlines()), 'line')}"
    return f"nothing ({frame.note})" if frame.note else "nothing"
