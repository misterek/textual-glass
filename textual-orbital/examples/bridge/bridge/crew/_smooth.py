"""The ``smooth`` crew style, the built-in crew: every sprite drawn at the exact pixel size asked for, from
signed-distance shapes, anti-aliased, softly shaded and lit by the console's lamp. A pack of this style
describes each member by parameters (colours, a hair style, extras, a build); see ``PACKS.md``.

``load_smooth`` reads a pack folder; ``station``, ``chair`` and ``viewscreen`` are the sprite makers behind
``Pack`` (the figure is in ``_figure``, the furniture and effects in ``_scene``, the viewscreen in ``_space``).
The picture style (``_picture``) draws its stations with the same furniture, through ``load_scene`` and the
``Scene`` protocol, and ``member_picture`` renders a member on its own for the export tool. Pure numpy, no
Textual.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from . import _figure as fig
from . import _scene as scene
from ._colors import ColorError, Palette, to_hex
from ._pack import (
    POSES,
    Member,
    PackError,
    check_ids,
    check_keys,
    color_table,
    expect,
    read_manifest,
    read_member,
)
from ._paint import WHITE, Array, Canvas, Light, empty_frame, mix, rgb
from ._space import viewscreen as _viewscreen

SMOOTH_ROOT = Path(__file__).parent / "smooth"
STYLE = "smooth"

_MANIFEST_KEYS = ("name", "style", "extends", "colors", "members", "ensigns", "about")
_MEMBER_KEYS = ("id", "name", "uniform", "colors", "hair_style", "extras", "build", "about")
_ENSIGN_KEYS = ("colors", "hair_style", "about")

#: The named colours of a smooth pack (its ``colors`` override them; an extending pack inherits its base's).
DEFAULT_COLORS = {
    "skin": "#f2c6a0", "hair": "#3a2a24", "eyes": "#1d1a2e", "beard": "hair*0.8", "trim": "uniform+0.55",
    "mouth": "skin~#7a2232:0.62", "cheek": "skin~#ff5a6e:0.45", "freckle": "skin~#8a3a1c:0.5",
    "frames": "#262335", "gear": "#b8c2d8", "uniform": "#8a94aa",
    "mug": "#f1ece2", "mug_band": "#e86a6a", "tea": "#8a5530",
    "console": "#3c4766", "chair": "#4b4569", "captain": "#3d3a63",
    "light1": "#5fe2ff", "light2": "#86ff9e", "light3": "#ff86d8", "light4": "#ffd166",
    "waiting": "#ffb238", "waiting_dim": "#c27a1e", "indicator_off": "#2a2738",
    "space_top": "#0c1636", "space_bottom": "#03050e", "nebula": "#4a2a8a", "star": "#eef3ff",
    "planet": "#6fcab8", "planet_dark": "#15304a", "ring": "#c9b8f0", "alert": "#ff3048", "bezel": "#353d5a",
}
_MEMBER_COLORS = ("skin", "hair", "eyes", "beard", "trim", "mouth", "cheek", "freckle", "frames", "gear")
_SCENE_COLORS = ("mug", "mug_band", "tea", "console", "chair", "captain", "light1", "light2", "light3", "light4",
                 "waiting", "waiting_dim", "indicator_off", "space_top", "space_bottom", "nebula", "star", "planet",
                 "planet_dark", "ring", "alert", "bezel")
#: Where the console lamp's light comes from: the screen in the console's top, in front of the member.
LAMP_AT = (fig.CX, fig.DESK + 1.0, 15.0)


@dataclass(frozen=True)
class SmoothMember:
    member: Member
    look: fig.Look
    colors: dict[str, str] = field(default_factory=dict)  # as written in the pack (to resolve again)


Ensigns = list[tuple[dict[str, str], str]]  # (colours, hair style) per slot


class Scene(Protocol):
    """What the furniture needs from a pack of either style: the scene's resolved colours, the ensigns and the
    colour expressions they are resolved against."""

    @property
    def colors(self) -> dict[str, Array]: ...

    @property
    def ensigns(self) -> Ensigns: ...

    @property
    def palette(self) -> dict[str, str]: ...


@dataclass(frozen=True)
class SmoothData:
    name: str
    root: Path
    colors: dict[str, Array]  # the scene's colours, resolved
    members: list[SmoothMember]
    ensigns: Ensigns
    palette: dict[str, str]  # the pack's colour expressions


# ================================================================ loading ==


def is_smooth(folder: Path) -> bool:
    """Whether the pack in ``folder`` says ``"style": "smooth"`` (anything unreadable counts as no)."""
    try:
        manifest = json.loads((folder.expanduser() / "pack.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(manifest, dict) and manifest.get("style") == STYLE


def load_scene(manifest: dict[str, Any], m: str, base: Scene | None = None) -> tuple[dict[str, Array], Ensigns,
                                                                                    dict[str, str]]:
    """The scene of a pack of either style: its ``colors`` over the base's (or the defaults), resolved for the
    furniture and the viewscreen, and its ``ensigns`` (the base's, or one in the default look)."""
    palette = dict(base.palette) if base else dict(DEFAULT_COLORS)
    palette.update(color_table(manifest.get("colors", {}), f"{m}: colors"))
    pal = Palette(palette)
    colors: dict[str, Array] = {}
    for key in _SCENE_COLORS:
        try:
            colors[key] = rgb(pal.get(key))
        except ColorError as e:
            raise PackError(f"{m}: colors.{key}: {e}") from None
    ensigns: Ensigns
    if "ensigns" in manifest:
        ensigns = []
        for i, spec in enumerate(expect(manifest["ensigns"], list, m, "ensigns")):
            where = f"{m}: ensigns[{i}]"
            expect(spec, dict, where, "an ensign")
            check_keys(spec, _ENSIGN_KEYS, where)
            ensigns.append((color_table(spec.get("colors", {}), where), _style(spec.get("hair_style", "crop"), where)))
    else:
        ensigns = list(base.ensigns) if base is not None else []
    if not ensigns:
        ensigns = [({}, "crop")]
    for i, (ec, style) in enumerate(ensigns):
        _look(Member("ensign", "", (128, 128, 128)), ec, palette, style, (), 0, 0, f"{m}: ensigns[{i}]")
    return colors, ensigns, palette


def load_smooth(path: Path | None = None, _chain: tuple[Path, ...] = ()) -> SmoothData:
    """The smooth pack in ``path`` (the built-in one for None). Everything is checked now, so a broken pack
    fails here with a ``PackError`` naming the file and the problem."""
    root = (path or SMOOTH_ROOT).expanduser().resolve()
    if root in _chain:
        raise PackError(f"{root}: packs extend each other in a loop")
    manifest, m = read_manifest(root)
    check_keys(manifest, _MANIFEST_KEYS, m)
    name = expect(manifest.get("name"), str, m, "name")
    if manifest.get("style") != STYLE:
        raise PackError(f"{m}: style should be {STYLE!r}")

    base: SmoothData | None = None
    if "extends" in manifest:
        ext = expect(manifest["extends"], str, m, "extends")
        base_root = SMOOTH_ROOT if ext == STYLE else (root / ext).expanduser().resolve()
        if not is_smooth(base_root):
            raise PackError(f"{m}: extends {ext!r}, which is not a smooth pack (\"smooth\" or a path to one)")
        base = load_smooth(base_root, (*_chain, root))

    colors, ensigns, palette = load_scene(manifest, m, base)
    if "members" in manifest:
        raw = expect(manifest["members"], list, m, "members")
        if not raw:
            raise PackError(f"{m}: members is empty")
        members = [_member(r, i, palette, m) for i, r in enumerate(raw)]
    elif base is not None:  # the base's crew, in this pack's colours
        members = [replace(sm, look=_look(sm.member, sm.colors, palette, sm.look.hair_style, sm.look.extras,
                                          sm.look.wide, sm.look.tall, f"{m}: member {sm.member.id!r}"))
                   for sm in base.members]
    else:
        raise PackError(f"{m}: members is required")
    check_ids([sm.member for sm in members], m)
    return SmoothData(name, root, colors, members, ensigns, palette)


def _style(value: Any, where: str) -> str:
    expect(value, str, where, "hair_style")
    if value not in fig.HAIR_STYLES:
        raise PackError(f"{where}: hair_style {value!r} is not one of {', '.join(fig.HAIR_STYLES)}")
    return str(value)


def _member(raw: Any, i: int, palette: dict[str, str], m: str) -> SmoothMember:
    member, raw, where = read_member(raw, i, m, _MEMBER_KEYS)
    colors = color_table(raw.get("colors", {}), f"{where}.colors")
    style = _style(raw.get("hair_style", "crop"), where)
    extras = expect(raw.get("extras", []), list, where, "extras")
    for x in extras:
        expect(x, str, where, "an extra")
        if x not in fig.EXTRAS:
            raise PackError(f"{where}: extra {x!r} is not one of {', '.join(fig.EXTRAS)}")
    build = expect(raw.get("build", {}), dict, where, "build")
    check_keys(build, ("wide", "tall"), f"{where}.build")
    wide = expect(build.get("wide", 0), int, where, "build.wide")
    tall = expect(build.get("tall", 0), int, where, "build.tall")
    if not -1 <= wide <= 2 or not -1 <= tall <= 1:
        raise PackError(f"{where}: build.wide is -1 to 2 and build.tall is -1 to 1")
    return SmoothMember(member, _look(member, colors, palette, style, tuple(extras), wide, tall, where), colors)


def _look(member: Member, colors: dict[str, str], palette: dict[str, str], style: str, extras: tuple[str, ...],
          wide: int, tall: int, where: str) -> fig.Look:
    pal = Palette(colors, {"uniform": to_hex(member.uniform)}, palette)
    got: dict[str, Array] = {}
    for key in _MEMBER_COLORS:
        try:
            got[key] = rgb(pal.get(key))
        except ColorError as e:
            raise PackError(f"{where}: colors.{key}: {e}") from None
    return fig.Look(uniform=rgb(member.uniform), hair_style=style, extras=extras, wide=wide, tall=tall, **got)


def ensign_look(data: Scene, k: int, uniform: tuple[int, int, int]) -> fig.Look:
    colors, style = data.ensigns[k % len(data.ensigns)]
    return _look(Member(f"ensign{k}", "", uniform), colors, data.palette, style, (), 0, 0, f"ensign {k}")


# ================================================================ the sprite makers ==


def member_light(lamp: Array, power: float) -> Light:
    """The light on a member: the station's, with the lamp's glow softened (the console screen's light on
    their face and chest), at ``power`` (the rig's; 0 for a picture lit only by the room)."""
    return Light(mix(lamp, WHITE, 0.2), LAMP_AT, lamp_power=0.5 * power)


def station(data: Scene, sm: SmoothMember | None, pose: str, lamp: tuple[int, int, int], ensigns: int,
            width: int, height: int) -> tuple[list[bytes], int]:
    """A console, its chair and, unless ``sm`` is None, the member in ``pose``, lit by ``lamp``, with
    ``ensigns`` (0 to 3) helpers in front: one frame per step of the pose."""
    if pose not in POSES:
        raise ValueError(f"unknown pose {pose!r}")
    if sm is None:
        pose, ensigns = "empty", 0
    rigs = fig.pose_rigs(pose)
    gap = fig.GAPS[pose]
    if width < 3 or height < 3:
        return [empty_frame(width, height)] * len(rigs), gap
    lamp_c = rgb(lamp)
    looks = [ensign_look(data, k, sm.member.uniform) for k in range(max(0, min(3, ensigns)))] if sm else []
    per = scene.ensign_step(len(rigs), gap, 420)
    frames = []
    for i, rig in enumerate(rigs):
        cv = Canvas(width, height, fig.DESIGN)
        power = rig.lamp if rig is not None else 0.0
        light = Light(lamp_c, LAMP_AT, lamp_power=0.95 * power)
        lit = member_light(lamp_c, power)
        scene.draw_chair_back(cv, light, data.colors)
        if rig is not None and sm is not None:
            rig = fig.built(rig, sm.look)
            _draw_member(cv, lit, sm.look, rig, data.colors)
        scene.draw_console(cv, light, data.colors, lamp_c, rig)
        if rig is not None and sm is not None:
            _draw_over_arms(cv, lit, sm.look, rig)
        for k, look in enumerate(looks):
            scene.draw_ensign(cv, light, look, scene.ENSIGN_SLOTS[k], 1.4 if (i // per + k) % 2 == 0 else 0.0)
        if rig is not None:
            scene.draw_fx(cv, rig)
        frames.append(cv.frame())
    return frames, gap


def _draw_member(cv: Canvas, light: Light, look: fig.Look, rig: fig.Rig, colors: dict[str, Array]) -> None:
    """The member, back to front: hair behind the head, the body, the head, arms in front of it all, a mug in
    the hands (arms resting on the console are drawn after the console, ``_draw_over_arms``)."""
    hs = fig.head_space(cv, rig, extra=8.0)
    if hs is not None:
        fig.draw_hair_back(cv, light, look, rig, hs)
    fig.draw_torso(cv, light, look, rig)
    fig.draw_neck(cv, light, look, rig)
    if hs is not None:
        fig.draw_head(cv, light, look, rig, hs)
    for arm in rig.arms:
        if arm.layer == "front":
            fig.draw_arm(cv, light, look, arm, rig.body)
    if rig.mug is not None:  # held up in both hands, in front of them
        fig.draw_mug(cv, light, colors, rig.mug, rig.body)


def _draw_over_arms(cv: Canvas, light: Light, look: fig.Look, rig: fig.Rig) -> None:
    """The arms that rest on the console's top, drawn over it."""
    for arm in rig.arms:
        if arm.layer == "over":
            fig.draw_arm(cv, light, look, arm, rig.body)


def member_picture(data: Scene, sm: SmoothMember, pose: str, frame: int, width: int, height: int) -> Array:
    """The member alone, as a picture pack's picture of them (``_picture``, ``tools/export_pictures.py``): frame
    ``frame`` of ``pose`` drawn on the whole ``width`` x ``height`` canvas (the station's 116 x 100 box, no
    margin), lit by the room but not the lamp (the renderer adds the lamp's light), without the furniture or
    the effects, and cut where the console's top plate would hide them, so the picture sits behind the console
    the way the smooth member does. (h, w, 4) uint8, straight alpha, full colour."""
    if pose not in POSES[1:]:
        raise ValueError(f"unknown pose {pose!r}")
    rigs = fig.pose_rigs(pose)
    rig = rigs[frame % len(rigs)]
    assert rig is not None
    rig = fig.built(rig, sm.look)
    cv = Canvas(width, height, fig.DESIGN, fit="exact")
    light = member_light(rgb((0, 0, 0)), 0.0)
    _draw_member(cv, light, sm.look, rig, data.colors)
    reg = cv.everywhere()
    keep = 1 - scene.plate_cover(cv, reg)
    cv.rgb[reg.sl] *= keep[..., None]
    cv.a[reg.sl] *= keep
    _draw_over_arms(cv, light, sm.look, rig)
    return cv.image(edge=False, pack=False)


def chair(data: Scene, waiting: int, width: int, height: int) -> tuple[list[bytes], int]:
    """The captain's chair: a still while nobody waits, else two frames of its waiting lights pulsing."""
    pulses = 2 if waiting > 0 else 1
    gap = 380 if waiting >= 3 else 650
    if width < 3 or height < 3:
        return [empty_frame(width, height)] * pulses, gap
    frames = []
    for i in range(pulses):
        cv = Canvas(width, height, scene.CHAIR_DESIGN)
        scene.captain_chair(data.colors, waiting, i == 0, cv)
        frames.append(cv.frame())
    return frames, gap


def viewscreen(data: Scene, alert: bool, width: int, height: int) -> tuple[list[bytes], int]:
    return _viewscreen(data.colors, alert, width, height)
