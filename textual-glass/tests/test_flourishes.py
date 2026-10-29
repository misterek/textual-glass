"""The flourish makers: every frame transparent at the stage's border (rule 17),
the limits, determinism, the stage's geometry, and the canvas they share."""

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


def nothing(stage: flourishes.Stage) -> tuple[list[bytes], list[int]]:
    """A maker for the tests of the stage's geometry."""
    return [_draw.blank(stage)] * 2, [16, 16]


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
    kind = Flourish(nothing, grow=(4, 2), travels=True)
    assert stage_cells(kind, (10, 5, 20, 6), None, (90, 28)) == (6, 3, 28, 10)
    assert stage_cells(kind, (10, 5, 20, 6), (60, 20, 10, 4), (90, 28)) == (6, 3, 68, 23)
    still = Flourish(nothing, grow=(4, 2))
    assert stage_cells(still, (10, 5, 20, 6), (60, 20, 10, 4), (90, 28)) == (6, 3, 28, 10), "only travellers reach"
    assert make_stage(still, (10, 5, 20, 6), (60, 20, 10, 4), (90, 28), (9.0, 19.0), CYAN, VIOLET)[1].to is None
    assert stage_cells(kind, (0, 0, 10, 3), None, (90, 28)) == (0, 0, 14, 5), "clamped to the window"
    assert stage_cells(kind, None, None, (90, 28)) == (0, 0, 90, 28), "no anchor: the whole window"
    screen = Flourish(nothing, screen=True)
    assert stage_cells(screen, (10, 5, 20, 6), None, (90, 28)) == (0, 0, 90, 28)


def test_the_stage_is_capped_in_pixels_and_keeps_square_pixels() -> None:
    kind = Flourish(nothing, screen=True)
    cells, stage = make_stage(kind, (10, 5, 20, 6), None, (140, 45), (16.0, 34.0), CYAN, VIOLET)
    assert stage.width * stage.height <= flourishes.MAX_PX * 1.02
    assert stage.window_edges == (True, True, True, True)
    assert abs(stage.cell[0] / stage.cell[1] - 16 / 34) < 0.02, "square pixels: a cell keeps its shape"
    x, y, w, h = stage.at
    assert abs(x - 10 * stage.cell[0]) < 1e-6 and abs(w - 20 * stage.cell[0]) < 1e-6
    _, small = make_stage(Flourish(nothing), (10, 5, 4, 2), None, (90, 28), (9.0, 19.0), CYAN, VIOLET)
    assert small.scale == 1.0, "small stages are drawn at the terminal's own resolution"


@pytest.mark.parametrize("dark", [True, False])
def test_light_draws_into_a_window_or_a_list_of_pixels_as_into_the_whole_frame(dark: bool) -> None:
    _, stage = make_stage(Flourish(nothing), (10, 5, 20, 6), None, (90, 28), (9.0, 19.0), CYAN, VIOLET, dark=dark)
    win = _draw.window(stage, 30.5, 20.2, 90.7, 61.0)
    assert win is not None
    where, xs, ys = win
    inside = np.exp(-((xs - 60) ** 2 + (ys - 40) ** 2) / 300) * 1.6
    whole = np.zeros((stage.height, stage.width), np.float32)
    whole[where] = inside
    flat = np.arange(stage.width * stage.height).reshape(stage.height, stage.width)[where].ravel()
    lights = [_draw.Light(stage) for _ in range(3)]
    for light, at, k in zip(lights, (None, where, flat), (whole, inside, inside.ravel()), strict=True):
        light.over(VIOLET, k * 0.5, at)
        light.add(CYAN, k, at)
        light.shine(CYAN, k, at)
    for light in lights[1:]:
        assert np.allclose(light.rgb, lights[0].rgb, atol=1e-6) and np.allclose(light.a, lights[0].a, atol=1e-6)


def test_frames_are_packed_within_a_few_levels_of_the_light() -> None:
    _, stage = make_stage(Flourish(nothing), (10, 5, 20, 6), None, (90, 28), (9.0, 19.0), CYAN, VIOLET)
    xs, ys = _draw.coords(stage)
    light = _draw.Light(stage)
    light.add(CYAN, np.exp(-((xs - 150) ** 2 + (ys - 80) ** 2) / 4000) * 1.4)
    light.add(VIOLET, np.exp(-((xs - 200) ** 2 + (ys - 90) ** 2) / 900))
    got = np.frombuffer(light.frame(), np.uint8).reshape(stage.height, stage.width, 4).astype(np.float32)
    a = np.clip(light.a * _draw.border(stage), 0, 1)
    s = light.rgb / np.maximum(light.a, 1e-4)[..., None]
    peak = s.max(axis=2, keepdims=True)
    s = s / np.maximum(peak, 1.0) + np.clip(peak - 1.0, 0, None) * 0.6
    want = np.concatenate([np.clip(s, 0, 1) * a[..., None], a[..., None]], 2) * 255
    shown = np.concatenate([got[..., :3] * got[..., 3:] / 255, got[..., 3:]], 2)
    assert np.abs(shown - want).max() <= 5, "premultiplied, as the terminal blends it: within 5/255"
    assert int(got[..., 3][a * 255 < 3.4].max()) == 0, "light too faint to see is dropped"
