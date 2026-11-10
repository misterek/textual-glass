"""The ``picture`` crew style: your own PNGs. A pack supplies each crew member as pictures, one per pose
(numbered frames for animation), drawn on the picture canvas: the station's 116 x 100 box at 4 pixels a unit,
464 x 400 (``CANVAS``), or any size with those proportions. The renderer draws the station round them, the
smooth style's chair, console, lamp, ensigns and effects, scales the picture to the station's real pixel
size (premultiplied, area-filtered), hides it where it sits behind the console, and lights it with the
console lamp, so the session's state still shows as light. See ``PACKS.md``.

``load_picture`` reads a pack folder (every picture is checked and kept, compressed, so a broken pack fails at
load and a render never touches the disk); ``station`` is the sprite maker behind ``Pack``. The captain's chair
and the viewscreen are the smooth ones. Pure numpy at render time; Pillow reads the PNGs at load.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from . import _figure as fig
from . import _scene as scene
from . import _smooth as smooth
from ._pack import (
    MAX_FRAMES,
    MAX_GAP,
    MIN_GAP,
    POSES,
    Member,
    PackError,
    check_ids,
    check_keys,
    expect,
    read_manifest,
    read_member,
)
from ._paint import F32, Array, Canvas, Light, Region, empty_frame, rgb

STYLE = "picture"
#: The picture canvas: the station's design box (``_figure.DESIGN``, 116 x 100) at 4 pixels a unit.
PX_PER_UNIT = 4
CANVAS = (int(fig.DESIGN[0]) * PX_PER_UNIT, int(fig.DESIGN[1]) * PX_PER_UNIT)
#: Smaller pictures are refused (they would only blur); the proportions must be the canvas's, within this.
MIN_SIZE = (int(fig.DESIGN[0]), int(fig.DESIGN[1]))
ASPECT_SLACK = 0.02
#: The poses a pack draws (the empty station is the renderer's) and the ones it must have.
MEMBER_POSES = POSES[1:]
REQUIRED = ("seated", "hand_up")
#: A pose a pack doesn't draw is shown with this one's picture (and so on down the chain to one it has).
FALLBACK = {"typing": "seated", "done": "seated", "jolt": "seated", "tea": "seated", "waving": "hand_up",
            "standing": "waving", "dozing": "done"}
#: Effects the renderer never draws over a picture: they follow a hand it doesn't know the place of.
NOT_OVER_PICTURES = ("arcs",)
#: Effects that are the console's (sparks off its corners, smoke), drawn even with ``effects`` off.
CONSOLE_FX = ("spark", "smoke")

_MANIFEST_KEYS = ("name", "style", "colors", "members", "ensigns", "gaps", "lamp_light", "effects", "about")
_MEMBER_KEYS = ("id", "name", "uniform", "about")
_NAME = re.compile(r"(?P<pose>[a-z]+(?:_[a-z]+)*?)(?:_(?P<n>[0-9]+))?\.png")


@dataclass(frozen=True)
class Picture:
    """One picture, at most the canvas's size, as zlib-compressed straight-alpha RGBA."""

    width: int
    height: int
    data: bytes

    def array(self) -> Array:
        return np.frombuffer(zlib.decompress(self.data), np.uint8).reshape(self.height, self.width, 4)


@dataclass(frozen=True)
class PictureMember:
    member: Member
    folder: Path
    pictures: dict[str, tuple[Picture, ...]]  # the poses it draws

    def resolve(self, pose: str) -> str:
        """The pose whose pictures show for ``pose``: itself, or down the fallback chain."""
        return resolve(pose, self.pictures)

    def frames(self, pose: str) -> tuple[Picture, ...]:
        return self.pictures[self.resolve(pose)]


@dataclass(frozen=True)
class PictureData:
    name: str
    root: Path
    colors: dict[str, Array]  # the scene's colours, resolved (``_smooth.Scene``)
    ensigns: smooth.Ensigns
    palette: dict[str, str]
    members: list[PictureMember]
    gaps: dict[str, int]  # per pose, where the pack says; else ``_figure.GAPS``
    lamp_light: float  # how much of the lamp's light falls on the pictures (1: as on the smooth crew)
    effects: bool  # the "!", the z's, the steam, the sparkle over the pictures


def resolve(pose: str, available: Any) -> str:
    if pose not in MEMBER_POSES:
        raise ValueError(f"unknown pose {pose!r}")
    while pose not in available:
        pose = FALLBACK[pose]  # ends at seated, which every pack has
    return pose


# ================================================================ loading ==


def load_picture(path: Path) -> PictureData:
    """The picture pack in ``path``. Every picture is read and checked now, so a broken pack fails here with a
    ``PackError`` naming the file and the problem."""
    root = path.expanduser().resolve()
    manifest, m = read_manifest(root)
    check_keys(manifest, _MANIFEST_KEYS, m)
    name = expect(manifest.get("name"), str, m, "name")
    if manifest.get("style") != STYLE:
        raise PackError(f"{m}: style should be {STYLE!r}")
    colors, ensigns, palette = smooth.load_scene(manifest, m)
    if "members" not in manifest:
        raise PackError(f"{m}: members is required (the crew, one folder of pictures each)")
    raw = expect(manifest["members"], list, m, "members")
    if not raw:
        raise PackError(f"{m}: members is empty")
    members = [_member(r, i, root, m) for i, r in enumerate(raw)]
    check_ids([pm.member for pm in members], m)

    gaps: dict[str, int] = {}
    for pose, gap in expect(manifest.get("gaps", {}), dict, m, "gaps").items():
        if pose not in MEMBER_POSES:
            raise PackError(f"{m}: gaps: {pose!r} is not a pose (poses: {', '.join(MEMBER_POSES)})")
        expect(gap, int, f"{m}: gaps.{pose}", "the gap (milliseconds between frames)")
        if not MIN_GAP <= gap <= MAX_GAP:
            raise PackError(f"{m}: gaps.{pose}: {gap} is out of range ({MIN_GAP} to {MAX_GAP} ms)")
        gaps[pose] = gap
    lamp_light = manifest.get("lamp_light", True)
    if isinstance(lamp_light, bool):
        lamp_light = 1.0 if lamp_light else 0.0
    elif not isinstance(lamp_light, (int, float)) or not 0 <= lamp_light <= 2:
        raise PackError(f"{m}: lamp_light should be true, false or a strength from 0 to 2, not {lamp_light!r}")
    effects = expect(manifest.get("effects", True), bool, m, "effects")
    return PictureData(name, root, colors, ensigns, palette, members, gaps, float(lamp_light), bool(effects))


def _member(raw: Any, i: int, root: Path, m: str) -> PictureMember:
    member, _, where = read_member(raw, i, m, _MEMBER_KEYS)
    folder = root / member.id
    if not folder.is_dir():
        raise PackError(f"{where}: no folder {folder} (one folder per member, named after the id, with at least "
                        f"{' and '.join(p + '.png' for p in REQUIRED)} in it)")
    pictures = read_pictures(folder)
    for pose in REQUIRED:
        if pose not in pictures:
            raise PackError(f"{folder}: no {pose}.png ({' and '.join(REQUIRED)} are required; the other poses fall "
                            f"back to them)")
    return PictureMember(member, folder, pictures)


def read_pictures(folder: Path) -> dict[str, tuple[Picture, ...]]:
    """The pictures in a member's folder, by pose: ``<pose>.png`` (a still) or ``<pose>_1.png``,
    ``<pose>_2.png``... (frames, at most 8). Files that aren't PNGs are left alone (notes, sources)."""
    found: dict[str, dict[int, Path]] = {}
    for path in sorted(folder.iterdir()):
        if path.is_dir() or path.suffix.lower() != ".png":
            continue
        match = _NAME.fullmatch(path.name)
        pose = match["pose"] if match else None
        if pose == "empty":
            raise PackError(f"{path}: the empty station (nobody there) is the renderer's; a picture pack has no "
                            f"empty pose")
        if match is None or pose not in MEMBER_POSES:
            raise PackError(f"{path}: {path.stem!r} is not a pose (a picture is <pose>.png or <pose>_<n>.png; "
                            f"poses: {', '.join(MEMBER_POSES)})")
        assert pose is not None
        n = int(match["n"]) if match["n"] is not None else 0
        if match["n"] is not None and n == 0:
            raise PackError(f"{path}: frames are numbered from 1 ({pose}_1.png, {pose}_2.png...)")
        found.setdefault(pose, {})[n] = path
    pictures: dict[str, tuple[Picture, ...]] = {}
    for pose, by_number in found.items():
        if 0 in by_number and len(by_number) > 1:
            raise PackError(f"{by_number[0]}: both {pose}.png and numbered frames of {pose}: use one or the other")
        if 0 in by_number:
            pictures[pose] = (read_picture(by_number[0]),)
            continue
        count = len(by_number)
        if count > MAX_FRAMES:
            raise PackError(f"{folder}: {count} frames of {pose}, at most {MAX_FRAMES}")
        for k in range(1, count + 1):
            if k not in by_number:
                raise PackError(f"{folder}: {pose}_{k}.png is missing (frames are numbered 1 to {count}, no gaps)")
        pictures[pose] = tuple(read_picture(by_number[k]) for k in range(1, count + 1))
    return pictures


def read_picture(path: Path) -> Picture:
    """A PNG as a ``Picture``: it must have an alpha channel with something transparent in it, be at least
    ``MIN_SIZE`` and have the canvas's proportions. Bigger than the canvas, it is shrunk to it."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow comes with the example
        raise PackError(f"{path}: picture packs need Pillow (pip install pillow)") from None
    try:
        with Image.open(path) as im:
            mode, (w, h) = im.mode, im.size
            if mode not in ("RGBA", "LA", "PA", "RGBa") and not (mode == "P" and "transparency" in im.info):
                raise PackError(f"{path}: no transparency (mode {mode}); a picture is a PNG with an alpha channel, "
                                f"the character cut out on a transparent background")
            if w < MIN_SIZE[0] or h < MIN_SIZE[1]:
                raise PackError(f"{path}: {w}x{h} pixels is too small (at least {MIN_SIZE[0]}x{MIN_SIZE[1]}; the "
                                f"canvas is {CANVAS[0]}x{CANVAS[1]})")
            if abs(w / h - CANVAS[0] / CANVAS[1]) > ASPECT_SLACK * CANVAS[0] / CANVAS[1]:
                raise PackError(f"{path}: {w}x{h} pixels; a picture has the canvas's proportions, "
                                f"{CANVAS[0]}x{CANVAS[1]} ({int(fig.DESIGN[0])}:{int(fig.DESIGN[1])})")
            rgba = np.asarray(im.convert("RGBA"), np.uint8)
    except FileNotFoundError:
        raise PackError(f"{path}: file not found") from None
    except OSError as e:  # unreadable, or not an image at all
        raise PackError(f"{path}: can't read it as an image ({e})") from None
    if int(rgba[..., 3].min()) == 255:
        raise PackError(f"{path}: every pixel is opaque; the background must be transparent")
    if (w, h) != CANVAS and (w > CANVAS[0] or h > CANVAS[1]):
        rgba = shrink(rgba, CANVAS)
        h, w = rgba.shape[:2]
    return Picture(w, h, zlib.compress(np.ascontiguousarray(rgba).tobytes(), 1))


# ================================================================ scaling ==


def _weights(centres: Array, n_in: int) -> Array:
    """Filter weights (len(centres), n_in) that sample a row of ``n_in`` source pixels at ``centres`` (source
    pixel units): a triangle as wide as the spacing between samples when shrinking (every source pixel counts,
    the area-filter way), one pixel wide when enlarging (bilinear). A sample beyond the picture's edge fades
    out instead of smearing the edge pixel."""
    step = float(np.median(np.abs(np.diff(centres)))) if len(centres) > 1 else 1.0
    support = max(1.0, step)
    pad = int(np.ceil(support)) + 1
    idx = np.arange(-pad, n_in + pad, dtype=F32) + 0.5
    w = np.clip(1.0 - np.abs(idx[None, :] - centres[:, None].astype(F32)) / support, 0, None)
    w /= np.maximum(w.sum(axis=1, keepdims=True), 1e-9)
    return w[:, pad:pad + n_in].astype(F32)


def resample(rgba: Array, xs: Array, ys: Array) -> tuple[Array, Array]:
    """``rgba`` (uint8, straight alpha) sampled at source coordinates ``xs`` (columns) and ``ys`` (rows), as
    premultiplied float colour (len(ys), len(xs), 3) and alpha (len(ys), len(xs)): alpha-weighted, so
    transparent pixels lend no colour to the edges."""
    sh, sw = rgba.shape[:2]
    p = rgba.astype(F32) / 255
    src = np.concatenate([p[..., :3] * p[..., 3:4], p[..., 3:4]], axis=-1)  # premultiplied
    wy, wx = _weights(ys, sh), _weights(xs, sw)
    h, w = len(ys), len(xs)
    rows = (wy @ src.reshape(sh, sw * 4)).reshape(h, sw, 4)  # two matrix products: the rows, then the columns
    out = (rows.transpose(0, 2, 1).reshape(h * 4, sw) @ wx.T).reshape(h, 4, w).transpose(0, 2, 1)
    return np.ascontiguousarray(out[..., :3]), np.ascontiguousarray(out[..., 3])


def shrink(rgba: Array, size: tuple[int, int]) -> Array:
    """``rgba`` scaled to ``size`` (width, height), straight alpha again."""
    w, h = size
    xs = (np.arange(w, dtype=F32) + 0.5) * rgba.shape[1] / w
    ys = (np.arange(h, dtype=F32) + 0.5) * rgba.shape[0] / h
    prgb, a = resample(rgba, xs, ys)
    out = np.zeros((h, w, 4), np.uint8)
    lit = a > 1e-4
    out[..., :3][lit] = np.clip(prgb[lit] / a[lit][:, None] * 255 + 0.5, 0, 255)
    out[..., 3] = np.clip(a * 255 + 0.5, 0, 255)
    return out


# ================================================================ the station ==


def lamp_on_picture(reg: Region, light: Light) -> Array:
    """How much of the lamp's light falls on each pixel of a flat picture facing the viewer (the lamp term of
    ``Light.shade`` with a flat normal): strongest on the chest and hands near the console's screen, fading up
    the face, gone far from it."""
    lx, ly, lz = light.lamp_at
    vx, vy = lx - reg.x, ly - reg.y
    dist = np.sqrt(vx * vx + vy * vy + lz * lz)
    fall = np.exp(-np.maximum(dist - lz, 0) / light.reach)
    return reg.full(np.clip((lz / dist + 0.2) / 1.2, 0, 1) * fall * light.lamp_power).astype(F32)


@dataclass
class Fitted:
    """A picture scaled to one station size and cut where the console hides it: shared by the frames of a
    station render (the lamp's light is added per frame)."""

    reg: Region
    prgb: Array
    alpha: Array


def fit_picture(cv: Canvas, picture: Picture, dx: float, hide: Array) -> Fitted | None:
    """``picture`` over the station's whole design box on ``cv``, shifted ``dx`` units (a jolt's shake, a
    wave's sway), less ``hide`` (the console's front over that box, from ``front_cover``)."""
    reg = cv.region(0.0, 0.0, fig.DESIGN[0], fig.DESIGN[1])
    if reg is None:
        return None
    xs = (reg.x[0] - dx) * (picture.width / fig.DESIGN[0])
    ys = reg.y[:, 0] * (picture.height / fig.DESIGN[1])
    prgb, a = resample(picture.array(), xs, ys)
    keep = 1 - hide
    return Fitted(reg, prgb * keep[..., None], a * keep)


#: How much of the lamp's light is added as a sheen over the picture (the rest multiplies the picture's own
#: colours): so a dark or saturated picture still takes the lamp's hue, as the smooth crew's rim light does.
SHEEN = 0.12


def draw_picture(cv: Canvas, fitted: Fitted, light: Light | None) -> None:
    """A fitted picture onto the station, lit by ``light``'s lamp if given."""
    prgb = fitted.prgb
    if light is not None and light.lamp_power > 0:
        k = lamp_on_picture(fitted.reg, light)[..., None]
        lamp = light.lamp[None, None, :]
        prgb = prgb * (1 + lamp * k) + lamp * (k * SHEEN * fitted.alpha[..., None])
    cv.blend(fitted.reg, prgb, fitted.alpha)


def station(data: PictureData, pm: PictureMember | None, pose: str, lamp: tuple[int, int, int], ensigns: int,
            width: int, height: int) -> tuple[list[bytes], int]:
    """A console, its chair and, unless ``pm`` is None, the member's picture for ``pose`` (or the nearest pose
    the pack has), lit by ``lamp``, with ``ensigns`` (0 to 3) helpers in front. The console, the effects and
    the ensigns move at the pose's pace (the smooth rigs' lamp flicker and lights), and a still picture sways
    as the pose's head does; a picture with frames of its own sets the pace instead. Frames that all came out
    the same collapse to one still."""
    if pose not in POSES:
        raise ValueError(f"unknown pose {pose!r}")
    if pm is None or pose == "empty":
        return smooth.station(data, None, "empty", lamp, 0, width, height)
    rigs = [rig for rig in fig.pose_rigs(pose) if rig is not None]
    gap = data.gaps.get(pose, fig.GAPS[pose])
    if width < 3 or height < 3:
        return [empty_frame(width, height)], gap
    pictures = pm.frames(pose)
    n = len(pictures) if len(pictures) > 1 else len(rigs)
    lamp_c = rgb(lamp)
    count = max(0, min(3, ensigns))
    looks = [smooth.ensign_look(data, k, pm.member.uniform) for k in range(count)]
    per = scene.ensign_step(n, gap, 420)
    # a still follows the pose's head side to side (a jolt shakes it, waving and standing sway it); a pack's
    # own frames bring their own motion
    centre = sum(rig.head[0] for rig in rigs) / len(rigs)
    sway = [round(rig.head[0] - centre, 2) if len(pictures) == 1 else 0.0 for rig in rigs]
    fitted: dict[tuple[int, float], Fitted | None] = {}  # the same picture at the same shift, scaled once
    hide: Array | None = None
    frames = []
    for i in range(n):
        step = i * len(rigs) // n
        rig = rigs[step]
        cv = Canvas(width, height, fig.DESIGN)
        light = Light(lamp_c, smooth.LAMP_AT, lamp_power=0.95 * rig.lamp)
        scene.draw_chair_back(cv, light, data.colors)
        scene.draw_console(cv, light, data.colors, lamp_c, rig)
        shake = sway[step]
        which = (i % len(pictures), shake)
        if which not in fitted:
            if hide is None:
                box = cv.region(0.0, 0.0, fig.DESIGN[0], fig.DESIGN[1])
                hide = scene.front_cover(cv, box) if box is not None else np.zeros((0, 0), F32)
            fitted[which] = fit_picture(cv, pictures[which[0]], shake, hide)
        fit = fitted[which]
        if fit is not None:
            lit = smooth.member_light(lamp_c, rig.lamp * data.lamp_light) if data.lamp_light > 0 else None
            draw_picture(cv, fit, lit)
        for k, look in enumerate(looks):
            scene.draw_ensign(cv, light, look, scene.ENSIGN_SLOTS[k], 1.4 if (i // per + k) % 2 == 0 else 0.0)
        wanted = tuple(f for f in rig.fx if f[0] not in NOT_OVER_PICTURES and (data.effects or f[0] in CONSOLE_FX))
        scene.draw_fx(cv, replace(rig, fx=wanted))
        frames.append(cv.frame())
    if len(set(frames)) == 1:
        frames = frames[:1]
    return frames, gap


