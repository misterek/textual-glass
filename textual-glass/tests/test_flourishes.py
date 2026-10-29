"""The flourish makers: every frame transparent at the stage's border (rule 17),
the limits, determinism, and the stage's geometry."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import numpy as np
import pytest

from textual_glass import flourishes
from textual_glass.flourishes import FLOURISHES, MAX_FRAMES, MAX_MS, Flourish, _draw, make_stage, stage_cells

CYAN, VIOLET = (56, 225, 255), (164, 107, 255)

#: (screen, cell px, anchor, target, dark): a corner window, a small one with the anchor on the
#: window's edge, a big one on a HiDPI screen, a tiny anchor with the target above and to the left,
#: and a light theme.
CASES = [
    ((90, 28), (9.0, 19.0), (4, 6, 38, 10), (50, 15, 36, 10), True),
    ((70, 22), (8.0, 16.0), (0, 0, 20, 5), (48, 17, 22, 5), True),
    ((140, 45), (16.0, 34.0), (60, 20, 10, 3), (5, 2, 30, 6), True),
    ((90, 28), (7.3, 15.7), (40, 20, 3, 1), (10, 3, 12, 4), True),
    ((90, 28), (9.0, 19.0), (30, 8, 30, 12), None, False),
]


def frames_for(name: str, case: tuple) -> tuple[list[np.ndarray], list[int], flourishes.Stage]:
    screen, cell, at, to, dark = case
    _, stage = make_stage(FLOURISHES[name], at, to, screen, cell, CYAN, VIOLET, dark=dark, seed=3)
    frames, gaps = FLOURISHES[name].make(stage)
    assert all(len(f) == stage.width * stage.height * 4 for f in frames), "RGBA bytes, width * height * 4 each"
    return [np.frombuffer(f, np.uint8).reshape(stage.height, stage.width, 4) for f in frames], gaps, stage


@pytest.mark.parametrize("name", list(FLOURISHES))
@pytest.mark.parametrize("case", range(len(CASES)))
def test_flourish_frames_are_transparent_at_the_border_and_within_the_limits(name: str, case: int) -> None:
    arrays, gaps, stage = frames_for(name, CASES[case])
    assert 2 <= len(arrays) <= MAX_FRAMES and len(gaps) == len(arrays)
    assert all(g >= 16 for g in gaps) and sum(gaps) <= MAX_MS
    top, right, bottom, left = stage.window_edges
    for k, a in enumerate(arrays):
        alpha = a[..., 3]
        for side, on_edge, ring in (("top", top, alpha[0]), ("right", right, alpha[:, -1]),
                                    ("bottom", bottom, alpha[-1]), ("left", left, alpha[:, 0])):
            assert on_edge or int(ring.max()) == 0, f"{name} frame {k + 1}: light at the {side} border"
    assert int(arrays[-1][..., 3].max()) <= 13, f"{name}: the last frame is (nearly) empty, so the end never pops"
    assert any(int(a[..., 3].max()) >= 80 for a in arrays), f"{name}: something shows"


@pytest.mark.parametrize("name", list(FLOURISHES))
def test_flourishes_are_deterministic(name: str) -> None:
    first, _, _ = frames_for(name, CASES[0])
    again, _, _ = frames_for(name, CASES[0])
    assert all(np.array_equal(a, b) for a, b in zip(first, again, strict=True))


def test_every_flourish_says_what_it_could_mean() -> None:
    assert len(FLOURISHES) >= 4
    for name, kind in FLOURISHES.items():
        assert re.fullmatch(r"[a-z]+(-[a-z]+)*", name), name
        assert kind.about, name
        for role in (kind.color, kind.accent):
            assert role in ("primary", "secondary", "accent", "success", "warning", "error", "foreground"), role


def test_the_readme_lists_every_kind_with_what_it_could_mean() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    for name, kind in FLOURISHES.items():
        assert f"| `{name}` | {kind.about} |" in readme, f"README.md: the row of {name}"


def test_the_flourishes_have_no_textual_imports() -> None:
    modules = [flourishes, _draw, *(inspect.getmodule(k.make) for k in FLOURISHES.values())]
    for module in modules:
        assert module is not None
        assert not re.search(r"^\s*(from|import) textual\b", inspect.getsource(module), re.M), module.__name__


def test_the_stage_grows_around_the_anchor_and_the_target_within_the_window() -> None:
    kind = Flourish(_draw.soft_flash, grow=(4, 2), travels=True)
    assert stage_cells(kind, (10, 5, 20, 6), None, (90, 28)) == (6, 3, 28, 10)
    assert stage_cells(kind, (10, 5, 20, 6), (60, 20, 10, 4), (90, 28)) == (6, 3, 68, 23)
    still = Flourish(_draw.soft_flash, grow=(4, 2))
    assert stage_cells(still, (10, 5, 20, 6), (60, 20, 10, 4), (90, 28)) == (6, 3, 28, 10), "only travellers reach"
    assert make_stage(still, (10, 5, 20, 6), (60, 20, 10, 4), (90, 28), (9.0, 19.0), CYAN, VIOLET)[1].to is None
    assert stage_cells(kind, (0, 0, 10, 3), None, (90, 28)) == (0, 0, 14, 5), "clamped to the window"
    assert stage_cells(kind, None, None, (90, 28)) == (0, 0, 90, 28), "no anchor: the whole window"
    screen = Flourish(_draw.soft_flash, screen=True)
    assert stage_cells(screen, (10, 5, 20, 6), None, (90, 28)) == (0, 0, 90, 28)


def test_the_stage_is_capped_in_pixels_and_keeps_square_pixels() -> None:
    kind = Flourish(_draw.soft_flash, screen=True)
    cells, stage = make_stage(kind, (10, 5, 20, 6), None, (140, 45), (16.0, 34.0), CYAN, VIOLET)
    assert stage.width * stage.height <= flourishes.MAX_PX * 1.02
    assert stage.window_edges == (True, True, True, True)
    assert abs(stage.cell[0] / stage.cell[1] - 16 / 34) < 0.02, "square pixels: a cell keeps its shape"
    x, y, w, h = stage.at
    assert abs(x - 10 * stage.cell[0]) < 1e-6 and abs(w - 20 * stage.cell[0]) < 1e-6
    _, small = make_stage(Flourish(_draw.soft_flash), (10, 5, 4, 2), None, (90, 28), (9.0, 19.0), CYAN, VIOLET)
    assert small.scale == 1.0, "small stages are drawn at the terminal's own resolution"
