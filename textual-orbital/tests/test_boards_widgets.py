"""The panel widgets: every kind in every state (loading, data, error with and without data, empty), with and
without glass; the footer and its minute ticks; no redraw for equal data; and the helpers behind them."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from textual.app import App, ComposeResult
from textual.color import Color

from textual_glass import Glass
from textual_glass.engine import MAIN
from textual_glass.items import Area, HeaderBar, Lines, Meter, Ring
from textual_glass.testing.pilot_helpers import Output, settle, start
from textual_orbital import ORBITAL, PALETTE
from textual_orbital.boards.model import Frame, Kind, PanelSpec, Series, Table
from textual_orbital.boards.widgets import (
    BarsBody,
    PanelView,
    StatBody,
    TableBody,
    TextBody,
    fit_columns,
    fmt_value,
    footer_text,
    same_frame,
    status_colour,
)

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
T1 = T0.timestamp()
NAN = float("nan")


def spec(kind: Kind | str, title: str = "p95 latency", **fields: Any) -> PanelSpec:
    return PanelSpec(id="3f9a0c1e", board="main", title=title, kind=cast(Kind, kind), added_at=T0, cmd="echo 1",
                     **fields)


def wave(name: str, base: float, n: int = 40, gap: tuple[int, int] | None = None) -> Series:
    points = tuple((T1 - 15 * (n - 1 - i), NAN if gap and gap[0] <= i < gap[1] else base + 10 * math.sin(i / 5))
                   for i in range(n))
    return Series(name, points)


SERVICES = Table(("NAME", "VERSION", "STATUS", "RESTARTS", "UPTIME"), (
    ("checkout-api.eu-west.blue-01", "2.14.1", "Running", "0", "3h"),
    ("checkout-worker.canary-02", "2.15.0", "Unhealthy", "7", "41m"),
    ("search.eu-west.green-01", "1.8.0", "Pending", "0", "2m"),
))

#: A frame for each kind, and text its body must show.
DATA: dict[str, tuple[Frame, dict[str, Any], tuple[str, ...]]] = {
    "timeseries": (Frame(series=(wave("api", 180), wave("web", 120, gap=(5, 9))), at=T1), {"unit": "ms"},
                   ("● api", "● web", "now")),
    "stat": (Frame(series=(wave("", 1200),), at=T1), {"unit": "req/s"}, ("req/s", "min ", "max ")),
    "gauge": (Frame(series=(Series("", ((T1, 72.4),)),), at=T1), {"unit": "%", "ymax": 100.0}, ("72.4%",)),
    "bars": (Frame(series=(Series("billing", ((T1, 42.0),)), Series("search", ((T1, 87.0),)),
                           Series("auth", ((T1, 12.0),))), at=T1), {}, ("search", "billing", "auth", "87")),
    "table": (Frame(table=SERVICES, at=T1), {}, ("NAME", "STATUS", "Running", "Unhealthy", "Pending")),
    "text": (Frame(text="12:01 deploy started\n12:03 canary 5%\n12:09 canary 25%", at=T1), {},
             ("12:09 canary 25%",)),
}
ITEMS: dict[str, type | None] = {"timeseries": Lines, "stat": Area, "gauge": Ring, "bars": Meter, "table": HeaderBar,
                                 "text": None}
STATES = ["loading", "data", "error", "error-without-data", "empty", "empty-without-note"]


class PanelApp(Glass, App[None]):
    CSS = "PanelView { width: 60; height: 14; }"

    def __init__(self, view: PanelView, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.view = view

    def compose(self) -> ComposeResult:
        yield self.view

    def on_mount(self) -> None:
        self.register_theme(ORBITAL)
        self.theme = "orbital"


def screen_text(app: App[None]) -> str:
    """Everything the panel's widgets render (their own lines), one widget per line."""
    out = []
    for widget in app.query("*"):
        if widget.display and hasattr(widget, "render_line") and widget.size.height:
            if isinstance(widget, (PanelView,)):
                continue
            try:
                out += [widget.render_line(y).text for y in range(widget.size.height)]
            except Exception:  # noqa: BLE001 - containers have no lines of their own
                continue
    return "\n".join(out)


def state_args(kind: str, state: str) -> tuple[Frame | None, str | None]:
    frame = DATA[kind][0]
    return {
        "loading": (None, None),
        "data": (frame, None),
        "error": (frame, "connection refused: db:5432"),
        "error-without-data": (None, "exited with status 2: mytool: command not found"),
        "empty": (Frame(note="No results found.", at=T1), None),
        "empty-without-note": (Frame(at=T1), None),
    }[state]


@pytest.mark.parametrize("glass", [True, False], ids=["glass", "plain"])
@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("kind", list(DATA))
async def test_every_kind_in_every_state(kind: str, state: str, glass: bool, output: Output) -> None:
    frame, fields, expected = DATA[kind]
    view = PanelView(spec(kind, **fields), at=T0)
    shown_frame, error = state_args(kind, state)
    view.set_frame(shown_frame, error)
    app = PanelApp(view, glass=None if glass else False)
    async with app.run_test(size=(90, 28)) as pilot:
        if glass:
            await start(app, pilot)
        else:
            await pilot.pause()
        text = screen_text(app)
        title = str(view.border_title)
        assert "p95 latency" in title
        body_shown, message_shown, error_shown = view.body.display, view.message.display, view.error_line.display
        if state == "loading":
            assert (body_shown, message_shown, error_shown) == (False, True, False)
            assert view.message.text == "loading…" and view.message.kind == "faint"
        elif state in ("data", "error"):
            assert body_shown and not message_shown
            for word in expected:
                assert word in text, (word, text)
            item = ITEMS[kind]
            if glass and item is not None:
                items = [i for i in app.glass.layers[MAIN].dynamic + app.glass.layers[MAIN].static
                         if isinstance(i, item)]
                assert items, f"{kind}: a {item.__name__} item with glass"
            if state == "error":
                assert error_shown and "✕ connection refused: db:5432" in text, "the error on the bottom line"
                if isinstance(view.body, (BarsBody, TableBody, TextBody)):
                    assert view.body.dim, "the last good data is dimmed"
                assert "✕" in title, "and a mark in the title"
            else:
                assert not error_shown and "✕" not in title
        elif state == "error-without-data":
            assert (body_shown, message_shown, error_shown) == (False, True, False)
            assert view.message.kind == "error" and "mytool: command not found" in view.message.text
        else:
            assert (body_shown, message_shown) == (False, True)
            assert view.message.text == ("No results found." if state == "empty" else "nothing")
        top = view.styles.border_top[1]
        red = Color.parse(PALETTE["red"])
        assert (top == red) == (error is not None), "the border turns red while the panel fails"
        if glass:
            await settle(app, pilot)
            assert output.model().errors == []
            assert not app.glass.errors


async def test_a_failing_panel_keeps_its_data_dimmed_and_recovers(output: Output) -> None:
    frame = DATA["timeseries"][0]
    view = PanelView(spec("timeseries", unit="ms"), at=T0)
    app = PanelApp(view)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        view.set_frame(frame)
        await settle(app, pilot)
        bright = next(i for i in app.glass.layers[MAIN].dynamic if isinstance(i, Lines))
        view.set_frame(frame, "timed out after 20 s")
        await settle(app, pilot)
        dim = next(i for i in app.glass.layers[MAIN].dynamic if isinstance(i, Lines))
        assert dim.series == bright.series, "the last good data stays"
        assert dim.colors != bright.colors, "dimmed"
        view.set_frame(frame)
        await settle(app, pilot)
        again = next(i for i in app.glass.layers[MAIN].dynamic if isinstance(i, Lines))
        assert again.colors == bright.colors and not view.error_line.display
        assert view.styles.border_top[1] != Color.parse(PALETTE["red"])
    assert output.model().errors == []


async def test_equal_frames_redraw_nothing(output: Output, monkeypatch: pytest.MonkeyPatch) -> None:
    for kind in DATA:
        frame, fields, _ = DATA[kind]
        view = PanelView(spec(kind, **fields), at=T0)
        app = PanelApp(view)
        async with app.run_test(size=(90, 28)) as pilot:
            await start(app, pilot)
            view.set_frame(frame)
            await settle(app, pilot)
            calls: list[str] = []

            def spy(widget: Any, record: list[str] = calls) -> None:
                real = widget.refresh

                def refresh(*args: Any, **kwargs: Any) -> Any:
                    record.append(type(widget).__name__)
                    return real(*args, **kwargs)

                monkeypatch.setattr(widget, "refresh", refresh)

            for widget in [view, *view.query("*")]:
                spy(widget)
            mark = output.mark()
            again = Frame(series=tuple(Series(s.name, tuple((t, float(v)) for t, v in s.points)) for s in frame.series),
                          table=frame.table, text=frame.text, note=frame.note, at=frame.at + 30)
            view.set_frame(again)
            view.set_frame(again, None)
            await settle(app, pilot)
            assert calls == [], (kind, calls)
            assert output.since(mark) == "", kind
            monkeypatch.undo()


async def test_the_footer_ticks_at_minute_resolution() -> None:
    view = PanelView(spec("gauge", unit="%", added_by="checkout-api", expires_at=T0 + timedelta(hours=1, minutes=20)),
                     at=T0)
    app = PanelApp(view, glass=False)
    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        assert view.border_subtitle == "checkout-api · % · 1h20m left"
        view.tick(T0 + timedelta(seconds=30))
        assert view.border_subtitle == "checkout-api · % · 1h19m left"
        view.tick(T0 + timedelta(minutes=79, seconds=30))
        assert view.border_subtitle == "checkout-api · % · <1m left"
    assert footer_text(spec("text"), T0) == "", "only the parts that exist"
    assert footer_text(spec("text", added_by="infra"), T0) == "infra"
    assert footer_text(spec("text", expires_at=T0 + timedelta(days=1, hours=6)), T0) == "1d6h left"


async def test_rows_that_dont_fit_say_how_many_more() -> None:
    bars = Frame(series=tuple(Series(f"queue-{k}", ((T1, float(k)),)) for k in range(20)), at=T1)
    table = Frame(table=Table(("NAME", "STATUS"), tuple((f"svc-{k}", "Running") for k in range(30))), at=T1)
    text = Frame(text="\n".join(f"line {k}" for k in range(40)), at=T1)
    for kind, frame in (("bars", bars), ("table", table), ("text", text)):
        view = PanelView(spec(kind), at=T0)
        view.set_frame(frame)
        app = PanelApp(view, glass=False)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            lines = [view.body.render_line(y).text.rstrip() for y in range(view.body.size.height)]
            if kind == "bars":
                assert lines[0].startswith("queue-19"), "biggest first"
                assert isinstance(view.body, BarsBody) and lines[-1].endswith("more") and lines[-1].startswith("+")
            elif kind == "table":
                assert isinstance(view.body, TableBody) and lines[0].startswith("NAME")
                assert lines[-1] == f"+{30 - (len(lines) - 2)} more"
            else:
                assert isinstance(view.body, TextBody) and lines[-1] == "line 39", "the last lines that fit"


async def test_a_stat_with_one_reading_has_no_sparkline() -> None:
    view = PanelView(spec("stat", unit="ms"), at=T0)
    view.set_frame(Frame(series=(Series("", ((T1, 4.2),)),), at=T1))
    app = PanelApp(view, glass=False)
    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        body = view.body
        assert isinstance(body, StatBody)
        assert body.value.render_line(0).text.startswith("4.2 ms")
        assert not body.spark.display and body.range_line.text.startswith("first reading")


async def test_a_shape_the_kind_cant_draw_says_so() -> None:
    view = PanelView(spec("gauge"), at=T0)
    view.set_frame(Frame(table=SERVICES, at=T1))
    app = PanelApp(view, glass=False)
    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        assert view.message.display and "no numbers" in view.message.text


# -- helpers ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "unit", "text"), [
    (42, "", "42"), (4.2, "ms", "4.2 ms"), (99.53, "%", "99.5%"), (1234.4, "req/s", "1,234 req/s"),
    (3_400_000, "", "3.4M"), (0.0421, "", "0.0421"), (None, "", "–"), (NAN, "ms", "–"), (0, "", "0"),
])
def test_fmt_value(value: float | None, unit: str, text: str) -> None:
    assert fmt_value(value, unit) == text


def test_same_frame_treats_nan_as_equal() -> None:
    a = Frame(series=(Series("x", ((1.0, float("nan")), (2.0, 3.0))),), at=1.0)
    b = Frame(series=(Series("x", ((1.0, float("nan")), (2.0, 3.0))),), at=9.0)
    assert same_frame(a, b)
    assert not same_frame(a, Frame(series=(Series("x", ((1.0, 2.0), (2.0, 3.0))),)))
    assert not same_frame(a, None) and same_frame(None, None)


@pytest.mark.parametrize(("word", "colour"), [
    ("Running", "green"), ("Ready", "green"), ("OK", "green"), ("Pending", "amber"), ("Waiting", "amber"),
    ("Init:0/1", "amber"), ("Failed", "red"), ("Error", "red"), ("CrashLoopBackOff", "red"),
    ("ImagePullBackOff", "red"), ("checkout-api", None), ("3h", None),
])
def test_status_words_are_coloured(word: str, colour: str | None) -> None:
    assert status_colour(word) == (PALETTE[colour] if colour else None)


def test_columns_fit_the_width_names_cut_first_and_statuses_whole() -> None:
    widths = fit_columns(SERVICES, 41)
    assert len(widths) == 3, "RESTARTS and UPTIME dropped from the right"
    assert widths[2] == len("Unhealthy"), "a status column is never cut"
    assert sum(widths) + 2 * 2 <= 41 and widths[0] >= 13, "the name keeps a third"
    assert widths[0] < len("checkout-api.eu-west.blue-01"), "the name is cut first"
    assert fit_columns(SERVICES, 200) == [28, 7, 9, 8, 6], "everything in full when there is room"
    assert fit_columns(Table(("A",), (("x" * 50,),)), 10) == [10]


async def test_a_gauge_says_what_it_is_out_of_unless_the_value_does() -> None:
    for fields, label in (({"unit": "%", "ymax": 100.0}, ""), ({"ymax": 500.0}, "of 500"),
                          ({"unit": "ms", "ymax": 250.0}, "of 250 ms")):
        view = PanelView(spec("gauge", **fields), at=T0)
        view.set_frame(Frame(series=(Series("", ((T1, 120.0),)),), at=T1))
        app = PanelApp(view, glass=False)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            body = view.body
            assert body.label == label  # type: ignore[attr-defined]
            lines = [body.render_line(y).text.strip() for y in range(body.size.height)]
            assert (label in lines) if label else all("of " not in line for line in lines)


async def test_bars_take_two_rows_each_when_there_is_room(output: Output) -> None:
    view = PanelView(spec("bars"), at=T0)
    view.set_frame(DATA["bars"][0])
    app = PanelApp(view)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        lines = [view.body.render_line(y).text.rstrip() for y in range(6)]
        assert lines[0].startswith("search") and lines[1] == "" and lines[2].startswith("billing")
        meters = sorted(i.rect[1] for i in app.glass.layers[MAIN].dynamic if isinstance(i, Meter))
        assert [y - meters[0] for y in meters] == [0, 2, 4], "the meters sit on their rows"
    assert output.model().errors == []
