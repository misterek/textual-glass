"""The scene contract (``textual_glass.scenes``): routes, and the limits ``check`` holds a maker to."""

from __future__ import annotations

import inspect
import re

import pytest

from textual_glass import scenes
from textual_glass.scenes import Actor, Cue, Route, Scene, Stage, check

STAGE = Stage(900, 380, 100, 20, (9.0, 19.0), {role: (200, 200, 200) for role in scenes.ROLES},
              dark=True, still=False, seed=0)


def actor(**changes: object) -> Actor:
    fields: dict[str, object] = dict(frames=[bytes(4 * 4 * 4)], width=4, height=4, route=Route.still(10, 10))
    fields.update(changes)
    return Actor(**fields)  # type: ignore[arg-type]


def test_a_route_is_straight_between_its_keys_and_still_outside_them() -> None:
    route = Route(((1.0, 0.0, 0.0), (3.0, 100.0, 50.0), (3.0, 0.0, 0.0)))
    assert route.at(0) == (0, 0) and route.at(1) == (0, 0)
    assert route.at(2) == (50, 25)
    assert route.at(9) == (0, 0) and route.settles == 3
    assert Route.still(4, 5).at(100) == (4, 5)


def test_an_eased_line_starts_and_ends_on_its_points() -> None:
    route = Route.line((0, 0), (90, 30), 2, 4, ease=lambda u: u * u)
    assert route.at(2) == (0, 0) and route.at(4) == (90, 30)
    assert route.at(3)[0] == pytest.approx(22.5)
    assert len(Route.line((0, 0), (1, 1), 0, 1).keys) == 2


def test_routes_refuse_keys_out_of_order() -> None:
    with pytest.raises(ValueError, match="order"):
        Route(((2.0, 0.0, 0.0), (1.0, 0.0, 0.0)))
    with pytest.raises(ValueError, match="at least one"):
        Route(())


def test_check_holds_a_maker_to_the_limits() -> None:
    check(Scene([actor(), actor(cells=(100, 20), z=99)], 30), STAGE)
    bad = {
        "not a Scene": ([actor()],),
        "at most": Scene([actor()], scenes.MAX_SECONDS + 1),
        "RGBA bytes": Scene([actor(frames=[b"x"])], 1),
        "one per frame": Scene([actor(frames=[bytes(64)] * 3, gaps=(1, 2))], 1),
        "z from 0": Scene([actor(z=100)], 1),
        "leave after": Scene([actor(enter=2, leave=1)], 1),
        "more than the stage": Scene([actor(cells=(101, 1))], 1),
        "actors": Scene([actor()] * (scenes.MAX_ACTORS + 1), 1),
        "pixels": Scene([actor(frames=[bytes(400 * 400 * 4)] * 64, width=400, height=400) for _ in range(2)], 1),
        "at least one pixel": Scene([actor(frames=[b""], width=0)], 1),
        "cues": Scene([actor()], 2, cues=[Cue(2.5, "late")]),
    }
    check(Scene([actor()], 2, cues=[Cue(0, "first"), Cue(2, "last")]), STAGE)
    for message, scene in bad.items():
        with pytest.raises(ValueError, match=message):
            check(scene, STAGE)


def test_actors_with_the_same_frames_share_one_image_and_count_it_once() -> None:
    """Stills and loops whose frames are the same object are one image: their pixels count once, so
    they must agree on its size (and a loop on its gaps). One-shots never share, so each counts."""
    big = [bytes(1000 * 1000 * 4)] * 9  # 9 million pixels
    loops = [actor(frames=big, width=1000, height=1000, route=Route.still(k, k)) for k in range(30)]
    check(Scene(loops, 1), STAGE)
    with pytest.raises(ValueError, match="pixels"):
        check(Scene([actor(frames=list(big), width=1000, height=1000) for _ in range(2)], 1), STAGE)
    with pytest.raises(ValueError, match="pixels"):
        check(Scene([actor(frames=big, width=1000, height=1000, loop=False) for _ in range(2)], 1), STAGE)
    light = [bytes(4 * 4 * 4)]
    check(Scene([actor(frames=light), actor(frames=light, gaps=(500,), loop=False)], 1), STAGE)
    flame = [bytes(4 * 4 * 4)] * 3
    with pytest.raises(ValueError, match="actors 0 and 1 share their frames"):
        check(Scene([actor(frames=flame), actor(frames=flame, gaps=(40,))], 1), STAGE)
    with pytest.raises(ValueError, match="share their frames"):
        check(Scene([actor(frames=light), actor(frames=light, width=2, height=8)], 1), STAGE)
    check(Scene([actor(frames=flame), actor(frames=flame, gaps=(40,), loop=False)], 1), STAGE)  # a one-shot: its own
    assert scenes.shares(actor(frames=flame)) and scenes.shares(actor(loop=False))
    assert not scenes.shares(actor(frames=flame, loop=False))


def test_the_scenes_have_no_textual_imports() -> None:
    assert not re.search(r"^\s*(from|import) textual\b", inspect.getsource(scenes), re.M)
