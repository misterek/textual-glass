"""``LineChart`` and its ``Lines`` item: values to rows, gaps, the text fallback with the same layout, the number
and time labels, the series' colours, and no redraw for equal data. ``Lines`` at the edge of its box (rule 16) is
in ``test_units.py`` with every other item."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.theme import Theme

from textual_glass import Glass, LineChart
from textual_glass.engine import MAIN
from textual_glass.items import Lines
from textual_glass.palette import Palette
from textual_glass.testing.pilot_helpers import Output, settle, start
from textual_glass.widgets import MAX_LINES, nice_ceil, scale_for, series_colours, short_number, time_ticks

NAN = float("nan")
#: A dark theme with the colours the charts were tuned on.
NIGHT = Theme(name="night", primary="#38e1ff", secondary="#a46bff", accent="#ffc24b", success="#6dffa8",
              warning="#ffc24b", error="#ff4d6d", background="#070b18", surface="#0d1430", panel="#16203d", dark=True)


# -- numbers and labels ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "text"), [
    (0, "0"), (0.42, "0.42"), (4.2, "4.2"), (42.5, "42.5"), (350, "350"), (1234, "1.23k"), (1200, "1.2k"),
    (12_000, "12k"), (3_400_000, "3.4M"), (-250, "-250"), (999_960, "1M"), (0.0005, "0.0005"), (NAN, "–"),
])
def test_short_numbers(value: float, text: str) -> None:
    assert short_number(value) == text


def test_scale_is_round_and_a_little_over_the_top() -> None:
    assert nice_ceil(212 * 1.05) == 250
    assert scale_for([10, 212, 180]) == (0.0, 250.0), "from 0, a little over the top, round"
    assert scale_for([0.1, 0.42]) == (0.0, 0.5)
    assert scale_for([-12, 30]) == (-15.0, 40.0), "below zero: the data's minimum, rounded down"
    assert scale_for([5, 7], ymin=4, ymax=8) == (4.0, 8.0), "the caller's bounds win"
    assert scale_for([]) == (0.0, 1.0) and scale_for([0, 0]) == (0.0, 1.0), "no data: a unit scale"
    assert scale_for([NAN, 3.0]) == (0.0, 4.0), "NaN is a gap, not a value"


def test_time_ticks_step_back_from_now() -> None:
    ticks = time_ticks(900, 40)
    assert ticks[0] == (1.0, "now")
    assert [label for _, label in ticks] == ["now", "-5m", "-10m", "-15m"]
    assert ticks[-1][0] == pytest.approx(0.0)
    assert [label for _, label in time_ticks(3600, 60)] == ["now", "-15m", "-30m", "-45m", "-1h"]
    assert len(time_ticks(900, 12)) <= 3, "fewer labels in a narrow chart"


def test_series_colours_are_distinct_and_from_the_theme() -> None:
    palette = Palette(dark=True, background=(7, 11, 24), surface=(13, 20, 48), panel=(22, 32, 61),
                      foreground=(201, 214, 242), primary=(56, 225, 255), secondary=(164, 107, 255),
                      accent=(255, 194, 75), success=(109, 255, 168), warning=(255, 194, 75), error=(255, 77, 109),
                      bg_top=(7, 11, 24), bg_bottom=(3, 5, 13))
    colours = series_colours(palette)
    assert len(colours) == MAX_LINES == len(set(colours))
    assert colours[:4] == [palette.primary, palette.secondary, palette.accent, palette.success]


# -- the widget ------------------------------------------------------------------------------------------------

T1 = 1_700_000_000.0


class ChartApp(Glass, App[None]):
    CSS = "LineChart { width: 60; height: 12; }"

    def compose(self) -> ComposeResult:
        yield LineChart()

    def on_mount(self) -> None:
        self.register_theme(NIGHT)
        self.theme = "night"


def flat(name: str, value: float, n: int = 61,
         gap: tuple[int, int] | None = None) -> tuple[str, list[tuple[float, float]]]:
    return name, [(T1 - 900 + i * 15, NAN if gap and gap[0] <= i < gap[1] else value) for i in range(n)]


def text_of(chart: LineChart) -> list[str]:
    return [chart.render_line(y).text for y in range(chart.size.height)]


async def test_values_land_on_their_rows_and_labels() -> None:
    app = ChartApp(glass=False)
    async with app.run_test(size=(90, 28)) as pilot:
        chart = app.query_one(LineChart)
        chart.set_series([flat("top", 250.0), flat("zero", 0.0)], start=T1 - 900, end=T1, unit="ms", ymax=250)
        await pilot.pause()
        lines = text_of(chart)
        lay = chart.layout_for(60, 12)
        assert (lay.legend, lay.rows, lay.axis) == (1, 10, 1), "a legend line, ten plot rows, the time axis"
        assert lines[0].startswith("● top 250  ● zero 0"), "the legend: each name and its last value"
        plot = lines[1:11]
        assert plot[0].startswith("250 ") and plot[-1].startswith("  0 "), "labels at the top and bottom rows"
        mid = float(plot[4].lstrip().split()[0])
        assert mid % 10 == 0 and 250 * (1 - 4.4 / 9) <= mid <= 250 * (1 - 3.6 / 9), "a round one within its row"
        braille = [{ch for ch in row[lay.gutter:] if "⠀" < ch <= "⣿"} for row in plot]
        assert braille[0] and braille[-1], "the top value on the top row, zero on the bottom row"
        assert not any(braille[1:-1]), "nothing in between"
        assert lines[-1].startswith(" ms -15m") and lines[-1].rstrip().endswith("now"), "unit, time labels"


async def test_gaps_split_the_line(output: Output) -> None:
    app = ChartApp()
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        chart = app.query_one(LineChart)
        chart.set_series([flat("lag", 5.0, gap=(20, 30))], start=T1 - 900, end=T1, ymax=10)
        await settle(app, pilot)
        item = next(i for i in app.glass.layers[MAIN].dynamic if isinstance(i, Lines))
        assert len(item.series) == 1 and len(item.series[0]) == 2, "two runs either side of the gap"
        first, second = item.series[0]
        assert first[-1][0] < second[0][0], "the gap is between them"
        assert all(v == pytest.approx(0.5) for run in item.series[0] for _, v in run), "5 of 10 is half way up"
        assert item.series[0][-1][-1][0] == pytest.approx(1.0), "the newest point at the right edge"
    assert output.model().errors == []
    plain = ChartApp(glass=False)
    async with plain.run_test(size=(90, 28)) as pilot:
        chart = plain.query_one(LineChart)
        chart.set_series([flat("lag", 5.0, gap=(20, 30))], start=T1 - 900, end=T1, ymax=10)
        await pilot.pause()
        lay = chart.layout_for(60, 12)
        row = next(r for r in text_of(chart)[: lay.rows] if any("⠀" < ch <= "⣿" for ch in r))
        cells = row[lay.gutter:]
        start_gap = int(0.3 + (20 / 60) * (lay.width - 1)) + 1
        end_gap = int(0.3 + (29 / 60) * (lay.width - 1)) - 1
        assert cells[start_gap:end_gap].strip(" ·") == "", "no dots in the gap"


async def test_the_text_fallback_has_the_same_layout(output: Output) -> None:
    series = [flat("api", 180.0), flat("web", 120.0, gap=(10, 14)), flat("db", 60.0)]
    texts: dict[bool, list[str]] = {}
    for glass in (True, False):
        app = ChartApp(glass=None if glass else False)
        async with app.run_test(size=(90, 28)) as pilot:
            if glass:
                await start(app, pilot)
            chart = app.query_one(LineChart)
            chart.set_series(series, start=T1 - 900, end=T1, unit="ms")
            await (settle(app, pilot) if glass else pilot.pause())
            assert chart.glass_on is glass
            texts[glass] = text_of(chart)
            if glass:
                items = [i for i in app.glass.layers[MAIN].dynamic if isinstance(i, Lines)]
                assert len(items) == 1 and len(items[0].series) == 3 and len(items[0].colors) == 3
                assert len(items[0].guides) == 3, "a guide at each label"
    lay = LineChart().layout_for(60, 12)
    glass_lines, plain_lines = texts[True], texts[False]
    assert glass_lines[0] == plain_lines[0], "the same legend"
    assert glass_lines[-1] == plain_lines[-1], "the same time axis"
    for g, p in zip(glass_lines[1:-1], plain_lines[1:-1], strict=True):
        assert g[: lay.gutter] == p[: lay.gutter], "the same labels"
        assert g[lay.gutter:].strip() == "", "glass: the plot is pixels"
    assert any("⠀" < ch <= "⣿" for line in plain_lines for ch in line), "plain: braille lines"


async def test_equal_data_redraws_nothing(output: Output, monkeypatch: pytest.MonkeyPatch) -> None:
    app = ChartApp()
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        chart = app.query_one(LineChart)

        def data() -> list[tuple[str, Sequence[tuple[float, float]]]]:
            return [flat("api", 180.0, gap=(5, 8)), flat("db", float("nan"))]  # fresh NaN objects every time

        chart.set_series(data(), start=T1 - 900, end=T1, unit="ms")
        await settle(app, pilot)
        refreshes: list[object] = []
        real = chart.refresh

        def refresh(*args: Any, **kwargs: Any) -> Any:
            refreshes.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(chart, "refresh", refresh)
        mark = output.mark()
        for _ in range(3):
            chart.set_series(data(), start=T1 - 900, end=T1, unit="ms")
            await settle(app, pilot)
        assert refreshes == [], "equal data, NaN gaps included, asks for no redraw"
        assert output.since(mark) == "", "and sends nothing"
        chart.set_series([flat("api", 181.0)], start=T1 - 900, end=T1, unit="ms")
        await settle(app, pilot)
        assert refreshes and "a=f" in output.since(mark), "new data is an in-place edit"
    assert output.model().errors == []


async def test_more_series_than_fit_are_counted_in_the_legend() -> None:
    app = ChartApp(glass=False)
    async with app.run_test(size=(90, 28)) as pilot:
        chart = app.query_one(LineChart)
        chart.set_series([flat(f"instance-{k}", 10.0 * k) for k in range(10)], start=T1 - 900, end=T1)
        await pilot.pause()
        assert len(chart.lines) == MAX_LINES, "at most eight lines"
        legend = text_of(chart)[0]
        assert legend.rstrip().endswith(("+6", "+5", "+7")) and "instance-0" in legend, legend
