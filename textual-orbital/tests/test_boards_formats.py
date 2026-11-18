"""Output formats -> frames, the kind a frame suggests, and the one-line description."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from textual_orbital.boards.formats import describe, guess_kind, parse, parse_data
from textual_orbital.boards.model import MAX_SERIES, Frame, Series, Table

#: The boards example's fake data commands.
FEED = Path(__file__).resolve().parents[1] / "examples" / "boards" / "boards" / "feed.py"
AT = 1_800_000_000.0


def feed(*args: str) -> str:
    return subprocess.run([sys.executable, str(FEED), *args, "--at", str(int(AT))], capture_output=True,
                          text=True, check=True, timeout=30).stdout


def prom(result_type: str, result: Any, **outer: Any) -> str:
    return json.dumps({"status": "success", "data": {"resultType": result_type, "result": result}, **outer})


def names(frame: Frame) -> list[str]:
    return [s.name for s in frame.series]


def readings(frame: Frame) -> list[tuple[str, float]]:
    assert all(len(s.points) == 1 and s.points[0][0] == AT for s in frame.series)
    return [(s.name, s.points[0][1]) for s in frame.series]


# ------------------------------------------------------------------------------- Prometheus-style JSON


def test_a_range_result_is_series_with_history_named_by_the_label_that_differs() -> None:
    frame = parse(feed("series", "api", "web", "db"), at=AT)
    assert names(frame) == ["api", "web", "db"]
    assert frame.history and frame.at == AT
    points = frame.series[0].points
    assert len(points) == 61 and points[-1][0] == AT and points[0][0] == AT - 900
    assert all(isinstance(v, float) for _, v in points)


def test_series_names_use_the_labels_that_differ_or_the_name_or_the_only_label() -> None:
    def series_named(*metrics: dict[str, str]) -> list[str]:
        result = [{"metric": m, "values": [[AT, "1"], [AT + 15, "2"]]} for m in metrics]
        return names(parse(prom("matrix", result), at=AT))

    assert series_named({"job": "api", "instance": "a"}, {"job": "api", "instance": "b"}) == ["a", "b"]
    assert series_named({"job": "api", "instance": "a"}, {"job": "web", "instance": "b"}) == ["a, api", "b, web"]
    assert series_named({"__name__": "up", "job": "api"}) == ["up"]
    assert series_named({"job": "api"}) == ["api"]
    assert series_named({"job": "api", "instance": "a"}) == [""]
    assert series_named({}) == [""]
    assert series_named({"job": "x"}, {"job": "x"}) == ["", "(2)"], "identical labels: still unique"


def test_nan_and_infinities_are_gaps() -> None:
    result = [{"metric": {}, "values": [[AT, "1"], [AT + 15, "NaN"], [AT + 30, "+Inf"], [AT + 45, "-Inf"],
                                        [AT + 60, "2.5"]]}]
    values = [v for _, v in parse(prom("matrix", result)).series[0].points]
    assert values[0] == 1 and values[-1] == 2.5 and all(math.isnan(v) for v in values[1:4])


def test_at_most_max_series_are_kept_the_first_ones() -> None:
    result = [{"metric": {"instance": f"i{n}"}, "values": [[AT, "1"], [AT + 15, "2"]]} for n in range(12)]
    assert names(parse(prom("matrix", result))) == [f"i{n}" for n in range(MAX_SERIES)]


def test_an_instant_result_is_one_reading_per_series_at_the_runs_time() -> None:
    result = [{"metric": {"instance": "a"}, "value": [1_700_000_000, "4.2"]},
              {"metric": {"instance": "b"}, "value": [1_700_000_000, "9"]}]
    frame = parse(prom("vector", result), at=AT)
    assert readings(frame) == [("a", 4.2), ("b", 9.0)] and not frame.history


def test_an_instant_result_as_a_table_is_a_row_per_series() -> None:
    result = [{"metric": {"__name__": "up", "job": "api", "instance": "a"}, "value": [AT, "1"]},
              {"metric": {"__name__": "up", "job": "api", "instance": "b"}, "value": [AT, "0"]}]
    frame = parse(prom("vector", result), kind="table")
    assert frame.table == Table(("instance", "value"), (("a", "1"), ("b", "0")))
    one = parse(prom("vector", result[:1]), kind="table")
    assert one.table is not None and one.table.columns == ("metric", "job", "instance", "value")
    same = [{**s, "value": [AT, "1"]} for s in result]  # like `== 1`: the value says nothing
    assert parse(prom("vector", same), kind="table").table == Table(("instance",), (("a",), ("b",)))


def test_scalar_string_and_empty_results() -> None:
    assert readings(parse(prom("scalar", [AT, "42"]), at=AT)) == [("", 42.0)]
    assert parse(prom("string", [AT, "hello"])).text == "hello"
    assert parse(prom("vector", [])).shape == "empty"


def test_the_bare_data_object_and_bare_series_lists_work_too() -> None:
    """A query CLI's ``-o json`` may print the whole response, its ``data``, or just the series."""
    series = [{"metric": {"instance": "a"}, "values": [[AT, "1"], [AT + 15, "2"]]}]
    expected = parse(prom("matrix", series))
    for text in (json.dumps({"resultType": "matrix", "result": series}),
                 json.dumps({"data": {"result": series}}),
                 json.dumps({"data": series}),
                 json.dumps({"result": series}),
                 json.dumps(series)):
        assert parse(text) == expected, text
    empties: list[Any] = [{"data": {"result": None}}, {"status": "success", "data": []}, {"result": []}, []]
    for empty in empties:
        assert parse(json.dumps(empty)).shape == "empty", empty
    # A list of other objects under "data" is someone's API, not series: a table.
    assert parse(json.dumps({"data": [{"name": "x"}]})).shape == "table"
    instant = [{"metric": {"instance": "a"}, "value": [AT, "3"]}]
    assert readings(parse(json.dumps(instant), at=AT)) == [("a", 3.0)]


def test_an_error_response_or_a_broken_result_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="query failed: parse error at char 5"):
        parse(json.dumps({"status": "error", "errorType": "bad_data", "error": "parse error at char 5"}))
    with pytest.raises(ValueError, match="not Prometheus-style JSON"):
        parse(prom("matrix", [{"metric": {}, "values": [[AT, "lots"]]}]))
    with pytest.raises(ValueError, match="not Prometheus-style JSON"):
        parse(prom("matrix", [{"metric": {}, "values": [AT]}]))


# ------------------------------------------------------------------------------------------- JSON


def test_a_list_of_objects_is_a_table_with_nested_fields_as_dotted_columns() -> None:
    doc = [{"name": "a", "status": {"phase": "Running", "ok": True}, "n": 1.0, "tags": ["x", "y"]},
           {"name": "b", "status": {"phase": "Pending", "ok": False}, "extra": None}]
    frame = parse(json.dumps(doc))
    assert frame.table == Table(
        ("name", "status.phase", "status.ok", "n", "tags", "extra"),
        (("a", "Running", "true", "1", "x, y", ""), ("b", "Pending", "false", "", "", "")))


def test_objects_one_per_line_and_items_lists_are_tables() -> None:
    lines = '{"Names": "web", "State": "running"}\n{"Names": "db", "State": "exited"}\n'
    assert parse(lines).table == Table(("Names", "State"), (("web", "running"), ("db", "exited")))
    items = {"total": 2, "items": [{"owner": {"name": "p1"}}, {"owner": {"name": "p2"}}]}
    assert parse(json.dumps(items)).table == Table(("owner.name",), (("p1",), ("p2",)))


def test_columns_are_picked_by_name_in_any_case_in_the_order_asked() -> None:
    doc = [{"NAME": "a", "READY": "1/1", "STATUS": "Running"}]
    assert parse(json.dumps(doc), columns=("status", "Name")).table == Table(("STATUS", "NAME"), (("Running", "a"),))
    with pytest.raises(ValueError, match="no column 'age'; the columns are NAME, READY, STATUS"):
        parse(json.dumps(doc), columns=("age",))


def test_json_numbers_are_readings() -> None:
    assert readings(parse("42", at=AT)) == [("", 42.0)]
    assert readings(parse('{"api": 4.2, "web": 9, "db": 1}', at=AT)) == [("api", 4.2), ("web", 9.0), ("db", 1.0)]
    assert parse("[]").shape == "empty"


def test_other_json_is_text_and_broken_json_is_an_error() -> None:
    assert parse('["a", "b"]').text == '["a", "b"]'
    assert parse("[INFO] starting server\n[WARN] disk at 91%").shape == "text"
    for broken in ('{"a": ', '[1, 2,]', '{"a": 1}\n{"b": '):
        with pytest.raises(ValueError, match="bad JSON"):
            parse(broken)
    with pytest.raises(ValueError, match="list of objects"):
        parse('["a"]', kind="table")


# ----------------------------------------------------------------------------------- numbers and labels


@pytest.mark.parametrize("text, value", [
    ("42", 42.0), ("  4.2 ms\n", 4.2), ("99%", 99.0), ("-3.5e2", -350.0), ("1,234 req/s", 1234.0), (".5", 0.5),
])
def test_one_number_with_an_optional_unit(text: str, value: float) -> None:
    assert readings(parse(text, at=AT)) == [("", value)]


def test_label_number_lines_are_one_reading_per_label_in_order() -> None:
    text = "api 4.2\nweb server   9\n\ndb -1\nload 99%\n"
    assert readings(parse(text, at=AT)) == [("api", 4.2), ("web server", 9.0), ("db", -1.0), ("load", 99.0)]
    assert [name for name, _ in readings(parse(feed("labels", "a", "b", "c"), at=AT))] == ["a", "b", "c"]


def test_label_lines_keep_at_most_max_series() -> None:
    text = "\n".join(f"s{n} {n}" for n in range(12))
    assert len(parse(text).series) == MAX_SERIES


def test_words_that_end_in_digits_are_not_label_lines() -> None:
    assert parse("svc/api-1\nsvc/api-2").shape == "text"


def test_the_time_may_be_a_datetime() -> None:
    when = datetime.fromtimestamp(AT, timezone.utc)
    assert readings(parse("7", at=when)) == [("", 7.0)]


# ----------------------------------------------------------------------------------------- columns


def test_a_services_table_reads_into_its_columns() -> None:
    frame = parse(feed("table"))
    assert frame.table is not None
    assert frame.table.columns == ("NAME", "VERSION", "STATUS", "RESTARTS", "UPTIME")
    assert len(frame.table.rows) == 6
    assert frame.table.rows[-1][:3] == ("db", "16.4", "Running")


def test_values_with_spaces_and_two_word_headers_stay_whole() -> None:
    text = ("CONTAINER ID   IMAGE    STATUS         NAMES\n"
            "4f1c2a9e7b3d   nginx    Up 2 hours     web\n"
            "9a8b7c6d5e4f   redis    Exited (0) 3   cache\n")
    frame = parse(text)
    assert frame.table == Table(("CONTAINER ID", "IMAGE", "STATUS", "NAMES"),
                                (("4f1c2a9e7b3d", "nginx", "Up 2 hours", "web"),
                                 ("9a8b7c6d5e4f", "redis", "Exited (0) 3", "cache")))
    df = ("Filesystem     Size   Used  Avail Capacity  Mounted on\n"
          "/dev/disk3s1  460Gi   10Gi  200Gi       5%  /\n"
          "devfs         200Ki  200Ki    0Bi     100%  /dev\n")
    table = parse(df).table
    assert table is not None and table.columns[-1] == "Mounted on" and table.rows[1][-1] == "/dev"


def test_tab_separated_text_is_a_table() -> None:
    assert parse("name\tstate\na b\tok\nc\t\n").table == Table(("name", "state"), (("a b", "ok"), ("c", "")))


def test_escapes_are_dropped() -> None:
    assert readings(parse("\x1b[32m42\x1b[0m\n", at=AT)) == [("", 42.0)]


# ------------------------------------------------------------------------------------------- text, kinds


def test_anything_else_is_text() -> None:
    text = "\n\nError: something went wrong\n  at line 3\n\n"
    assert parse(text) == Frame(text="Error: something went wrong\n  at line 3")
    assert parse("hello world\nthe end").shape == "text"
    assert parse("   \n").shape == "empty"


def test_kind_text_keeps_even_numbers_as_text() -> None:
    assert parse("42", kind="text").text == "42"
    assert parse('{"a": 1}', kind="text").text == '{"a": 1}'


def test_kind_table_reads_only_json_and_columns() -> None:
    assert parse("NAME  COUNT\napi   4\nweb   5", kind="table").table == Table(("NAME", "COUNT"),
                                                                            (("api", "4"), ("web", "5")))
    assert parse("CONTAINER ID   IMAGE", kind="table").table == Table(("CONTAINER ID", "IMAGE"), ())
    for text in ("42", "api 4\nweb 5"):
        with pytest.raises(ValueError, match="for a table, print JSON or text in columns under a header line"):
            parse(text, kind="table")


# ------------------------------------------------------------------------------------------- parse_data


def test_inline_data_reads_like_json_output() -> None:
    assert readings(parse_data(42, at=AT)) == [("", 42.0)]
    assert readings(parse_data({"a": 1, "b": 2}, at=AT)) == [("a", 1.0), ("b", 2.0)]
    assert parse_data([{"name": "x"}]).table == Table(("name",), (("x",),))
    assert readings(parse_data("api 4\nweb 5", at=AT)) == [("api", 4.0), ("web", 5.0)]
    assert parse_data("Failover started at 14:02.").text == "Failover started at 14:02."
    assert parse_data({"a": 1}, kind="text").text == '{\n  "a": 1\n}'
    assert parse_data([1, 2]).text == "[\n  1,\n  2\n]"
    series = [{"metric": {"instance": "a"}, "values": [[AT, "1"], [AT + 15, "2"]]}]
    assert parse_data({"status": "success", "data": {"resultType": "matrix", "result": series}}).history


# ------------------------------------------------------------------------------------- guess and describe


def test_guess_kind() -> None:
    one = Frame(series=(Series("", ((AT, 1.0),)),))
    several = Frame(series=(Series("a", ((AT, 1.0),)), Series("b", ((AT, 2.0),))))
    history = Frame(series=(Series("a", ((AT, 1.0), (AT + 15, 2.0))),))
    assert guess_kind(one) == "stat"
    assert guess_kind(several) == "bars"
    assert guess_kind(history) == "timeseries"
    assert guess_kind(Frame(table=Table(("A",), ()))) == "table"
    assert guess_kind(Frame(text="x")) == "text"
    assert guess_kind(Frame()) == "stat"


def test_describe() -> None:
    history = parse(feed("series", "a", "b", "c"), at=AT)
    last = history.series[0].last
    assert describe(history) == f"3 series, 183 points, last {last:.3g}"
    assert describe(history, "ms").endswith(" ms")
    assert describe(parse("42")) == "1 reading: 42"
    assert describe(parse("99"), "%") == "1 reading: 99%"
    assert describe(parse("212.4"), "ms") == "1 reading: 212 ms"
    assert describe(parse("api 4.2\nweb 12900")) == "2 readings: api 4.2, web 12.9K"
    assert describe(parse(feed("table"))) == "table: 5 columns, 6 rows"
    assert describe(Frame(table=Table(("A",), (("1",),)))) == "table: 1 column, 1 row"
    assert describe(parse("a\nb\nc d e f g\n")) == "text: 3 lines"
    assert describe(Frame()) == "nothing"
    assert describe(Frame(note="No results found.")) == "nothing (No results found.)"
    gaps = Frame(series=(Series("", ((AT, math.nan),)),))
    assert describe(gaps) == "1 reading: –"


def test_a_right_aligned_first_column_keeps_its_header() -> None:
    """ps and top right-align their first column, so the header line starts with spaces."""
    text = "  PID  %CPU COMM\n  616  52.7 /usr/sbin/ws\n53460  16.6 claude\n 7263   7.6 /Applications/Ghostty\n"
    frame = parse(text)
    assert frame.table is not None
    assert frame.table.columns == ("PID", "%CPU", "COMM")
    assert frame.table.rows[1] == ("53460", "16.6", "claude")
