"""The crew art: the contract's promises (sizes, transparent borders, determinism, timing), the two pack styles
(smooth, the built-in crew; picture, your own PNGs), their errors, and the tools. No terminal, no Textual."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

from bridge.crew import POSES, Frames, Member, Pack, PackError, load
from bridge.crew import _figure as fig
from bridge.crew import _picture as picture
from bridge.crew._smooth import SmoothData, SmoothMember

ROOT = Path(__file__).resolve().parent.parent
SMOOTH = ROOT / "bridge" / "crew" / "smooth"
TOOLS = ROOT / "tools"
#: Station sizes the app asks for: the deck's tiers on 9 x 19 px cells (7x3 to 20x8), on Retina's 18 x 38 px
#: (7x3 is 126x114, 10x4 is 180x152), the harness's 8x3 and 16x34 cells, glass's cap, odd ones, tiny ones.
SIZES = [(63, 57), (90, 76), (126, 114), (153, 133), (180, 152), (72, 57), (160, 136), (128, 102), (192, 192),
         (101, 63), (33, 29), (31, 26), (20, 11), (7, 5), (3, 3), (2, 2), (1, 1)]
LAMPS = [(255, 178, 56), (80, 220, 130), (0, 0, 0), (255, 255, 255)]
AMBER = (255, 178, 56)
RED, GREEN = (255, 40, 40), (40, 255, 40)
#: What the poses play: frames and the gap between them (``PACKS.md``'s table).
TIMING = {"empty": (1, 1000), "seated": (6, 500), "typing": (4, 150), "done": (2, 900), "hand_up": (2, 420),
          "waving": (2, 260), "standing": (2, 240), "jolt": (4, 170), "tea": (3, 380), "dozing": (3, 650)}

# the colours of the synthetic pictures below
SKIN, BLUE, PINK, MAGENTA, LIME = (240, 200, 160), (40, 90, 200), (200, 60, 60), (255, 0, 255), (0, 255, 0)


def pixels(frame: bytes, w: int, h: int) -> np.ndarray:
    return np.frombuffer(frame, np.uint8).reshape(h, w, 4)


def alpha(frame: bytes, w: int, h: int) -> np.ndarray:
    return pixels(frame, w, h)[..., 3]


def check(result: Frames, w: int, h: int, what: str) -> None:
    """The contract's promises for one sprite: 1 to 8 frames of w*h*4 bytes, a gap of at least 16 ms, and an
    outermost pixel ring that is fully transparent (glass rule 17)."""
    frames, gap = result
    assert 1 <= len(frames) <= 8, (what, len(frames))
    assert isinstance(gap, int) and gap >= 16, (what, gap)
    for k, f in enumerate(frames):
        assert isinstance(f, bytes) and len(f) == w * h * 4, (what, k, len(f))
        if w and h:
            a = alpha(f, w, h)
            ring = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
            assert ring.max() == 0, (what, k, "opaque border")


def at(w: int, h: int, x: float, y: float) -> tuple[int, int]:
    """The pixel (row, column) of design point (x, y) in a w x h station (the canvas fits the 116 x 100 box
    into the picture less a one-pixel margin, bottom centred)."""
    s = min((w - 2) / fig.DESIGN[0], (h - 2) / fig.DESIGN[1])
    return int((h - 1 - fig.DESIGN[1] * s) + y * s), int((w - fig.DESIGN[0] * s) / 2 + x * s)


def near(px: np.ndarray, color: tuple[int, int, int], slack: int = 40) -> bool:
    return bool(px[3] > 200 and all(abs(int(px[c]) - color[c]) <= slack for c in range(3)))


# ---------------------------------------------------------------- synthetic pictures --


def figure(size: tuple[int, int] = (232, 200), body: tuple[int, int, int] = BLUE, hand_up: bool = False,
           marker: int | None = None, mug: bool = False) -> Image.Image:
    """A stand-in for a painted character on the picture canvas (any size with its proportions): a body down to
    the bottom of the picture, a round head, a raised arm (``hand_up``), a marker that moves between frames."""
    w, h = size
    k = w / picture.CANVAS[0]
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    def box(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
        return (x0 * k, y0 * k, x1 * k, y1 * k)

    d.rectangle(box(150, 250, 314, 400), fill=(*body, 255))
    d.ellipse(box(140, 70, 324, 254), fill=(*SKIN, 255))
    if hand_up:
        d.rectangle(box(340, 30, 380, 260), fill=(*MAGENTA, 255))
    if marker is not None:
        d.rectangle(box(40 + 60 * marker, 30, 80 + 60 * marker, 70), fill=(*LIME, 255))
    if mug:
        d.rectangle(box(300, 200, 340, 240), fill=(255, 255, 255, 255))
    return im


Pictures = dict[str, Image.Image | list[Image.Image]]


def picture_pack(folder: Path, members: dict[str, Pictures], **manifest: Any) -> Path:
    """A picture pack on disk: one folder per member, ``<pose>.png`` or ``<pose>_<n>.png``, and a pack.json."""
    folder.mkdir(parents=True, exist_ok=True)
    for mid, poses in members.items():
        (folder / mid).mkdir(exist_ok=True)
        for pose, images in poses.items():
            if isinstance(images, list):
                for k, im in enumerate(images, start=1):
                    im.save(folder / mid / f"{pose}_{k}.png")
            else:
                images.save(folder / mid / f"{pose}.png")
    data: dict[str, Any] = {"name": folder.name, "style": "picture",
                            "members": [{"id": mid, "name": mid.title(), "uniform": "#ff9f43"} for mid in members]}
    data.update(manifest)
    (folder / "pack.json").write_text(json.dumps(data))
    return folder


KIT: Pictures = {"seated": figure(), "hand_up": figure(hand_up=True),
                 "typing": [figure(marker=0), figure(marker=1), figure(marker=2)], "tea": figure(mug=True)}
RHO: Pictures = {"seated": figure(picture.CANVAS, PINK), "hand_up": figure(picture.CANVAS, PINK, hand_up=True)}


@pytest.fixture(scope="module")
def smooth() -> Pack:
    """The built-in crew."""
    return load()


@pytest.fixture(scope="module")
def tiny(tmp_path_factory: pytest.TempPathFactory) -> Pack:
    """A small picture pack: Kit with four poses (typing in three frames), Rho with the two required ones."""
    return load(picture_pack(tmp_path_factory.mktemp("packs") / "tiny", {"kit": KIT, "rho": RHO}))


@pytest.fixture(scope="module")
def starter(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """What the export tool writes for one member: a starter pack of pictures of Ash."""
    out = tmp_path_factory.mktemp("export") / "starter"
    done = subprocess.run([sys.executable, str(TOOLS / "export_pictures.py"), "--out", str(out), "--members", "ash"],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    return out


@pytest.fixture(scope="module", params=["smooth", "tiny", "starter"])
def every(request: pytest.FixtureRequest) -> Pack:
    """Each kind of pack in turn: the contract is the same for all."""
    got = request.getfixturevalue(request.param)
    return got if isinstance(got, Pack) else load(got)


def fresh(pack: Pack) -> Pack:
    """A new instance of the same pack: nothing cached."""
    return load(SMOOTH) if pack.style == "smooth" else load(pack._data.root)


# ---------------------------------------------------------------- the contract --


def test_the_built_in_crew_keeps_the_contract_names(smooth: Pack) -> None:
    assert POSES == ("empty", "seated", "typing", "done", "hand_up", "waving", "standing", "jolt", "tea", "dozing")
    assert smooth.name == smooth.style == "smooth"
    assert [m.id for m in smooth.members] == ["ash", "bo", "cy", "dee", "eli", "fen"]
    assert all(isinstance(m, Member) and len(m.uniform) == 3 for m in smooth.members)
    assert len({m.uniform for m in smooth.members}) == 6, "every member has their own identity colour"
    assert smooth.member("cy").name == "Cy"
    with pytest.raises(KeyError):
        smooth.member("nobody")
    assert load() is smooth and load("smooth") is smooth, "the built-in crew is loaded once"
    assert load(SMOOTH).members == smooth.members and load(str(SMOOTH)).style == "smooth", "and loads from its folder"
    assert isinstance(Pack(), Pack) and Pack().style == "smooth", "Pack() is the built-in crew too"


@pytest.mark.parametrize("w,h", SIZES)
def test_every_station_frame_is_sized_and_transparent_at_its_border(every: Pack, w: int, h: int) -> None:
    pack = every
    for k, m in enumerate(pack.members):
        for pose in POSES:  # drawn for real at every size: the lamps and ensign counts spread over the crew
            n = k + POSES.index(pose)
            check(pack.station(m.id, pose, LAMPS[n % len(LAMPS)], n % 4, w, h), w, h, f"{m.id} {pose} {w}x{h}")
    check(pack.station(None, "empty", AMBER, 0, w, h), w, h, f"no member {w}x{h}")


def test_station_output_is_deterministic(every: Pack) -> None:
    pack, new = every, fresh(every)
    sizes = ((63, 57), (180, 152), (33, 29))
    for k, m in enumerate(pack.members):
        for pose in POSES:
            w, h = sizes[(k + POSES.index(pose)) % 3]
            assert pack.station(m.id, pose, AMBER, 2, w, h) == new.station(m.id, pose, AMBER, 2, w, h), (m.id, pose)
    assert pack.viewscreen(True, 400, 76) == new.viewscreen(True, 400, 76)
    assert pack.chair(2, 90, 57) == new.chair(2, 90, 57)


def test_the_cache_hands_out_copies(smooth: Pack) -> None:
    frames, _ = smooth.station("ash", "typing", AMBER, 0, 90, 76)
    frames.clear()
    assert len(smooth.station("ash", "typing", AMBER, 0, 90, 76)[0]) == 4


@pytest.mark.parametrize("w,h", [(1600, 270), (1008, 152), (504, 76), (360, 57), (640, 136), (1000, 95), (192, 29),
                                 (123, 45), (30, 19), (10, 5), (3, 3), (2, 2), (1, 1)])
def test_viewscreen(smooth: Pack, w: int, h: int) -> None:
    for alert in (False, True):
        check(smooth.viewscreen(alert, w, h), w, h, f"viewscreen {alert} {w}x{h}")
    if w >= 100:
        calm, red = smooth.viewscreen(False, w, h)[0][0], smooth.viewscreen(True, w, h)[0][0]
        rgb = lambda f: pixels(f, w, h)[..., :3].astype(int)  # noqa: E731
        assert (rgb(red)[..., 0] - rgb(red)[..., 2]).mean() > (rgb(calm)[..., 0] - rgb(calm)[..., 2]).mean() + 20
        assert len(set(smooth.viewscreen(False, w, h)[0])) > 1, "the stars twinkle"


def test_viewscreen_tiles_stay_in_step(smooth: Pack) -> None:
    """The app may cut the viewscreen into tiles, each a terminal animation of its own: every size has the same
    number of frames and the same gap (for calm, and for red alert), so tiles of one picture stay in step."""
    for alert in (False, True):
        timings = {(len(f), gap) for f, gap in (smooth.viewscreen(alert, w, h) for w, h in
                                                ((1600, 270), (1008, 152), (504, 76), (192, 29), (40, 12), (10, 5),
                                                 (3, 3)))}
        assert len(timings) == 1, (alert, timings)


@pytest.mark.parametrize("w,h", [(90, 57), (90, 76), (160, 102), (180, 152), (108, 38), (47, 23), (12, 8), (3, 3),
                                 (1, 1)])
def test_chair(smooth: Pack, w: int, h: int) -> None:
    for waiting in (0, 1, 2, 3, 4, 9):
        check(smooth.chair(waiting, w, h), w, h, f"chair {waiting} {w}x{h}")
    assert len(smooth.chair(0, w, h)[0]) == 1, "nobody waiting: a still"
    if w >= 47:
        lit = [smooth.chair(n, w, h)[0][0] for n in range(4)]
        assert len(set(lit)) == 4, "0, 1, 2 and 3 waiting look different"
        assert smooth.chair(3, w, h) == smooth.chair(7, w, h), "3 or more all look the same"


def test_a_picture_pack_keeps_the_smooth_chair_and_viewscreen(tiny: Pack, smooth: Pack) -> None:
    assert tiny.chair(2, 126, 114) == smooth.chair(2, 126, 114)
    assert tiny.viewscreen(True, 504, 76) == smooth.viewscreen(True, 504, 76)
    assert tiny.station(None, "empty", AMBER, 0, 126, 114) == smooth.station(None, "empty", AMBER, 0, 126, 114)


def test_the_lamp_shows_its_colour(every: Pack) -> None:
    pack = every
    lamp = (17, 201, 99)

    def lamp_pixels(frame: bytes) -> int:  # frames keep 6 bits of colour where opaque: within 3 levels
        px = np.frombuffer(frame, np.uint8).reshape(-1, 4).astype(int)
        return int(((np.abs(px[:, :3] - lamp) <= 3).all(axis=1) & (px[:, 3] == 255)).sum())

    for m in pack.members:
        for pose in POSES[1:]:
            frames, _ = pack.station(m.id, pose, lamp, 0, 90, 76)
            assert lamp_pixels(frames[0]) >= 36, (m.id, pose)  # the lamp is a bar
    assert lamp_pixels(pack.station(None, "empty", lamp, 0, 90, 76)[0][0]) == 0, "an empty station's lamp is dark"


def test_poses_animate_and_differ(smooth: Pack) -> None:
    for m in smooth.members:
        firsts = set()
        for pose in POSES:
            frames, gap = smooth.station(m.id, pose, AMBER, 0, 90, 76)
            firsts.add(frames[0])
            assert (len(frames), gap) == TIMING[pose], pose
            if pose != "empty":
                assert len(set(frames)) > 1, (m.id, pose, "animates")
        assert len(firsts) == len(POSES), m.id
    looks = {smooth.station(m.id, "seated", AMBER, 0, 90, 76)[0][0] for m in smooth.members}
    assert len(looks) == 6, "six different-looking members"


def test_ensigns(every: Pack) -> None:
    pack = every
    mid = pack.members[0].id
    views = [pack.station(mid, "typing", AMBER, n, 90, 76)[0] for n in range(4)]
    assert len({v[0] for v in views}) == 4
    assert pack.station(mid, "typing", AMBER, 3, 90, 76) == pack.station(mid, "typing", AMBER, 7, 90, 76)
    assert pack.station(mid, "typing", AMBER, -1, 90, 76) == pack.station(mid, "typing", AMBER, 0, 90, 76)


def test_member_none_or_empty_pose_is_the_bare_station(every: Pack) -> None:
    pack = every
    mid = pack.members[0].id
    bare = pack.station(None, "empty", AMBER, 0, 90, 76)
    assert pack.station(None, "typing", AMBER, 2, 90, 76) == bare
    assert pack.station(mid, "empty", AMBER, 2, 90, 76) == bare, "nobody there: no ensigns either"
    with pytest.raises(ValueError, match="unknown pose"):
        pack.station(mid, "flying", AMBER, 0, 90, 76)
    with pytest.raises(KeyError):
        pack.station("nobody", "seated", AMBER, 0, 90, 76)


def test_the_sprite_key_set_stays_finite(every: Pack) -> None:
    """The app keys a station sprite by (pack, member, pose, lamp role, ensigns): the makers tell apart nothing
    finer. Ensigns and waiting counts clamp, poses are ``POSES``, and a pose plays at most 8 frames."""
    pack = every
    distinct = set()
    for m in pack.members:
        for pose in POSES:
            for n in (0, 1, 2, 3, 5, 9):
                frames, _ = pack.station(m.id, pose, AMBER, n, 63, 57)
                assert len(frames) <= 8
                distinct.add(frames[0])
    assert len(distinct) <= len(pack.members) * (len(POSES) - 1) * 4 + 1
    assert pack.chair(9, 90, 76) == pack.chair(3, 90, 76)


# ---------------------------------------------------------------- the smooth style --


def smooth_members(pack: Pack) -> list[SmoothMember]:
    data = pack._data
    assert isinstance(data, SmoothData)
    return data.members


def test_smooth_crew_has_six_different_looks(smooth: Pack) -> None:
    assert [(m.id, m.name) for m in smooth.members] == [("ash", "Ash"), ("bo", "Bo"), ("cy", "Cy"), ("dee", "Dee"),
                                                        ("eli", "Eli"), ("fen", "Fen")]
    looks = {(a.look.hair_style, a.look.extras) for a in smooth_members(smooth)}
    assert len(looks) == 6, "every member has their own hair and extras"
    assert {len(fig.pose_rigs(p)) for p in POSES} == {1, 2, 3, 4, 6}
    assert {p: (len(fig.pose_rigs(p)), fig.GAPS[p]) for p in POSES} == TIMING


def test_smooth_draws_at_any_size_not_whole_numbers(smooth: Pack) -> None:
    """The art fills the box at whatever size, bottom centred, with its border clear."""
    for w, h in ((63, 57), (101, 63), (153, 133), (180, 152)):
        a = alpha(smooth.station(None, "empty", AMBER, 0, w, h)[0][0], w, h)
        rows, cols = np.nonzero(a > 8)
        top, bottom, left, right = int(np.min(rows)), int(np.max(rows)), int(np.min(cols)), int(np.max(cols))
        assert bottom >= h - 4, ("sits on the bottom", w, h)
        assert abs((left + right) / 2 - (w - 1) / 2) <= 1.0, ("centred", w, h)
        assert right - left >= 0.86 * (w - 2) or bottom - top >= 0.6 * (h - 2), (w, h)


def test_smooth_edges_are_anti_aliased(smooth: Pack) -> None:
    """Shapes end in partly transparent pixels, never a hard pixel edge."""
    a = alpha(smooth.station("dee", "hand_up", AMBER, 0, 126, 114)[0][0], 126, 114)
    soft = ((a > 0) & (a < 255)).sum()
    assert soft > 0.03 * (a > 0).sum(), soft


def test_smooth_raised_hands_are_well_above_the_head(smooth: Pack) -> None:
    """In the rig, for every build: a raised hand's fingertips (hand_up, waving) are at least half a head's
    radius above the top of the head; standing, both arms are up past the top of the head. On screen: what
    hand_up adds to the seated picture reaches the top fifth."""
    for art in smooth_members(smooth):
        for pose, margin, count in (("hand_up", 0.5, 1), ("waving", 0.5, 1), ("standing", 0.0, 2)):
            for rig in fig.pose_rigs(pose):
                assert rig is not None
                rig = fig.built(rig, art.look)
                fingers = 4.0 * rig.body  # the fingertips, beyond the hand's point
                top = rig.head[1] - rig.head_r
                raised = [a for a in rig.arms if a.hand[1] - fingers < top - margin * rig.head_r]
                assert len(raised) == count, (art.member.id, pose)
    for w, h in ((63, 57), (180, 152)):
        for m in smooth.members:
            seated = alpha(smooth.station(m.id, "seated", AMBER, 0, w, h)[0][0], w, h) > 128
            up = alpha(smooth.station(m.id, "hand_up", AMBER, 0, w, h)[0][1], w, h) > 128  # frame 1: no "!"
            rows = np.nonzero((up & ~seated).any(axis=1))[0]
            assert rows.size and int(np.min(rows)) < 0.2 * h, (m.id, w, h)


def test_smooth_done_reads_differently_from_hand_up(smooth: Pack) -> None:
    """Finished is relaxed, arms folded: no hand anywhere near the head's height."""
    for rig in fig.pose_rigs("done"):
        assert rig is not None
        assert all(a.hand[1] > rig.head[1] + rig.head_r for a in rig.arms), "both hands below the chin"
        assert rig.face.eyes == "happy"
    for m in smooth.members:
        done = alpha(smooth.station(m.id, "done", AMBER, 0, 90, 76)[0][0], 90, 76) > 128
        seated = alpha(smooth.station(m.id, "seated", AMBER, 0, 90, 76)[0][0], 90, 76) > 128
        assert int(np.min(np.nonzero(done.any(axis=1))[0])) >= int(np.min(np.nonzero(seated.any(axis=1))[0])) - 2, m.id


def share(px: np.ndarray, c: int) -> float:
    return float((px[:, c] / (px[:, :3].sum(axis=1) + 1)).mean())


def lamp_shift(pack: Pack, mid: str, pose: str = "seated") -> tuple[float, float]:
    """How much redder the member is under a red lamp than under a green one, and greener under the green (each
    channel's share of the colour, over the member above the console), at 126 x 114."""
    w, h = 126, 114
    red = pixels(pack.station(mid, pose, RED, 0, w, h)[0][0], w, h)
    green = pixels(pack.station(mid, pose, GREEN, 0, w, h)[0][0], w, h)
    body = (red[..., 3] == 255) & (green[..., 3] == 255)
    body[int(h * 0.7):] = False  # the member, not the console
    r, g = red[body].astype(float), green[body].astype(float)
    return share(r, 0) - share(g, 0), share(g, 1) - share(r, 1)


def test_smooth_members_are_lit_by_their_lamp(smooth: Pack) -> None:
    """The HD-2D trick: state shows as light. The same pose under a red lamp is redder on the member, under a
    green lamp greener: dark skin and hair count as much as light."""
    for m in smooth.members:
        redder, greener = lamp_shift(smooth, m.id)
        assert redder > 0.025 and greener > 0.025, m.id


def test_smooth_is_safe_from_threads(smooth: Pack) -> None:
    """The makers run in glass's worker thread: renders at once from several threads agree."""
    from concurrent.futures import ThreadPoolExecutor

    def render(k: int) -> tuple[list[bytes], int]:
        return load(SMOOTH).station("cy", ("typing", "jolt")[k % 2], AMBER, k % 4, 90, 76)

    with ThreadPoolExecutor(4) as pool:
        got = list(pool.map(render, range(8)))
    for k, frames in enumerate(got):
        assert frames == got[k % 4], k
    with ThreadPoolExecutor(4) as pool:
        shared = list(pool.map(lambda _: smooth.station("eli", "tea", AMBER, 1, 63, 57), range(6)))
    assert all(s == shared[0] for s in shared)


def _smooth_pack(tmp_path: Path, manifest: dict[str, Any]) -> Path:
    folder = tmp_path / "pack"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pack.json").write_text(json.dumps(manifest))
    return folder


def test_a_smooth_pack_can_extend_the_built_in_one(tmp_path: Path, smooth: Pack) -> None:
    folder = _smooth_pack(tmp_path / "a", {"name": "mine", "style": "smooth", "extends": "smooth",
                                           "colors": {"console": "#604030"}})
    mine = load(folder)
    assert mine.style == "smooth" and mine.members == smooth.members
    assert mine.station("ash", "typing", AMBER, 0, 90, 76) != smooth.station("ash", "typing", AMBER, 0, 90, 76)
    folder = _smooth_pack(tmp_path / "b", {"name": "kit", "style": "smooth", "extends": "smooth", "members": [
        {"id": "kit", "name": "Kit", "uniform": "#ff9f43", "colors": {"skin": "#d9a27c", "hair": "#2b1d14"},
         "hair_style": "curly", "extras": ["glasses", "headset"], "build": {"wide": 1}}]})
    kit = load(folder)
    assert [m.id for m in kit.members] == ["kit"]
    for pose in POSES:
        check(kit.station("kit", pose, AMBER, 1, 63, 57), 63, 57, f"kit {pose}")


def test_broken_smooth_packs_fail_clearly(tmp_path: Path) -> None:
    good: dict[str, Any] = {"id": "kit", "name": "Kit", "uniform": "#ff9f43", "hair_style": "bob"}
    cases: list[tuple[dict[str, Any], str]] = [
        ({"members": [{**good, "hair_style": "mohawk"}]}, "hair_style 'mohawk' is not one of"),
        ({"members": [{**good, "extras": ["cape"]}]}, "extra 'cape' is not one of"),
        ({"members": [{**good, "colors": {"skin": "skin_tone*0.9"}}]}, "colors.skin: no colour named 'skin_tone'"),
        ({"members": [{**good, "hairstyle": "bob"}]}, "unknown key 'hairstyle'"),
        ({"members": [{**good, "build": {"wide": 3}}]}, "build.wide is -1 to 2"),
        ({"members": [good, good]}, "used twice"),
        ({"members": [{k: v for k, v in good.items() if k != "uniform"}]}, "uniform .* is required"),
        ({"members": []}, "members is empty"),
        ({}, "members is required"),
        ({"members": [good], "colors": {"console": "#zz0000"}}, "not a hex colour"),
        ({"members": [good], "ensigns": [{"hair_style": "afro"}]}, r"ensigns\[0\]: hair_style 'afro'"),
        ({"members": [good], "extends": ".."}, "not a smooth pack"),
    ]
    for i, (extra, message) in enumerate(cases):
        folder = _smooth_pack(tmp_path / str(i), {"name": "x", "style": "smooth", **extra})
        with pytest.raises(PackError, match=message):
            load(folder)


def test_broken_packs_fail_clearly_whatever_the_style(tmp_path: Path) -> None:
    with pytest.raises(PackError, match="not a folder"):
        load(tmp_path / "missing")
    (tmp_path / "empty").mkdir()
    with pytest.raises(PackError, match="no pack.json"):
        load(tmp_path / "empty")
    bad = _smooth_pack(tmp_path / "json", {})
    (bad / "pack.json").write_text('{"name": "x",\n "members": [}')
    with pytest.raises(PackError, match=r"pack.json:2: not valid JSON"):
        load(bad)
    (bad / "pack.json").write_text("[1, 2]")
    with pytest.raises(PackError, match="the manifest should be an object"):
        load(bad)
    for style in ("pixel", "watercolour", None):
        folder = _smooth_pack(tmp_path / f"style-{style}", {"name": "x", "style": style})
        with pytest.raises(PackError, match="style should be 'smooth' or 'picture'"):
            load(folder)
    with pytest.raises(PackError, match="not a folder"):
        load("pixel")  # no built-in of that name: it is read as a folder


# ---------------------------------------------------------------- the picture style --


def test_a_picture_pack_loads_and_its_poses_fall_back(tiny: Pack) -> None:
    assert tiny.style == "picture" and tiny.name == "tiny"
    assert [m.id for m in tiny.members] == ["kit", "rho"] and tiny.member("rho").uniform == (255, 159, 67)
    assert picture.REQUIRED == ("seated", "hand_up") and POSES[1:] == picture.MEMBER_POSES
    assert picture.FALLBACK == {"typing": "seated", "done": "seated", "jolt": "seated", "tea": "seated",
                                "waving": "hand_up", "standing": "waving", "dozing": "done"}
    least = {"seated", "hand_up"}
    assert {p: picture.resolve(p, least) for p in POSES[1:]} == {
        "seated": "seated", "typing": "seated", "done": "seated", "jolt": "seated", "tea": "seated",
        "dozing": "seated", "hand_up": "hand_up", "waving": "hand_up", "standing": "hand_up"}
    assert picture.resolve("dozing", {*least, "done"}) == "done"
    assert picture.resolve("standing", {*least, "waving"}) == "waving"
    with pytest.raises(ValueError, match="unknown pose"):
        picture.resolve("empty", least)
    kit = tiny._data.members[0]
    assert isinstance(kit, picture.PictureMember)
    assert kit.frames("standing") is kit.pictures["hand_up"] and kit.frames("tea") is kit.pictures["tea"]
    assert len(kit.frames("typing")) == 3 and len(kit.frames("jolt")) == 1


def test_a_raised_hand_shows_in_every_waiting_pose(tiny: Pack) -> None:
    """Rho has only seated and hand_up: the raised arm (magenta) is in hand_up, waving and standing, and in no
    other pose, at the small size too."""
    for w, h in ((63, 57), (126, 114)):
        for pose in POSES[1:]:
            px = pixels(tiny.station("rho", pose, AMBER, 0, w, h)[0][0], w, h).astype(int)
            magenta = ((px[..., 3] > 200) & (px[..., 0] > 150) & (px[..., 1] < 90) & (px[..., 2] > 150)).sum()
            if pose in ("hand_up", "waving", "standing"):
                assert magenta > 0.01 * w * h, (pose, w, h, magenta)
            else:
                assert magenta == 0, (pose, w, h, magenta)


def test_picture_frames_set_the_pace_and_the_station_animates_round_a_still(tiny: Pack) -> None:
    """Kit's typing has three pictures: three frames at the pose's gap. Kit's other poses are stills: the
    console, the effects and the ensigns still move at the pose's pace, and a pose where nothing moves is one
    frame."""
    w, h = 126, 114
    frames, gap = tiny.station("kit", "typing", AMBER, 0, w, h)
    assert (len(frames), gap) == (3, 150) and len(set(frames)) == 3
    for pose, count in (("seated", 1), ("hand_up", 2), ("waving", 2), ("standing", 2), ("done", 2), ("jolt", 4),
                        ("tea", 3), ("dozing", 3)):
        frames, gap = tiny.station("kit", pose, AMBER, 0, w, h)
        assert (len(frames), gap) == (count, TIMING[pose][1]), pose
        assert len(set(frames)) == count, (pose, "every frame differs")
    assert len(tiny.station("kit", "waving", AMBER, 0, w, h)[0]) == 2, "a still sways while waving"
    frames, _ = tiny.station("kit", "seated", AMBER, 2, w, h)
    assert len(frames) == 6 and len(set(frames)) == 2, "the ensigns bob through the seated loop"
    marks = []
    for f in tiny.station("kit", "typing", AMBER, 0, w, h)[0]:
        px = pixels(f, w, h).astype(int)
        lime = (px[..., 3] > 200) & (px[..., 1] > 200) & (px[..., 0] < 80) & (px[..., 2] < 80)
        marks.append(int(np.nonzero(lime.any(axis=0))[0].mean()))
    assert marks[0] < marks[1] < marks[2], "the frames play in order"


def test_gaps_can_be_set_per_pose(tmp_path: Path) -> None:
    pack = load(picture_pack(tmp_path / "gaps", {"kit": KIT}, gaps={"typing": 120, "seated": 2000}))
    assert pack.station("kit", "typing", AMBER, 0, 63, 57)[1] == 120
    assert pack.station("kit", "seated", AMBER, 2, 63, 57)[1] == 2000
    assert pack.station("kit", "hand_up", AMBER, 0, 63, 57)[1] == 420, "the default"


def test_a_picture_sits_behind_the_console_and_over_its_plate(tiny: Pack) -> None:
    """Kit's body reaches the bottom of the picture: the console's front hides it, the plate shows it (hands
    rest there), and above the console it is all there."""
    for w, h in ((63, 57), (126, 114), (180, 152)):
        px = pixels(tiny.station("kit", "seated", AMBER, 0, w, h)[0][0], w, h)
        chest, plate, front = at(w, h, 58, 70), at(w, h, 58, 81.5), at(w, h, 58, 92)
        assert px[chest][3] == 255 and px[chest][2] > px[chest][0] + 40, ("the body above the console", w, h)
        assert px[plate][3] == 255 and px[plate][2] > px[plate][0] + 40, ("the body over the plate", w, h)
        assert px[front][3] == 255 and px[front][2] < 140, ("the console's front hides the body", w, h)
        head = at(w, h, 58, 41)
        assert near(px[head], SKIN, 60), ("the head", w, h)


def test_the_lamp_lights_a_picture_unless_told_not_to(tmp_path: Path) -> None:
    lit = load(picture_pack(tmp_path / "lit", {"kit": KIT}))
    unlit = load(picture_pack(tmp_path / "unlit", {"kit": KIT}, lamp_light=False))
    strong = load(picture_pack(tmp_path / "strong", {"kit": KIT}, lamp_light=2.0))
    redder, greener = lamp_shift(lit, "kit")
    assert redder > 0.025 and greener > 0.025, "state shows as light on a picture too"
    assert lamp_shift(strong, "kit")[0] > redder, "a stronger setting lights it more"
    assert lamp_shift(unlit, "kit")[0] < 0.6 * redder, "unlit, only the chair behind the picture takes the lamp"
    w, h = 126, 114
    red = pixels(unlit.station("kit", "seated", RED, 0, w, h)[0][0], w, h)
    green = pixels(unlit.station("kit", "seated", GREEN, 0, w, h)[0][0], w, h)
    for x, y in ((58, 70), (58, 41), (50, 75), (70, 50)):  # the chest, the head, a shoulder, a cheek
        assert tuple(red[at(w, h, x, y)]) == tuple(green[at(w, h, x, y)]), "unlit, the same under any lamp"
    chest = red[at(w, h, 58, 70)]
    assert all(abs(int(chest[c]) - BLUE[c]) <= 4 for c in range(3)), "and its colours are the painted ones"


def test_a_jolt_shakes_a_still_picture_only(tmp_path: Path, tiny: Pack) -> None:
    w, h = 180, 152

    def head_x(frame: bytes) -> float:
        px = pixels(frame, w, h).astype(int)
        skin = (px[..., 3] > 200) & (np.abs(px[..., 0] - SKIN[0]) < 30) & (np.abs(px[..., 1] - SKIN[1]) < 30)
        skin[int(h * 0.6):] = False
        return float(np.nonzero(skin)[1].mean())

    xs = [head_x(f) for f in tiny.station("kit", "jolt", AMBER, 0, w, h)[0]]
    assert max(xs) - min(xs) > 1.0, xs
    own = load(picture_pack(tmp_path / "own", {"kit": {**KIT, "jolt": [figure(marker=0), figure(marker=1)]}}))
    xs = [head_x(f) for f in own.station("kit", "jolt", AMBER, 0, w, h)[0]]
    assert max(xs) - min(xs) < 0.5, ("a pack's own jolt frames are not shaken", xs)


def test_effects_can_be_turned_off(tmp_path: Path, tiny: Pack) -> None:
    w, h = 126, 114
    quiet = load(picture_pack(tmp_path / "quiet", {"kit": KIT}, effects=False))
    for pose, count in (("hand_up", 1), ("dozing", 1), ("done", 1), ("tea", 1), ("jolt", 4), ("typing", 3)):
        assert len(quiet.station("kit", pose, AMBER, 0, w, h)[0]) == count, pose

    def yellow(pack: Pack, pose: str) -> int:  # the glowing "!" in the top right corner, under a dark lamp
        px = pixels(pack.station("kit", pose, (0, 0, 0), 0, w, h)[0][0], w, h).astype(int)[: h // 3, int(0.78 * w):]
        return int(((px[..., 3] > 200) & (px[..., 0] > 200) & (px[..., 1] > 170) & (px[..., 2] < 180)).sum())

    assert yellow(tiny, "hand_up") > 0 and yellow(quiet, "hand_up") == 0, "the '!' is an effect"


def test_pictures_of_any_size_with_the_canvas_proportions(tmp_path: Path) -> None:
    big = (picture.CANVAS[0] * 2, picture.CANVAS[1] * 2)
    pack = load(picture_pack(tmp_path / "sizes", {
        "kit": {"seated": figure(big), "hand_up": figure((116, 100), hand_up=True),
                "tea": figure((580, 500), mug=True)}}))
    kit = pack._data.members[0]
    assert isinstance(kit, picture.PictureMember)
    assert (kit.pictures["seated"][0].width, kit.pictures["seated"][0].height) == picture.CANVAS, "shrunk to it"
    assert (kit.pictures["hand_up"][0].width, kit.pictures["hand_up"][0].height) == (116, 100), "small stays"
    assert (kit.pictures["tea"][0].width, kit.pictures["tea"][0].height) == picture.CANVAS
    for pose in POSES[1:]:
        check(pack.station("kit", pose, AMBER, 1, 126, 114), 126, 114, pose)
    px = pixels(pack.station("kit", "seated", AMBER, 0, 126, 114)[0][0], 126, 114)
    assert near(px[at(126, 114, 58, 41)], SKIN, 60), "the big picture scaled down lands in the same place"


def test_broken_pictures_fail_clearly(tmp_path: Path) -> None:
    both: Pictures = {"seated": figure(), "hand_up": figure(hand_up=True)}

    def pack_with(name: str, image: Image.Image | bytes, file: str = "seated.png") -> Path:
        folder = picture_pack(tmp_path / name, {"kit": both})
        if isinstance(image, bytes):
            (folder / "kit" / file).write_bytes(image)
        else:
            image.save(folder / "kit" / file)
        return folder

    with pytest.raises(PackError, match=r"seated.png: 400x400 pixels; a picture has the canvas's proportions, 464x400"):
        load(pack_with("aspect", Image.new("RGBA", (400, 400), (0, 0, 0, 0))))
    with pytest.raises(PackError, match=r"seated.png: 58x50 pixels is too small"):
        load(pack_with("small", Image.new("RGBA", (58, 50), (0, 0, 0, 0))))
    with pytest.raises(PackError, match=r"seated.png: no transparency \(mode RGB\)"):
        load(pack_with("rgb", figure().convert("RGB")))
    with pytest.raises(PackError, match=r"seated.png: every pixel is opaque"):
        load(pack_with("opaque", Image.new("RGBA", picture.CANVAS, (10, 20, 30, 255))))
    with pytest.raises(PackError, match=r"seated.png: can't read it as an image"):
        load(pack_with("text", b"not a png"))
    folder = pack_with("jpeg", figure())
    figure().convert("RGB").save(folder / "kit" / "tea.png", format="JPEG")
    with pytest.raises(PackError, match=r"tea.png: no transparency \(mode RGB\)"):
        load(folder)
    (folder / "kit" / "tea.png").unlink()
    figure().convert("LA").save(folder / "kit" / "tea.png")
    assert load(folder).station("kit", "tea", AMBER, 0, 63, 57), "greyscale with alpha is fine"


def test_broken_picture_packs_fail_clearly(tmp_path: Path) -> None:
    both: Pictures = {"seated": figure(), "hand_up": figure(hand_up=True)}
    member = {"id": "kit", "name": "Kit", "uniform": "#ff9f43"}
    cases: list[tuple[dict[str, Any], str]] = [
        ({"members": []}, "members is empty"),
        ({"members": [{**member, "id": "nobody"}]}, r"member 'nobody': no folder .*nobody \(one folder per member"),
        ({"members": [member, member]}, "used twice"),
        ({"members": [{"id": "kit", "name": "Kit"}]}, "uniform .* is required"),
        ({"members": [{**member, "colors": {"skin": "#fff"}}]}, "unknown key 'colors'"),
        ({"members": [member], "extends": "smooth"}, "unknown key 'extends'"),
        ({"members": [member], "gaps": {"flying": 100}}, "gaps: 'flying' is not a pose"),
        ({"members": [member], "gaps": {"typing": 5}}, r"gaps.typing: 5 is out of range \(16 to 60000 ms\)"),
        ({"members": [member], "gaps": {"typing": "slow"}}, "gaps.typing: the gap .* should be a whole number"),
        ({"members": [member], "lamp_light": "yes"}, "lamp_light should be true, false or a strength"),
        ({"members": [member], "lamp_light": 3}, "lamp_light should be true, false or a strength"),
        ({"members": [member], "effects": 1}, "effects should be true or false"),
    ]
    for i, (extra, message) in enumerate(cases):
        folder = picture_pack(tmp_path / f"m{i}", {"kit": both})
        manifest = json.loads((folder / "pack.json").read_text())
        manifest.pop("members")
        manifest.update(extra)
        (folder / "pack.json").write_text(json.dumps(manifest))
        with pytest.raises(PackError, match=message):
            load(folder)
    folder = picture_pack(tmp_path / "manifest", {"kit": both})
    manifest = json.loads((folder / "pack.json").read_text())
    del manifest["members"]
    (folder / "pack.json").write_text(json.dumps(manifest))
    with pytest.raises(PackError, match="members is required"):
        load(folder)

    files: list[tuple[Pictures, str]] = [
        ({"hand_up": figure(hand_up=True)}, r"kit: no seated.png \(seated and hand_up are required"),
        ({"seated": figure()}, r"kit: no hand_up.png"),
        ({**both, "empty": figure()}, r"empty.png: the empty station .* is the renderer's"),
        ({**both, "flying": figure()}, r"flying.png: 'flying' is not a pose \(a picture is <pose>.png or <pose>_<n>"),
        ({**both, "Tea": figure()}, r"Tea.png: 'Tea' is not a pose"),  # (not Seated: the file system may fold case)
        ({**both, "typing": [figure()] * 9}, r"kit: 9 frames of typing, at most 8"),
    ]
    for i, (pictures, message) in enumerate(files):
        with pytest.raises(PackError, match=message):
            load(picture_pack(tmp_path / f"f{i}", {"kit": pictures}))
    folder = picture_pack(tmp_path / "twice", {"kit": {**both, "typing": [figure(), figure()]}})
    figure().save(folder / "kit" / "typing.png")
    with pytest.raises(PackError, match=r"typing.png: both typing.png and numbered frames of typing"):
        load(folder)
    folder = picture_pack(tmp_path / "gap", {"kit": {**both, "typing": [figure(), figure(), figure()]}})
    (folder / "kit" / "typing_2.png").unlink()
    with pytest.raises(PackError, match=r"kit: typing_2.png is missing \(frames are numbered 1 to 2, no gaps\)"):
        load(folder)
    folder = picture_pack(tmp_path / "zero", {"kit": both})
    figure().save(folder / "kit" / "typing_0.png")
    with pytest.raises(PackError, match=r"typing_0.png: frames are numbered from 1"):
        load(folder)


def test_other_files_in_a_member_folder_are_left_alone(tmp_path: Path) -> None:
    folder = picture_pack(tmp_path / "notes", {"kit": KIT})
    (folder / "kit" / "notes.txt").write_text("Kit, the navigator")
    (folder / "kit" / "seated.psd").write_bytes(b"\x00\x01")
    (folder / "kit" / "sources").mkdir()
    (folder / "guide.png").write_bytes(b"not read")
    assert load(folder).station("kit", "seated", AMBER, 0, 63, 57)


def test_picture_packs_are_safe_from_threads(tiny: Pack) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(4) as pool:
        got = list(pool.map(lambda k: tiny.station("kit", ("typing", "jolt")[k % 2], AMBER, k % 4, 90, 76), range(8)))
    for k, frames in enumerate(got):
        assert frames == got[k % 4], k


# ---------------------------------------------------------------- the tools --


def test_the_export_tool_writes_a_pack_that_draws_like_the_live_crew(starter: Path, smooth: Pack) -> None:
    """Export, load, draw: the starter pack's Ash is the live Ash within a few levels of colour, pose by pose,
    frame by frame, at the small and the big station."""
    assert sorted(p.name for p in starter.iterdir()) == ["ash", "guide.png", "pack.json"]
    names = sorted(p.name for p in (starter / "ash").iterdir())
    assert len(names) == sum(TIMING[p][0] for p in POSES[1:]) == 28
    assert "seated_1.png" in names and "seated_6.png" in names and "hand_up_2.png" in names
    manifest = json.loads((starter / "pack.json").read_text())
    assert manifest["style"] == "picture"
    assert manifest["members"] == [{"id": "ash", "name": "Ash", "uniform": "#36c2a6"}]
    assert len(manifest["ensigns"]) == 3 and "colors" not in manifest, "the built-in colours are the defaults"
    with Image.open(starter / "guide.png") as guide, Image.open(starter / "ash" / "seated_1.png") as still:
        assert guide.size == still.size == picture.CANVAS and still.mode == "RGBA"
    pack = load(starter)
    assert pack.style == "picture" and pack.members == smooth.members[:1]
    for w, h in ((63, 57), (126, 114), (180, 152)):
        for pose in POSES[1:]:
            mine, live = pack.station("ash", pose, LAMPS[0], 1, w, h), smooth.station("ash", pose, LAMPS[0], 1, w, h)
            assert (len(mine[0]), mine[1]) == (len(live[0]), live[1]), pose
            for k, (a, b) in enumerate(zip(mine[0], live[0], strict=True)):
                pa, pb = pixels(a, w, h).astype(int), pixels(b, w, h).astype(int)
                both, either = (pa[..., 3] > 128) & (pb[..., 3] > 128), (pa[..., 3] > 128) | (pb[..., 3] > 128)
                assert int(both.sum()) / int(either.sum()) > 0.98, (pose, k, w, h, "the same silhouette")
                assert np.abs(pa[..., :3] - pb[..., :3]).mean(axis=2)[both].mean() < 16, (pose, k, w, h, "colour")


def test_the_export_tool_takes_members_poses_stills_and_a_scale(tmp_path: Path, tiny: Pack) -> None:
    out = tmp_path / "dee"
    done = subprocess.run([sys.executable, str(TOOLS / "export_pictures.py"), "--out", str(out), "--members", "dee",
                           "--poses", "seated,hand_up", "--stills", "--scale", "2"], cwd=ROOT, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in (out / "dee").iterdir()) == ["hand_up.png", "seated.png"]
    with Image.open(out / "dee" / "seated.png") as im:
        assert im.size == (picture.CANVAS[0] * 2, picture.CANVAS[1] * 2)
    pack = load(out)
    assert [m.id for m in pack.members] == ["dee"]
    check(pack.station("dee", "standing", AMBER, 2, 126, 114), 126, 114, "dee standing from hand_up")
    for argv, message in ((["--members", "nobody"], "no member 'nobody'"), (["--poses", "empty"], "not a pose"),
                          (["--pack", str(tiny._data.root)], "only a smooth pack"), (["--scale", "0"], "--scale")):
        done = subprocess.run([sys.executable, str(TOOLS / "export_pictures.py"), "--out", str(tmp_path / "x"), *argv],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)
        assert done.returncode == 2 and message in done.stderr, (argv, done.stderr)


def test_the_contact_sheet_tool_draws_both_styles(tmp_path: Path, starter: Path) -> None:
    out = tmp_path / "sheets"
    done = subprocess.run([sys.executable, str(TOOLS / "crew_sheet.py"), "--pack", str(starter), "--against", "smooth",
                           "--out", str(out), "--only", "sheets,ensigns", "--sizes", "small"], cwd=ROOT,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in out.iterdir()) == ["ensigns.png", "sheet_small.png"]
    with Image.open(out / "sheet_small.png") as sheet:
        assert sheet.height > 2 * 57, "a row for the pictures and one for the live crew"


# ---------------------------------------------------------------- no Textual --


def test_crew_imports_nothing_from_textual() -> None:
    for path in (ROOT / "bridge" / "crew").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(n.split(".")[0].startswith("textual") for n in names), (path, names)
    code = ("import sys, bridge.crew as c; c.load().station('ash', 'typing', (1, 2, 3), 1, 90, 76); "
            "bad = [m for m in sys.modules if m.split('.')[0].startswith('textual')]; print(bad); "
            "sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr


def test_nothing_of_the_old_pixel_style_is_left() -> None:
    """The smooth crew is the only built-in crew: no grid packs, no second built-in name."""
    from bridge import crew

    assert crew.BUILT_IN == ("smooth",) and crew.STYLES == ("smooth", "picture")
    assert sorted(p.name for p in (ROOT / "bridge" / "crew").iterdir() if p.is_dir() and p.name != "__pycache__") == \
        ["smooth"]
    assert not (ROOT / "packs").exists() and not (TOOLS / "draw_default_pack.py").exists()
