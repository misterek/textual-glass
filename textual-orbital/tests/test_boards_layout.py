"""``plan``: the panels packed in order into rows of a 12-unit grid, every row full, the rows' heights from the
kinds' minimums and the sizes' weights; and the helpers around it (sizes, minimums, moving a panel)."""

from __future__ import annotations

import pytest

from textual_orbital.boards.layout import (
    LADDER,
    MIN_HEIGHT,
    UNITS,
    Need,
    Plan,
    min_height,
    need,
    plan,
    reposition,
    resolve,
    span,
    step_size,
)

S, M, L = (need("stat", size) for size in LADDER)  # stat: 6 rows, small weighs nothing
TS = need("timeseries", "auto")  # 9 rows, medium


def spans(laid: Plan) -> list[list[int]]:
    return [[units for _, units in row] for row in laid.rows]


def order(laid: Plan) -> list[int]:
    return [index for row in laid.rows for index, _ in row]


def test_sizes_resolve_from_the_kind_and_step_along_the_ladder() -> None:
    assert [resolve(k, "auto") for k in ("stat", "gauge", "timeseries", "bars", "table", "text")] == [
        "small", "small", "medium", "medium", "medium", "medium"]
    assert resolve("stat", "large") == "large"
    assert step_size("stat", "auto", 1) == "medium" and step_size("timeseries", "auto", 1) == "large"
    assert step_size("gauge", "auto", -1) is None, "small is the smallest"
    assert step_size("table", "large", 1) is None and step_size("table", "large", -1) == "medium"


def test_minimum_heights() -> None:
    assert min_height("stat", "auto") == MIN_HEIGHT["stat"]
    assert min_height("stat", "large") == MIN_HEIGHT["stat"] + 3, "large panels get more"
    assert min_height("bars", "auto", 1) == min_height("bars", "auto", 0) == MIN_HEIGHT["bars"], "at least"
    assert min_height("bars", "auto", 7) == 9, "a row per series"
    assert min_height("table", "auto", 4) == 7 and min_height("table", "auto", 50) == 8, "rows, up to 5 (+N more)"


def test_one_panel_fills_the_page() -> None:
    for item in (S, M, L, TS):
        laid = plan([item], 90, 25)
        assert spans(laid) == [[UNITS]] and laid.heights == (25,), item


def test_three_smalls_share_a_row_at_90() -> None:
    laid = plan([S, S, S], 90, 25)
    assert spans(laid) == [[4, 4, 4]]


def test_small_small_medium_the_medium_starts_a_row_and_the_smalls_stretch() -> None:
    laid = plan([S, S, TS], 90, 25)
    assert spans(laid) == [[6, 6], [12]]
    assert order(laid) == [0, 1, 2]


def test_a_lone_panel_at_the_end_stretches() -> None:
    laid = plan([TS, TS, TS], 90, 30)
    assert spans(laid) == [[6, 6], [12]]
    laid = plan([S, S, S, S], 100, 30)
    assert spans(laid) == [[4, 4, 4], [12]]


def test_leftover_units_go_by_span() -> None:
    assert spans(plan([S, TS], 90, 25)) == [[5, 7]], "the medium stays the wider one"
    assert spans(plan([TS, S, S], 140, 25)) == [[6, 3, 3]], "a full row is left as it is"
    assert spans(plan([S, TS], 140, 25)) == [[4, 8]]


def test_everything_is_full_width_under_70() -> None:
    laid = plan([S, S, TS, L], 69, 60)
    assert spans(laid) == [[12], [12], [12], [12]]
    assert span("small", 69) == span("medium", 69) == 12


def test_the_units_at_70_and_from_140() -> None:
    assert [span(size, 70) for size in LADDER] == [4, 6, 12]
    assert [span(size, 139) for size in LADDER] == [4, 6, 12]
    assert [span(size, 140) for size in LADDER] == [3, 6, 12]
    assert spans(plan([S, S, S, S], 140, 40)) == [[3, 3, 3, 3]], "four smalls in a row at 140"
    assert spans(plan([S, S, S, S, TS], 140, 40)) == [[3, 3, 3, 3], [12]]
    assert spans(plan([L, S], 140, 40)) == [[12], [12]]


def test_a_row_is_as_tall_as_its_tallest_minimum() -> None:
    gauge = need("gauge", "auto")
    laid = plan([S, gauge, Need("small", 7)], 90, 6)
    assert laid.heights == (gauge.height,), "the page is too short: the minimum"


def test_spare_height_goes_by_weight() -> None:
    laid = plan([S, S, S, TS, TS], 90, 25)
    assert spans(laid) == [[4, 4, 4], [6, 6]]
    assert laid.heights == (S.height, 25 - S.height), "the smalls' row keeps its minimum; the mediums get the rest"
    laid = plan([TS, TS, L], 90, 40)
    small, big = TS.height, L.height
    spare = 40 - small - big
    assert laid.heights == (small + spare // 3, big + spare - spare // 3), "large weighs twice a medium"
    assert sum(laid.heights) == 40


def test_only_weightless_rows_share_the_spare_height_evenly() -> None:
    laid = plan([S, S, S, S, S], 90, 25)
    assert spans(laid) == [[4, 4, 4], [6, 6]]
    assert laid.heights == (13, 12) and sum(laid.heights) == 25


def test_minimums_that_dont_fit_scroll() -> None:
    laid = plan([TS] * 7, 60, 25)
    assert laid.heights == (TS.height,) * 7, "each row keeps its minimum; the page scrolls"
    assert sum(laid.heights) > 25


def test_order_is_kept() -> None:
    items = [TS, S, L, S, S, TS, M, S]
    for width in (60, 90, 140):
        assert order(plan(items, width, 30)) == list(range(len(items))), width
    laid = plan(items, 90, 30)
    assert laid.spans() == [7, 5, 12, 6, 6, 6, 6, 12]
    assert all(sum(row) == UNITS for row in spans(laid)), "every row is full"


def test_nothing_to_lay_out() -> None:
    assert plan([], 90, 25) == Plan((), ())


@pytest.mark.parametrize(("index", "dy", "expected"), [
    (0, 1, 3), (1, 1, 3), (2, 1, 4), (3, -1, 0), (4, -1, 2), (0, -1, 0), (4, 1, 4)])
def test_up_and_down_go_to_the_panel_that_overlaps_most(index: int, dy: int, expected: int) -> None:
    laid = plan([S, S, S, TS, TS], 90, 25)  # [4 4 4] over [6 6]
    assert laid.vertical(index, dy) == expected


def test_reposition_puts_a_panel_between_its_new_neighbours() -> None:
    keys = [10.0, 20.0, 30.0]
    assert reposition(keys, 1, 0) == {1: 9.0}, "before the first"
    assert reposition(keys, 0, 1) == {0: 25.0}, "between 20 and 30"
    assert reposition(keys, 1, 2) == {1: 31.0}, "after the last"
    assert reposition(keys, 0, -1) == {} and reposition(keys, 2, 3) == {} and reposition(keys, 1, 1) == {}


def test_reposition_spaces_everything_out_when_there_is_no_room() -> None:
    keys = [5.0, 5.0, 5.0]  # added in the same instant: only the ids tell them apart
    moved = reposition(keys, 2, 1)
    final = [moved.get(k, keys[k]) for k in range(3)]
    assert sorted(range(3), key=lambda k: final[k]) == [0, 2, 1]
    assert len(set(final)) == 3
