"""The shared types: panel specs round-trip through JSON, bad ones are refused, and frames accumulate."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from textual_orbital.boards.model import (
    MAX_SERIES,
    Frame,
    PanelSpec,
    Series,
    SpecError,
    Table,
    accumulate,
    fmt_duration,
    parse_duration,
)

T0 = datetime(2026, 11, 17, 9, 30, tzinfo=timezone.utc)


def spec(**changes: object) -> PanelSpec:
    args: dict[str, object] = {"id": "3f9a0c1e", "board": "main", "title": "p95 latency", "kind": "timeseries",
                               "added_at": T0, "cmd": "echo 1"}
    args.update(changes)
    return PanelSpec(**args)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------- round trips


def test_a_full_spec_round_trips_through_json_text() -> None:
    full = spec(cwd="/tmp", unit="ms", every=10.0, window=3600.0, timeout=5.0, ymin=0.0, ymax=250.0,
                columns=("NAME", "STATUS"), key="lat", added_by="api", expires_at=T0 + timedelta(hours=2),
                updated_at=T0 + timedelta(minutes=5))
    text = json.dumps(full.to_json())
    back = PanelSpec.from_json(json.loads(text))
    assert back == full
    assert back.columns == ("NAME", "STATUS")
    assert back.changed_at == T0 + timedelta(minutes=5)


def test_to_json_writes_every_field_with_format_first() -> None:
    doc = spec().to_json()
    assert next(iter(doc)) == "format"
    assert doc["format"] == 1
    assert doc["added_at"] == T0.isoformat()
    assert doc["columns"] == [] and doc["expires_at"] is None and doc["data"] is None


def test_a_data_spec_round_trips_any_json_value() -> None:
    for data in (42, {"a": 1, "b": 2.5}, [{"name": "x"}], "api 4\nweb 5", [1, [2, None]]):
        s = spec(cmd=None, data=data, kind="stat")
        assert PanelSpec.from_json(json.loads(json.dumps(s.to_json()))) == s


def test_from_json_reads_z_times_and_ignores_unknown_keys() -> None:
    doc = spec().to_json()
    doc["added_at"] = "2026-11-17T09:30:00Z"
    doc["something_new"] = True
    assert PanelSpec.from_json(doc).added_at == T0


def test_changed_at_and_expired() -> None:
    s = spec(expires_at=T0 + timedelta(hours=1))
    assert s.changed_at == T0
    assert not s.expired(T0 + timedelta(minutes=59))
    assert s.expired(T0 + timedelta(hours=1))
    assert not spec().expired(T0 + timedelta(days=999))


# ---------------------------------------------------------------------------------------------- validation


@pytest.mark.parametrize("changes, message", [
    ({"id": "XYZ"}, "bad panel id"),
    ({"id": "3F9A0C1E"}, "bad panel id"),
    ({"board": "Main"}, "bad board name"),
    ({"board": "-x"}, "bad board name"),
    ({"board": "a" * 33}, "bad board name"),
    ({"kind": "pie"}, "bad kind"),
    ({"title": "  "}, "needs a title"),
    ({"cmd": None}, "exactly one of cmd and data"),
    ({"data": 1}, "exactly one of cmd and data"),
    ({"cmd": "  "}, "cmd is empty"),
    ({"every": 0}, "every must be"),
    ({"window": -1.0}, "window must be"),
    ({"timeout": math.inf}, "timeout must be"),
    ({"added_at": datetime(2026, 11, 17)}, "timezone-aware"),
    ({"expires_at": datetime(2026, 11, 17)}, "timezone-aware"),
])
def test_bad_fields_are_refused(changes: dict[str, object], message: str) -> None:
    with pytest.raises(SpecError, match=message):
        spec(**changes)


@pytest.mark.parametrize("change, message", [
    ({"format": 2}, "unknown panel format"),
    ({"title": 3}, "title must be text"),
    ({"cmd": 3}, "cmd must be text"),
    ({"every": "30s"}, "every must be a number"),
    ({"every": True}, "every must be a number"),
    ({"added_at": 12}, "ISO 8601"),
    ({"added_at": "yesterday"}, "added_at"),
    ({"added_at": "2026-11-17T09:30:00"}, "time zone"),
    ({"columns": "NAME"}, "columns must be a list"),
])
def test_bad_files_are_refused(change: dict[str, object], message: str) -> None:
    doc = spec().to_json()
    doc.update(change)
    with pytest.raises(SpecError, match=message):
        PanelSpec.from_json(doc)


def test_a_file_missing_fields_or_not_an_object_is_refused() -> None:
    with pytest.raises(SpecError, match="one JSON object"):
        PanelSpec.from_json([1, 2])
    doc = spec().to_json()
    del doc["title"], doc["kind"]
    with pytest.raises(SpecError, match="missing title, kind"):
        PanelSpec.from_json(doc)


# ---------------------------------------------------------------------------------------------- durations


def test_parse_duration() -> None:
    assert parse_duration("90") == 90
    assert parse_duration("90s") == 90
    assert parse_duration("15m") == 900
    assert parse_duration(" 2h ") == 7200
    assert parse_duration("1.5d") == 129600
    for bad in ("", "15 minutes", "-5m", "0s", "m"):
        with pytest.raises(SpecError):
            parse_duration(bad)


def test_fmt_duration() -> None:
    assert [fmt_duration(s) for s in (-3, 0, 45, 60, 900, 3599, 3600, 5400, 86400, 108000, 90061)] == [
        "0s", "0s", "45s", "1m", "15m", "59m", "1h", "1h30m", "1d", "1d6h", "1d1h"]


# ----------------------------------------------------------------------------------------------- frames


def test_frame_shapes_history_and_equality_ignores_at() -> None:
    assert Frame().shape == "empty"
    assert Frame(text="hi").shape == "text"
    assert Frame(table=Table(("A",), ())).shape == "table"
    one = Frame(series=(Series("", ((1.0, 2.0),)),), at=1.0)
    assert one.shape == "series" and not one.history
    assert Frame(series=(Series("", ((1.0, 2.0), (2.0, 3.0))),)).history
    assert one == Frame(series=(Series("", ((1.0, 2.0),)),), at=99.0)


def test_series_last_skips_gaps() -> None:
    assert Series("a", ((1.0, 4.0), (2.0, math.nan))).last == 4.0
    assert Series("a", ((1.0, math.nan),)).last is None
    assert Series("a", ()).last is None


def reading(at: float, **values: float) -> Frame:
    return Frame(series=tuple(Series(name, ((at, v),)) for name, v in values.items()), at=at)


def test_accumulate_adds_single_readings_by_name_and_ages_them_out() -> None:
    frame = accumulate(None, reading(100, a=1), window=60)
    frame = accumulate(frame, reading(130, a=2, b=5), window=60)
    assert [(s.name, s.points) for s in frame.series] == [("a", ((100, 1), (130, 2))), ("b", ((130, 5),))]
    frame = accumulate(frame, reading(170, b=6), window=60)
    # a's points are older than 170 - 60 = 110 except the one at 130; b gets its second point.
    assert [(s.name, s.points) for s in frame.series] == [("a", ((130, 2),)), ("b", ((130, 5), (170, 6)))]
    frame = accumulate(frame, reading(300, b=7), window=60)
    assert [(s.name, s.points) for s in frame.series] == [("b", ((300, 7),))]
    assert frame.at == 300


def test_accumulate_replaces_with_history_tables_text_and_empty() -> None:
    previous = reading(100, a=1)
    history = Frame(series=(Series("a", ((1.0, 1.0), (2.0, 2.0))),), at=110)
    for new in (history, Frame(text="x", at=110), Frame(table=Table(("A",), (("1",),)), at=110),
                Frame(note="nothing", at=110)):
        assert accumulate(previous, new, window=60) is new
    # And readings after a table start over.
    assert accumulate(Frame(text="x"), reading(120, a=3), window=60) == reading(120, a=3)


def test_accumulate_keeps_the_note_and_at_most_max_series() -> None:
    previous = reading(100, **{f"s{i}": i for i in range(MAX_SERIES)})
    new = Frame(series=(Series("extra", ((110, 1.0),)),), note="partial", at=110)
    frame = accumulate(previous, new, window=60)
    assert len(frame.series) == MAX_SERIES and "extra" not in [s.name for s in frame.series]
    assert frame.note == "partial"
