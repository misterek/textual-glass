"""The week-in-review art (``textual_glass.scenes.space``): every look builds a scene within the
contract's limits, every frame of every actor is transparent at its border (rule 17), the same seed
gives the same scene, minimal motion draws one frame per actor, and the loops are seamless. On a
light theme the show brings its own night; a big week fits the limits comfortably, keeps one light
per piece of work and lasts about as long as a small one."""

from __future__ import annotations

import colorsys
import inspect
import math
import re

import numpy as np
import pytest

from textual_glass.scenes import (
    MAX_ACTORS,
    MAX_PIXELS,
    ROLES,
    Cue,
    Scene,
    Stage,
    _effects,
    _paint,
    _ships,
    _sky,
    _worlds,
    check,
    shares,
    space,
)

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


def retina(dark: bool = True) -> Stage:
    """140x45 cells on a Retina screen: the biggest page the limits are measured on."""
    return stage(dark, cols=140, rows=45, cell=(18.0, 38.0))


def pixels(scene: Scene) -> int:
    """The pixels the terminal holds: an image shared by stills and loops counts once."""
    seen = {id(a.frames) if shares(a) else id(a): a for a in scene.actors}
    return sum(a.width * a.height * len(a.frames) for a in seen.values())


def held(scene: Scene) -> list:
    """The lights that stay: on stage until the end and after it."""
    return [a for a in scene.actors if a.leave == math.inf and a.z >= 10]


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
    assert [c.data[0] for c in scene.cues] == ["landed"] * 8 + ["end"], "one cue per landing, then the end"
    assert scene.cues[-1] == Cue(scene.duration, space.END) and len(held(scene)) == 8, "a light each stays"
    assert max(c.t for c in scene.cues[:-1]) < scene.duration


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
    assert all(len(a.frames) == 1 for a in scene.actors) and list(scene.cues) == [Cue(0.1, space.END)]
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


# -- the night ---------------------------------------------------------------------------------------


def test_on_a_light_theme_the_show_brings_its_own_night() -> None:
    """A viewport over the whole stage behind everything, its edge fading to nothing, a few stars of
    three pictures; and the art drawn as on a dark theme, in the theme's hues lifted to glow."""
    for look in space.LOOKS:
        light, dark = build(look, stage(dark=False)), build(look, stage(dark=True))
        sky = light.actors[0]
        assert sky.cells == (64, 22) and sky.z == 0 and len(sky.frames) == 1, "stretched over the whole stage"
        assert sky.route.at(0) == (64 * 8 / 2, 22 * 16 / 2)
        a = np.frombuffer(bytes(sky.frames[0]), np.uint8).reshape(sky.height, sky.width, 4)[..., 3]
        assert a[sky.height // 2, sky.width // 2] == 255 and a[sky.height // 2, 1 : sky.width // 2].min() > 0
        assert a[0, 0] == 0 and a[2, 2] < a[sky.height // 2, 6], "rounded, and fading at the edge"
        stars = [x for x in light.actors if x.z == 1]
        assert len(stars) >= 6 and len({id(x.frames) for x in stars}) == 3, "a few stars, three images"
        assert not any(x.z <= 1 for x in dark.actors), "on a dark theme the dashboard's own space shows"
        assert len(light.actors) == len(dark.actors) + 1 + len(stars)
    lit = _sky.night(stage(dark=False))
    assert lit.dark and lit.colors["background"] == _sky._mix(*lit.space, 0.55)
    assert _sky.night(stage(dark=True)) == stage(dark=True)


@pytest.mark.parametrize("role", [r for r in ROLES if r not in ("foreground", "background")])
def test_lifted_hues_keep_their_hue_and_glow(role: str) -> None:
    c = LIGHT[role]
    up = _sky.lift(c)
    h0, l0, s0 = colorsys.rgb_to_hls(*(v / 255 for v in c))
    h1, l1, s1 = colorsys.rgb_to_hls(*(v / 255 for v in up))
    assert abs((h1 - h0 + 0.5) % 1 - 0.5) < 0.01, "the same hue"
    assert l1 >= max(l0, _sky.GLOW_LIGHTNESS) - 0.01 and s1 >= min(1.0, _sky.GLOW_SATURATION) - 0.01
    assert _sky.lift(NAVY[role]) == pytest.approx(NAVY[role], abs=16), "a hue that glows already barely moves"
    fg = _sky.night(stage(dark=False)).colors["foreground"]
    assert colorsys.rgb_to_hls(*(v / 255 for v in fg))[1] > 0.8, "the night's foreground is light"


# -- big weeks ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("look", space.LOOKS)
@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_a_big_week_fits_the_limits_comfortably_with_a_light_each(look: str, dark: bool) -> None:
    """150 pieces of work at 140x45 cells on a Retina screen: well within the pixel and actor limits,
    every one a cue and a light that stays, and every light of a kind (and slope) one shared image."""
    st = retina(dark)
    week = space.sample_week(150, seed=5, releases=2)
    scene = space.make(st, week, look, loop_frames=8)
    check(scene, st)
    assert pixels(scene) < 0.7 * MAX_PIXELS and len(scene.actors) < 0.7 * MAX_ACTORS
    landings = [c for c in scene.cues if c.data != space.END]
    assert sorted(c.data[1] for c in landings) == list(range(150)) and scene.cues[-1].data == space.END
    assert all(c.data == ("landed", c.data[1], week[c.data[1]].kind) for c in landings)
    lights = held(scene)
    assert len(lights) == 150, "one light each (the harbour's releases are its new moons)"
    kept = [a for a in lights if a.z == 20]  # not the moons
    kinds = {k for k in space.KINDS if any(w.kind == k for w in week)} - ({"release"} if look == "harbour" else set())
    assert len({id(a.frames) for a in kept}) <= (len(kinds) if look != "nightfall" else 80), \
        "one picture per kind (at night, per slope and town)"
    ships = [a for a in scene.actors if a.z in (40, 41)]
    assert len(ships) == 150 and len({id(a.frames) for a in ships}) < 100, "every one a ship, sharing pictures"


@pytest.mark.parametrize("count", [3, 12, 25, 60, 150])
def test_the_show_lasts_about_its_length_however_busy_the_week(count: int) -> None:
    st = stage()
    week = space.sample_week(count, seed=6)
    scene = space.make(st, week, "halo", loop_frames=4)
    assert scene.duration <= 22.0
    if count >= 12:
        assert scene.duration >= 16.0
    launches = space.schedule(week)
    assert launches == sorted(launches) and all(b - a <= 1.4 + 2 * space.RELEASE_ROOM + 1e-9
                                                for a, b in zip(launches, launches[1:], strict=False))
    if count >= 75:  # in waves: the gap between days is longer than within one
        gaps = [(launches[k + 1] - launches[k], week[k].day != week[k + 1].day) for k in range(count - 1)
                if "release" not in (week[k].kind, week[k + 1].kind)]
        assert min(g for g, new_day in gaps if new_day) > 3 * max(g for g, new_day in gaps if not new_day)


def test_a_week_too_big_for_the_terminal_shows_its_latest_work() -> None:
    st = stage()
    week = space.sample_week(1200, seed=7)
    scene = space.make(st, week, "nightfall", loop_frames=4)
    check(scene, st)
    shown = sorted(c.data[1] for c in scene.cues if c.data != space.END)
    assert shown == list(range(1200 - space.MAX_WORK, 1200)), "the latest, by their index in the week"


@pytest.mark.parametrize("look", space.LOOKS)
def test_a_big_week_is_clear_at_every_border(look: str) -> None:
    """Rule 17 for what only a busy week draws: brief warp-ins, flashes, smaller lights."""
    st = stage(dark=False)
    scene = space.make(st, space.sample_week(120, seed=8), look, loop_frames=4)
    check(scene, st)
    for n, actor in enumerate(scene.actors):
        for k, frame in enumerate(actor.frames):
            assert border_ring_clear(frame, actor.width, actor.height), f"{look} actor {n} frame {k + 1}: at the edge"


def test_a_light_coming_on_hands_over_to_the_one_that_stays() -> None:
    """The one-shot ends where the held light begins: its last frame is close to it, and the held light
    is the same picture for every light of a kind."""
    st = stage()
    colour = _paint.ink(st, "secondary")
    on = _effects.light_on(st, "merged", colour, "star", seed=3)
    stays = _effects.held_light(st, "merged", colour, "star", seed=3)
    assert sum(on.gaps) / 1000 == pytest.approx(_effects.LIGHT_SETTLES)
    last = np.frombuffer(on.frames[-1], np.uint8).astype(int)[3::4]
    kept = np.frombuffer(stays.frames[0], np.uint8).astype(int)[3::4]
    assert (on.width, on.height) == (stays.width, stays.height) and np.abs(last - kept).mean() < 4


def test_the_art_has_no_textual_imports() -> None:
    for module in (space, _paint, _ships, _effects, _worlds, _sky):
        assert not re.search(r"^\s*(from|import) textual\b", inspect.getsource(module), re.M), module.__name__
