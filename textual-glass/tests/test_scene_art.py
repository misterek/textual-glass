"""The week-in-review art (``textual_glass.scenes.space``): every look builds a scene within the
contract's limits, every frame of every actor is transparent at its border (rule 17), the same seed
gives the same scene, minimal motion draws one frame per actor, and the loops are seamless."""

from __future__ import annotations

import inspect
import re

import numpy as np
import pytest

from textual_glass.scenes import ROLES, Scene, Stage, _effects, _paint, _ships, _worlds, check, space

NAVY = {"primary": (56, 225, 255), "secondary": (164, 107, 255), "accent": (255, 194, 75), "success": (109, 255, 168),
        "warning": (255, 194, 75), "error": (255, 77, 109), "foreground": (201, 214, 242), "background": (7, 11, 24)}
LIGHT = {"primary": (3, 105, 212), "secondary": (124, 58, 237), "accent": (217, 119, 6), "success": (21, 128, 61),
         "warning": (217, 119, 6), "error": (190, 18, 60), "foreground": (30, 36, 51), "background": (244, 246, 251)}


def stage(dark: bool = True, still: bool = False, cols: int = 64, rows: int = 22,
          cell: tuple[float, float] = (8.0, 16.0)) -> Stage:
    colors = NAVY if dark else LIGHT
    assert set(colors) == set(ROLES)
    return Stage(round(cols * cell[0]), round(rows * cell[1]), cols, rows, cell, colors, dark, still, 3)


def build(look: str, st: Stage, items: int = 8) -> Scene:
    return space.make(st, space.sample_week(items, seed=4), look, loop_frames=4)


def border_ring_clear(frame: bytes, width: int, height: int) -> bool:
    a = np.frombuffer(bytes(frame), np.uint8).reshape(height, width, 4)[..., 3]
    return not (a[0].any() or a[-1].any() or a[:, 0].any() or a[:, -1].any())


@pytest.mark.parametrize("look", space.LOOKS)
@pytest.mark.parametrize("dark", [True, False])
def test_every_look_is_within_the_limits_and_clear_at_every_border(look: str, dark: bool) -> None:
    st = stage(dark)
    scene = build(look, st)
    check(scene, st)
    for n, actor in enumerate(scene.actors):
        for k, frame in enumerate(actor.frames):
            assert border_ring_clear(frame, actor.width, actor.height), f"{look} actor {n} frame {k + 1}: at the edge"
    assert len(scene.cues) == 8, "one cue per landing"
    held = [a for a in scene.actors if a.leave == float("inf") and a.z >= 10]
    assert len(held) == 8, "one light per piece of work stays"


@pytest.mark.parametrize("look", space.LOOKS)
def test_the_summary_holds_still_and_only_loops_move(look: str) -> None:
    scene = build(look, stage())
    for actor in scene.actors:
        if actor.leave == float("inf"):
            assert actor.route.settles <= scene.duration, "nothing on stage at the end is still moving"
            assert actor.loop or actor.enter <= scene.duration, "one-shots that stay have played by the end"
        else:
            assert actor.leave <= scene.duration + 1e-6, "everything that goes has gone by the end"


@pytest.mark.parametrize("look", space.LOOKS)
def test_the_same_seed_gives_the_same_scene(look: str) -> None:
    a, b = build(look, stage()), build(look, stage())
    assert len(a.actors) == len(b.actors)
    for x, y in zip(a.actors, b.actors, strict=True):
        assert x.route == y.route and list(map(bytes, x.frames)) == list(map(bytes, y.frames))


@pytest.mark.parametrize("look", space.LOOKS)
def test_minimal_motion_draws_only_the_summary(look: str) -> None:
    st = stage(still=True)
    scene = build(look, st)
    check(scene, st)
    assert all(len(a.frames) == 1 for a in scene.actors)
    assert all(a.leave == float("inf") and a.route.settles == 0 for a in scene.actors)


def test_the_world_loops_are_seamless_and_the_ring_is_whole() -> None:
    """Each loop is drawn from a phase that goes once round a circle, so its last frame leads back to
    its first: frame k + n equals frame k."""
    st = stage()
    giant = _worlds.Giant.on(st)
    two = _worlds.giant_loop(st, giant, frames=2)
    four = _worlds.giant_loop(st, giant, frames=4)
    assert two.frames[0] == four.frames[0] and two.frames[1] == four.frames[2]
    for sprite in (_worlds.giant_ring(st, giant, front=True), _worlds.giant_ring(st, giant, front=False)):
        assert border_ring_clear(sprite.frames[0], sprite.width, sprite.height)


def test_the_art_has_no_textual_imports() -> None:
    for module in (space, _paint, _ships, _effects, _worlds):
        assert not re.search(r"^\s*(from|import) textual\b", inspect.getsource(module), re.M), module.__name__
