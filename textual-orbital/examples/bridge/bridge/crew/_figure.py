"""The smooth crew member: a rig per pose and frame (where the head, the shoulders, the arms and the hands
are, and what the face does), and the painter that draws a member on that rig from signed-distance shapes,
shaded and lit (``_paint``). Pure numpy.

Coordinates are design units in the station's 116 x 100 box, y down. The member is the subject: a seated
head is centred at (58, 41) with radius ``HEAD_R`` (23, nearly half the box's height across), in front of a
low, slim console whose top edge is at ``DESK``. Body sizes (limbs, torso) are ``BODY`` times the first
cut's. Faces and hair are drawn in head units (the head's radius is 1, ``u`` right, ``v`` down), so they
follow the head when it tilts or bows. A rig's ``scale`` draws the whole member smaller (standing up, so the
hair and both hands still fit the box).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Protocol

import numpy as np

from ._paint import (
    F32,
    WHITE,
    Array,
    Canvas,
    Light,
    Region,
    fill,
    mix,
    normals,
    sd_arc,
    sd_box,
    sd_capsule,
    sd_circle,
    sd_ellipse,
    sd_poly,
    smin,
    smoothstep,
    soft_glow,
    union,
)

DESIGN = (116.0, 100.0)
CX = 58.0
DESK = 79.0  # the console's top edge
HEAD_R = 23.0
BODY = 1.35  # limbs and torso, against the first cut's (HEAD_R was 15.5)
HAIR_STYLES = ("bob", "curly", "spiky", "bun", "crop", "ponytail")
EXTRAS = ("glasses", "headset", "beard", "freckles")

P = tuple[float, float]


@dataclass(frozen=True)
class Look:
    """What a member (or an ensign) looks like: colours as float RGB, a hair style, extras and a build."""

    skin: Array
    hair: Array
    eyes: Array
    uniform: Array
    trim: Array
    beard: Array
    mouth: Array
    cheek: Array
    freckle: Array
    frames: Array
    gear: Array
    hair_style: str = "crop"
    extras: tuple[str, ...] = ()
    wide: int = 0
    tall: int = 0


@dataclass(frozen=True)
class Face:
    eyes: str = "open"  # open, blink, happy, closed, wide, down, sleep
    look: P = (0.0, 0.0)  # where the pupils look (head units)
    brows: float = 0.0  # raised (head units, up is +)
    frown: float = 0.0  # inner ends down (+) or up (-)
    mouth: str = "smile"  # smile, content, grin, flat, o, shout, sleep
    blush: float = 0.3


@dataclass(frozen=True)
class Arm:
    shoulder: P
    elbow: P
    hand: P
    layer: str = "front"  # front: over the body; over: over the console too
    hand_kind: str = "fist"  # fist, open (palm up, fingers), hidden (tucked away)


@dataclass(frozen=True)
class Rig:
    """One frame of a pose."""

    head: P
    tilt: float
    face: Face
    top: float  # the shoulder line
    arms: tuple[Arm, Arm]
    lean: float = 0.0  # the body's x offset
    bow: float = 0.0  # the head bowed forward (dozing): head units the face drops
    mug: P | None = None
    fx: tuple[tuple[str, float, float, float], ...] = ()  # (kind, x, y, k)
    lamp: float = 1.0  # the lamp's light this frame
    lights: int = 0  # which console lights blink (a pattern number)
    jitter: float = 0.0
    standing: bool = False  # up out of the chair: a tall build doesn't rise further (it would leave the box)
    scale: float = 1.0  # the member's size: under 1, smaller (standing, to fit the box)

    @property
    def head_r(self) -> float:
        return HEAD_R * self.scale

    @property
    def body(self) -> float:
        return BODY * self.scale


# ================================================================ poses ==

HEAD_Y = 41.0  # a seated head's centre
TOP = 64.0  # a seated member's shoulder line
L_SH, R_SH = (CX - 18.0, TOP + 4.5), (CX + 18.0, TOP + 4.5)


def _rest(side: int, lift: float = 0.0, dx: float = 0.0) -> Arm:
    """An arm resting on the console, hand on its top (side -1: the viewer's left)."""
    s = L_SH if side < 0 else R_SH
    return Arm(s, (CX + side * 26.5, DESK + 0.5), (CX + side * 13.5 + dx, DESK + 4.0 - lift), "over")


GAPS = {"empty": 1000, "seated": 500, "typing": 150, "done": 900, "hand_up": 420, "waving": 260, "standing": 240,
        "jolt": 170, "tea": 380, "dozing": 650}


def pose_rigs(pose: str) -> list[Rig | None]:
    """The frames of a pose (None: nobody there)."""
    if pose == "empty":
        return [None]
    return _POSES[pose]()


def _seated() -> list[Rig | None]:
    out: list[Rig | None] = []
    for i, breath in enumerate((0.0, 0.3, 0.6, 0.6, 0.3, 0.0)):
        face = Face("blink" if i == 5 else "open", mouth="content", blush=0.3)
        out.append(Rig((CX, HEAD_Y - breath * 0.6), 0.0, face, TOP - breath, (_rest(-1), _rest(1)), lamp=0.85))
    return out


def _typing() -> list[Rig | None]:
    out: list[Rig | None] = []
    lifts = ((2.2, 0.0), (0.4, 0.4), (0.0, 2.2), (0.4, 0.4))
    for i, (left, right) in enumerate(lifts):
        face = Face("down", look=(0.02 * (1 if i < 2 else -1), 0.05), mouth="flat", blush=0.2)
        arms = (_rest(-1, left, dx=1.2 if i == 1 else 0.0), _rest(1, right, dx=-1.2 if i == 3 else 0.0))
        out.append(Rig((CX, HEAD_Y + 1.0 + 0.3 * (i % 2)), 0.03, face, TOP + 0.4, arms,
                       lamp=(1.0, 1.15, 0.95, 1.1)[i], lights=i + 1))
    return out


def _done() -> list[Rig | None]:
    out: list[Rig | None] = []
    for i in range(2):
        b = 0.6 * i
        face = Face("happy", look=(0.0, -0.03), brows=0.04, mouth="smile", blush=0.55)
        arms = (Arm((L_SH[0] - 0.5, L_SH[1] + 0.5 - b), (CX - 24.0, 75.5 - b), (CX + 13.0, 71.5 - b), "front",
                    "hidden"),
                Arm((R_SH[0] + 0.5, R_SH[1] + 0.5 - b), (CX + 24.0, 75.0 - b), (CX - 12.5, 71.0 - b), "front",
                    "hidden"))
        out.append(Rig((CX, HEAD_Y - 1.0 - b), -0.07, face, TOP + 0.5 - b, arms, lamp=0.9,
                       fx=(("sparkle", CX + 32.0, 19.0, 1.0 - i),)))
    return out


def _hand_up() -> list[Rig | None]:
    out: list[Rig | None] = []
    for i in range(2):
        w = 1.5 * i
        face = Face("open", look=(0.05, -0.04), brows=0.1, mouth="o", blush=0.35)
        up = Arm((R_SH[0] + 0.5, R_SH[1] - 5.0), (CX + 31.0, 40.0), (CX + 33.0 + w, 11.0), "front", "open")
        fx = (("bang", CX + 47.0, 25.0, 1.0),) if i == 0 else ()
        out.append(Rig((CX - 2.5, HEAD_Y + 1.0), 0.08, face, TOP - 0.5, (_rest(-1), up), fx=fx, lamp=1.05))
    return out


def _waving() -> list[Rig | None]:
    out: list[Rig | None] = []
    for i in range(2):
        hand = (CX + 41.0, 12.0) if i == 0 else (CX + 24.0, 10.0)
        face = Face("open", look=(0.06, -0.04), brows=0.12, mouth="grin", blush=0.5)
        up = Arm((R_SH[0] + 0.5, R_SH[1] - 5.0), (CX + 32.0, 41.0), hand, "front", "open")
        fx = (("arcs", hand[0], hand[1] - 1.0, 1.0 if i == 0 else -1.0),)
        out.append(Rig((CX - 2.5 + 0.8 * i, HEAD_Y + 1.0), 0.08 - 0.05 * i, face, TOP - 0.5, (_rest(-1), up),
                       fx=fx))
    return out


def _standing() -> list[Rig | None]:
    """Up out of the chair, both arms waving overhead: drawn a little smaller, so the hair and the hands fit."""
    out: list[Rig | None] = []
    k = 0.84
    for i in range(2):
        s = 1 if i == 0 else -1
        face = Face("open", look=(0.0, -0.03), brows=0.16, mouth="shout", blush=0.6)
        head_y = 33.5
        top = head_y + HEAD_R * k + 0.5
        lh = (CX - 31.0 - 3.0 * s, 12.0 + 1.5 * s)
        rh = (CX + 31.0 - 3.0 * s, 12.0 - 1.5 * s)
        arms = (Arm((CX - 15.0, top + 4.0), (CX - 26.0, top - 12.0), lh, "front", "open"),
                Arm((CX + 15.0, top + 4.0), (CX + 26.0, top - 12.0), rh, "front", "open"))
        fx = (("arcs", lh[0], lh[1] - 1.0, -1.0), ("arcs", rh[0], rh[1] - 1.0, 1.0)) if i == 0 else \
            (("bang", CX + 48.0, 26.0, 1.0), ("bang", CX - 48.0, 26.0, 1.0))
        out.append(Rig((CX + 0.8 * s, head_y), 0.04 * s, face, top, arms, fx=fx, lamp=1.1, standing=True, scale=k))
    return out


def _jolt() -> list[Rig | None]:
    out: list[Rig | None] = []
    shakes = (-1.4, 1.2, -0.7, 0.6)
    for i, sx in enumerate(shakes):
        face = Face("wide", look=(0.0, -0.02), brows=0.2, frown=-0.08, mouth="O", blush=0.0)
        arms = (Arm(L_SH, (CX - 31.0, 63.0 + sx * 0.6), (CX - 41.0, 54.0 - sx), "front", "open"),
                Arm(R_SH, (CX + 31.0, 63.0 - sx * 0.6), (CX + 41.0, 54.0 + sx), "front", "open"))
        fx = (("spark", 20.0 if i % 2 == 0 else 96.0, DESK + 1.5, 1.0 - 0.3 * (i // 2)),
              ("spark", 94.0 if i % 2 == 0 else 26.0, DESK + 7.0, 0.55),
              ("smoke", 30.0 + 4.0 * i, DESK - 9.0 - 4.0 * i, i / 4))
        out.append(Rig((CX + sx, HEAD_Y - 0.5), -0.1 * sx, face, TOP - 0.5, arms, lean=sx * 0.5, fx=fx,
                       lamp=(1.5, 0.5, 1.3, 0.7)[i], lights=7 + i, jitter=sx))
    return out


def _tea() -> list[Rig | None]:
    out: list[Rig | None] = []
    for i in range(3):
        face = Face("closed", look=(0.0, 0.03), mouth="content", blush=0.55)
        mug = (CX + 17.0, 72.5)
        arms = (Arm(L_SH, (CX - 21.0, 78.5), (mug[0] - 6.5, mug[1] + 4.5), "front", "fist"),
                Arm(R_SH, (CX + 28.0, 79.0), (mug[0] + 6.0, mug[1] + 5.0), "front", "fist"))
        out.append(Rig((CX - 3.0, HEAD_Y), -0.07, face, TOP + 0.2, arms, mug=mug,
                       fx=(("steam", mug[0] + 1.0, mug[1] - 9.0, i / 3),), lamp=0.9))
    return out


def _dozing() -> list[Rig | None]:
    out: list[Rig | None] = []
    for i in range(3):
        b = (0.0, 0.6, 0.3)[i]
        face = Face("sleep", mouth="sleep", blush=0.4)
        arms = (Arm((L_SH[0] - 1.0, L_SH[1] + 5.0 - b), (CX - 27.0, DESK + 1.0), (CX + 12.0, DESK + 0.5), "over",
                    "hidden"),
                Arm((R_SH[0] + 1.0, R_SH[1] + 5.0 - b), (CX + 27.0, DESK + 1.5), (CX - 12.0, DESK + 1.0), "over",
                    "hidden"))
        fx = tuple(("zee", CX + 33.0 + 6.0 * k, 50.0 - 12.0 * k - 3.0 * i / 3, 0.6 + 0.25 * k + 0.08 * i)
                   for k in range(3) if (k + i) % 3 != 2)
        out.append(Rig((CX - 1.0, 57.0 - b * 0.6), 0.16, face, TOP + 5.0 - b, arms, bow=0.32, fx=fx, lamp=0.7))
    return out


_POSES = {"seated": _seated, "typing": _typing, "done": _done, "hand_up": _hand_up, "waving": _waving,
          "standing": _standing, "jolt": _jolt, "tea": _tea, "dozing": _dozing}


def built(rig: Rig, look: Look) -> Rig:
    """The rig for a broader, slimmer, taller or shorter member: shoulders out, everything above the desk up."""
    dw, dt = 2.6 * BODY * look.wide * rig.scale, 0.0 if rig.standing else 2.0 * look.tall

    def lift(p: P, side: float) -> P:
        return p[0] + side * dw, p[1] - (dt if p[1] < DESK - 1 else 0.0)

    arms = []
    for k, arm in enumerate(rig.arms):
        side = -1.0 if k == 0 else 1.0
        hand_side = side if arm.layer != "over" or arm.hand_kind != "hidden" else 0.0
        arms.append(replace(arm, shoulder=lift(arm.shoulder, side), elbow=lift(arm.elbow, side),
                            hand=lift(arm.hand, hand_side * 0.5)))
    hx, hy = rig.head
    return replace(rig, head=(hx, hy - dt), top=rig.top - dt, arms=(arms[0], arms[1]))


# ================================================================ the head ==


@dataclass
class HeadSpace:
    """A region round the head, and its head-unit coordinates there (``u`` right, ``v`` down, radius 1)."""

    reg: Region
    u: Array
    v: Array
    r: float  # design units per head unit


def head_space(cv: Canvas, rig: Rig, extra: float = 0.0) -> HeadSpace | None:
    hx, hy = rig.head
    r = rig.head_r
    reg = cv.region(hx - 2.0 * r - extra, hy - 1.95 * r, hx + 2.0 * r + extra, hy + 2.0 * r)
    if reg is None:
        return None
    c, s = math.cos(rig.tilt), math.sin(rig.tilt)
    dx, dy = reg.x - hx, reg.y - hy
    u = (dx * c + dy * s) / r
    v = (-dx * s + dy * c) / r
    return HeadSpace(reg, reg.full(u).astype(F32), reg.full(v).astype(F32), r)


def face_shape(u: Array, v: Array) -> Array:
    return smin(sd_circle(u, v, 0.0, -0.05, 1.0), sd_ellipse(u, v, 0.0, 0.3, 0.86, 0.72), 0.25)


def hair_shapes(style: str, u: Array, v: Array, bow: float) -> tuple[Array | None, Array]:
    """The hair behind the head (or None) and in front of it, in head units."""
    if style == "bob":
        outer = sd_box(u, v, 0.0, -0.15, 1.2, 1.02, 0.95)
        fringe = -0.3 + bow + 0.06 * np.cos(u * 8.5) + 0.05 * u
        window = np.maximum(sd_box(u, v, 0.0, 0.5, 0.79, 0.95, 0.5), fringe - v)
        return outer, np.maximum(outer, -window)
    if style == "curly":
        puffs = [sd_ellipse(u, v, 0.0, -0.3, 1.2, 0.9)]
        for a in np.radians(np.arange(-205.0, 26.0, 23.0)):
            puffs.append(sd_circle(u, v, 1.2 * math.cos(a), -0.28 + 0.92 * math.sin(a), 0.4))
        back = union(*puffs)
        cap = np.maximum(sd_circle(u, v, 0.0, -0.22, 1.12), v + 0.48 - bow)
        curls = [sd_circle(u, v, x, -0.46 + bow - 0.16 * (1 - x * x) + 0.02 * math.cos(x * 9), 0.23)
                 for x in np.linspace(-0.86, 0.86, 7)]
        return back, union(cap, *curls)
    if style == "spiky":
        back = sd_circle(u, v, 0.0, -0.1, 1.08)
        cap = np.maximum(sd_circle(u, v, 0.0, -0.12, 1.1), v + 0.3 - bow + 0.09 * np.cos(u * 7.0))
        spikes = []
        for k, a in enumerate(np.radians((-168.0, -143.0, -118.0, -93.0, -68.0, -43.0, -18.0))):
            reach = 1.4 + 0.1 * ((k * 7) % 3 - 1)
            sweep = a + 0.18
            b1 = (0.78 * math.cos(a - 0.24), -0.12 + 0.78 * math.sin(a - 0.24))
            b2 = (0.78 * math.cos(a + 0.24), -0.12 + 0.78 * math.sin(a + 0.24))
            tip = (reach * math.cos(sweep), -0.12 + reach * math.sin(sweep))
            spikes.append(sd_poly(u, v, (b1, tip, b2)))
        bangs = [sd_poly(u, v, ((x - 0.17, -0.5 + bow), (x + 0.02 + 0.1 * x, -0.12 + bow - 0.06 * abs(x)),
                                (x + 0.19, -0.5 + bow))) for x in (-0.55, -0.18, 0.2, 0.55)]
        return back, smin(cap, union(*spikes, *bangs) - 0.03, 0.1)
    if style == "bun":
        back = np.minimum(sd_circle(u, v, 0.0, -0.1, 1.07), sd_circle(u, v, 0.1, -1.22, 0.42))
        cap = np.maximum(sd_circle(u, v, 0.0, -0.1, 1.09), v - (-0.42 + bow + 0.12 * u + 0.12 * u * u))
        return back, cap
    if style == "ponytail":
        back = sd_circle(u, v, 0.0, -0.09, 1.08)
        tail = union(sd_circle(u, v, 0.86, -0.82, 0.3),
                     sd_capsule(u, v, 0.95, -0.8, 1.32, -0.35, 0.27, 0.25),
                     sd_capsule(u, v, 1.32, -0.35, 1.4, 0.4, 0.25, 0.19),
                     sd_capsule(u, v, 1.4, 0.4, 1.2, 1.15, 0.19, 0.05))
        back = smin(back, tail, 0.1)
        t = np.clip((u + 1.0) / 2.0, 0, 1)
        sweep = -0.1 + bow - 0.44 * t ** 1.4 + 0.05 * np.cos(u * 9.0)
        cap = np.maximum(sd_circle(u, v, 0.0, -0.11, 1.11), v - sweep)
        lock = sd_capsule(u, v, 0.2, -0.82, -0.9, 0.12 + bow, 0.22, 0.06)
        return back, smin(cap, lock, 0.14)
    # crop: short, a hairline lower at the temples, short sideburns
    line = -0.5 + bow + 0.2 * u * u + 0.03 * np.cos(u * 11.0)
    cap = np.maximum(sd_circle(u, v, 0.0, -0.08, 1.07), v - line)
    burns = np.minimum(sd_capsule(u, v, -0.97, -0.35, -0.97, -0.08, 0.09, 0.06),
                       sd_capsule(u, v, 0.97, -0.35, 0.97, -0.08, 0.09, 0.06))
    return None, smin(cap, burns, 0.1)


#: How thick each style's hair is (head units): how far in from its edge it rounds over.
HAIR_THICK = {"bob": 0.36, "curly": 0.3, "spiky": 0.3, "bun": 0.22, "crop": 0.12, "ponytail": 0.3}


def _hair_albedo(look: Look, hs: HeadSpace, style: str) -> Array:
    """Hair colour with a little texture: strands falling from the crown, or curls."""
    base = look.hair * np.ones((*hs.reg.shape, 1), F32)
    if style == "curly":
        t = 0.5 + 0.5 * np.sin(hs.u * 15.0 + np.sin(hs.v * 9.0) * 1.5) * np.sin(hs.v * 15.0 + np.cos(hs.u * 7.0))
        return base * (0.84 + 0.26 * t)[..., None]
    ang = np.arctan2(hs.v + 1.3, hs.u)
    t = 0.5 + 0.5 * np.sin(ang * 34.0 + np.sin(ang * 7.0) * 2.0)
    return base * (0.94 + 0.09 * t)[..., None]


def draw_hair_back(cv: Canvas, light: Light, look: Look, rig: Rig, hs: HeadSpace) -> None:
    back, _ = hair_shapes(look.hair_style, hs.u, hs.v, rig.bow)
    if back is not None:
        fill(cv, light, hs.reg, back * hs.r, _hair_albedo(look, hs, look.hair_style) * 0.82, 0.45 * hs.r,
             wrap=0.3, gloss=0.18, shininess=12.0)


class Decal(Protocol):
    def __call__(self, d: Array, color: Array, alpha: float = 1.0, soft: float = 1.0, lit: bool = True) -> None:
        ...


def draw_head(cv: Canvas, light: Light, look: Look, rig: Rig, hs: HeadSpace) -> None:
    """Ears, the face and its features, the hair in front, glasses, a headset, a beard."""
    u, v, r, reg = hs.u, hs.v, hs.r, hs.reg
    face = face_shape(u, v)
    # the head's soft shadow on the neck and collar below it
    cv.darken(reg, 0.45 * soft_glow(reg.full(np.maximum(face_shape(u, v - 0.16) * r, 0)), 0.32 * r) * (v > 0.4))
    ears = np.minimum(sd_ellipse(u, v, -0.96, 0.2, 0.17, 0.25), sd_ellipse(u, v, 0.96, 0.2, 0.17, 0.25))
    fill(cv, light, reg, ears * r, look.skin * 0.95, 0.15 * r, wrap=0.4, warm=0.8)
    fd = reg.full(face * r)
    fn = normals(reg, fd, 0.85 * r, cv.px)
    fill(cv, light, reg, fd, look.skin, n=fn, wrap=0.42, warm=0.7, gloss=0.06, shininess=10.0)
    on_face = fd < 0.3 * cv.px

    def decal(d: Array, color: Array, alpha: float = 1.0, soft: float = 1.0, lit: bool = True) -> None:
        fill(cv, light if lit else None, reg, d * r, color, n=fn, alpha=alpha * on_face, soft=soft, wrap=0.5,
             rim=0.0)

    b = rig.bow
    f = rig.face
    if f.blush > 0:
        decal(np.minimum(sd_ellipse(u, v, -0.58, 0.44 + b, 0.19, 0.11), sd_ellipse(u, v, 0.58, 0.44 + b, 0.19, 0.11)),
              look.cheek, f.blush, soft=3.0)
    decal(sd_ellipse(u, v, 0.03, 0.34 + b, 0.06, 0.04), look.skin * 0.78, 0.5, soft=2.0)
    if "freckles" in look.extras:
        dots = [sd_circle(u, v, s * x, y + b, 0.028) for s in (-1, 1)
                for x, y in ((0.46, 0.34), (0.58, 0.3), (0.55, 0.42), (0.68, 0.38))]
        decal(union(*dots), look.freckle, 0.45, soft=1.5)
    _eyes(decal, look, f, u, v, b)
    _mouth(decal, look, f, u, v, b)
    if "beard" in look.extras:
        _beard(cv, light, look, hs, face, b)
    _, front = hair_shapes(look.hair_style, u, v, b)
    hd = reg.full(front * r)
    if (hd < cv.px).any():
        shade = np.maximum(hair_shapes(look.hair_style, u, v - 0.09, b)[1] * r, 0)
        cv.darken(reg, 0.35 * soft_glow(reg.full(shade), 0.18 * r) * (fd < 0))
        fill(cv, light, reg, hd, _hair_albedo(look, hs, look.hair_style), HAIR_THICK[look.hair_style] * r, wrap=0.3,
             gloss=0.32, shininess=14.0, rim=0.35)
    if "glasses" in look.extras:
        _glasses(cv, light, look, hs, b)
    if "headset" in look.extras:
        _headset(cv, light, look, hs)


def _eyes(paint: Decal, look: Look, f: Face, u: Array, v: Array, b: float) -> None:
    lx, ly = f.look
    brow_col = look.hair * (0.72 if float(np.mean(look.hair)) < 0.6 else 0.78)
    for s in (-1.0, 1.0):
        ex, ey = 0.37 * s, 0.13 + b
        by = ey - 0.31 - f.brows
        inner, outer = ex - 0.12 * s, ex + 0.13 * s
        if f.eyes != "sleep" or b < 0.25:
            paint(sd_capsule(u, v, inner, by - 0.025 + f.frown, outer, by + 0.005 - f.frown * 0.3, 0.045, 0.035),
                  brow_col)
        if f.eyes in ("open", "down"):
            cx, cy = ex + lx, ey + ly
            eye = sd_ellipse(u, v, cx, cy, 0.125, 0.175)
            if f.eyes == "down":
                lid = cy - 0.02
                paint(np.maximum(eye, lid - v), look.eyes)
                paint(sd_arc(u, v, cx, lid + 0.17, 0.2, -2.15, -0.99, 0.018), look.eyes * 0.8, 0.8)
                paint(sd_circle(u, v, cx - 0.04, cy + 0.07, 0.035), WHITE, 0.9, lit=False)
            else:
                paint(eye, look.eyes)
                iris = mix(look.eyes, np.array([0.55, 0.62, 0.85], F32), 0.35)
                paint(np.maximum(sd_ellipse(u, v, cx, cy + 0.07, 0.09, 0.09), eye), iris, 0.8, soft=2.0)
                paint(sd_circle(u, v, cx - 0.045, cy - 0.065, 0.052), WHITE, 0.95, lit=False)
                paint(sd_circle(u, v, cx + 0.045, cy + 0.07, 0.024), WHITE, 0.7, lit=False)
        elif f.eyes == "wide":
            cx, cy = ex + lx, ey + ly - 0.02
            paint(sd_circle(u, v, cx, cy, 0.19), look.eyes * 0.9)
            paint(sd_circle(u, v, cx, cy, 0.155), WHITE * 0.97)
            paint(sd_circle(u, v, cx + 0.01 * s, cy + 0.01, 0.065), look.eyes)
        elif f.eyes == "happy":
            paint(sd_arc(u, v, ex, ey + 0.09, 0.14, -2.75, -0.39, 0.038), look.eyes)
        elif f.eyes == "closed":
            paint(sd_arc(u, v, ex, ey - 0.06, 0.13, 0.45, 2.69, 0.034), look.eyes)
        elif f.eyes == "sleep":
            paint(sd_arc(u, v, ex, ey - 0.02, 0.12, 0.6, 2.54, 0.032), look.eyes * 0.9)
        else:  # blink
            paint(sd_capsule(u, v, ex - 0.12, ey + 0.05, ex + 0.12, ey + 0.05, 0.03), look.eyes)


def _mouth(paint: Decal, look: Look, f: Face, u: Array, v: Array, b: float) -> None:
    my = 0.53 + b
    dark = look.mouth * 0.55
    tongue = mix(look.mouth, np.array([1.0, 0.45, 0.5], F32), 0.5)
    if f.mouth == "smile":
        paint(sd_arc(u, v, 0.0, my - 0.15, 0.17, 0.62, 2.52, 0.036), look.mouth)
    elif f.mouth == "content":
        paint(sd_arc(u, v, 0.0, my - 0.1, 0.12, 0.65, 2.49, 0.033), look.mouth)
    elif f.mouth == "grin":
        m = np.maximum(sd_circle(u, v, 0.0, my - 0.07, 0.2), my - 0.05 - v)
        paint(m, dark)
        paint(np.maximum(sd_circle(u, v, 0.0, my + 0.13, 0.11), m), tongue)
        paint(np.maximum(sd_box(u, v, 0.0, my - 0.03, 0.17, 0.035, 0.02), m), WHITE * 0.95)
    elif f.mouth == "shout":
        m = sd_ellipse(u, v, 0.0, my + 0.03, 0.16, 0.15)
        paint(m, dark)
        paint(np.maximum(sd_circle(u, v, 0.0, my + 0.15, 0.1), m), tongue)
    elif f.mouth == "O":
        paint(sd_ellipse(u, v, 0.0, my + 0.04, 0.1, 0.13), dark)
    elif f.mouth == "o":
        paint(sd_ellipse(u, v, 0.0, my + 0.01, 0.07, 0.08), dark)
    elif f.mouth == "sleep":
        paint(sd_ellipse(u, v, 0.03, my, 0.05, 0.035), dark)
    else:  # flat
        paint(sd_capsule(u, v, -0.08, my, 0.08, my - 0.01, 0.03), look.mouth)


def _beard(cv: Canvas, light: Light, look: Look, hs: HeadSpace, face: Array, b: float) -> None:
    u, v, r = hs.u, hs.v, hs.r
    beard = np.maximum(face - 0.05, 0.42 + b - v - 0.36 * u * u)
    beard = smin(beard, sd_ellipse(u, v, 0.0, 0.86 + b, 0.5, 0.28), 0.15)
    beard = np.maximum(beard, -sd_ellipse(u, v, 0.0, 0.57 + b, 0.19, 0.075))
    stache = np.minimum(sd_capsule(u, v, 0.0, 0.45 + b, -0.27, 0.51 + b, 0.075, 0.045),
                        sd_capsule(u, v, 0.0, 0.45 + b, 0.27, 0.51 + b, 0.075, 0.045))
    tex = 0.94 + 0.1 * (0.5 + 0.5 * np.sin(u * 30.0 + np.sin(v * 12.0)))
    fill(cv, light, hs.reg, np.minimum(beard, stache) * r, look.beard * tex[..., None], 0.2 * r, wrap=0.35,
         gloss=0.1, rim=0.3)


def _glasses(cv: Canvas, light: Light, look: Look, hs: HeadSpace, b: float) -> None:
    u, v, r, reg = hs.u, hs.v, hs.r, hs.reg
    lenses = np.minimum(sd_box(u, v, -0.38, 0.14 + b, 0.25, 0.2, 0.13), sd_box(u, v, 0.38, 0.14 + b, 0.25, 0.2, 0.13))
    cv.over(reg, np.array([0.8, 0.9, 1.0], F32), cv.cover(reg.full(lenses * r)) * 0.16)
    glint = np.maximum(lenses + 0.03, np.abs((u + v * 0.9) % 0.76 - 0.33) - 0.045)
    cv.shine(reg, WHITE, cv.cover(reg.full(glint * r)) * 0.45)
    frame = union(np.abs(lenses) - 0.038, sd_capsule(u, v, -0.13, 0.08 + b, 0.13, 0.08 + b, 0.035),
                  sd_capsule(u, v, -0.63, 0.08 + b, -0.97, 0.05 + b, 0.035),
                  sd_capsule(u, v, 0.63, 0.08 + b, 0.97, 0.05 + b, 0.035))
    fill(cv, light, reg, frame * r, look.frames, 0.05 * r, gloss=0.5, shininess=30.0)


def _headset(cv: Canvas, light: Light, look: Look, hs: HeadSpace) -> None:
    u, v, r, reg = hs.u, hs.v, hs.r, hs.reg
    band = sd_arc(u, v, 0.0, -0.06, 1.15, -2.95, -0.19, 0.07)
    cup = sd_box(u, v, 1.07, 0.15, 0.17, 0.27, 0.15)
    boom = sd_capsule(u, v, 1.04, 0.34, 0.45, 0.68, 0.045)
    fill(cv, light, reg, union(band, cup, boom) * r, look.gear, 0.08 * r, gloss=0.45, shininess=20.0)
    fill(cv, None, reg, sd_box(u, v, 1.12, 0.15, 0.08, 0.2, 0.07) * r, look.uniform * 0.8, alpha=0.9)
    fill(cv, light, reg, sd_circle(u, v, 0.42, 0.68, 0.085) * r, look.frames, 0.05 * r, gloss=0.4)
    cv.shine(reg, light.lamp, soft_glow(reg.full(np.maximum(sd_circle(u, v, 0.42, 0.68, 0.0), 0) * r), 0.3 * r) *
             0.6, opacity=0.6)


# ================================================================ the body ==


def draw_torso(cv: Canvas, light: Light, look: Look, rig: Rig) -> None:
    """The uniform: a darker yoke over the shoulders, a collar in the trim colour, a badge on the chest. It
    stops behind the console."""
    k = rig.body
    cx = CX + rig.lean
    top = rig.top
    sw, ww = (15.0 + 2.6 * look.wide) * k, (13.0 + 2.2 * look.wide) * k
    reg = cv.region(cx - sw - 2, top - 2, cx + sw + 2, 99.0)
    if reg is None:
        return
    x, y = reg.x, reg.y
    c = 5.0 * k
    body = sd_poly(x, y, ((cx - sw + c, top + c), (cx + sw - c, top + c), (cx + ww - 4 * k, 96.0),
                          (cx - ww + 4 * k, 96.0))) - c
    yoke = smoothstep(0.6, -0.6, reg.full(y - (top + 5.5 * k + 0.06 * np.abs(x - cx))))
    tone = mix(look.uniform * np.ones((*reg.shape, 1), F32), look.uniform * 0.62, yoke * 0.85)
    fill(cv, light, reg, body, tone, 7.0 * k, wrap=0.3, gloss=0.08)
    fill(cv, light, reg, sd_arc(x, y, cx, top - 1.2 * k, 5.6 * k, 0.25, 2.89, 1.1 * k), look.trim, 1.0 * k,
         wrap=0.4)
    bx, by = cx + 7.0 * k, top + 9.0 * k
    badge = sd_poly(x, y, ((bx, by - 2.6 * k), (bx + 2.0 * k, by + 1.6 * k), (bx, by + 0.6 * k),
                           (bx - 2.0 * k, by + 1.6 * k))) - 0.35 * k
    fill(cv, light, reg, badge, np.array([1.0, 0.86, 0.5], F32), 0.8 * k, gloss=0.9, shininess=16.0)


def draw_neck(cv: Canvas, light: Light, look: Look, rig: Rig) -> None:
    hx, hy = rig.head
    k = rig.body
    cx = CX + rig.lean
    reg = cv.region(min(hx, cx) - 8 * k, hy + 4, max(hx, cx) + 8 * k, rig.top + 6 * k)
    if reg is not None:
        neck = sd_capsule(reg.x, reg.y, hx, hy + 0.5 * rig.head_r, cx, rig.top + 1.5 * k, 3.4 * k, 3.8 * k)
        fill(cv, light, reg, neck, look.skin * 0.9, 3.4 * k, wrap=0.4, warm=0.8)


def draw_arm(cv: Canvas, light: Light, look: Look, arm: Arm, k: float = BODY) -> None:
    """A sleeve (upper arm and forearm, a cuff in the trim colour) and a hand: a fist, an open hand with its
    fingers spread, or none (tucked away). ``k``: the size of the limbs (``Rig.body``)."""
    (sx, sy), (ex, ey), (hx, hy) = arm.shoulder, arm.elbow, arm.hand
    xs, ys = (sx, ex, hx), (sy, ey, hy)
    reg = cv.region(min(xs) - 9 * k, min(ys) - 10 * k, max(xs) + 9 * k, max(ys) + 8 * k)
    if reg is None:
        return
    x, y = reg.x, reg.y
    dx, dy = hx - ex, hy - ey
    length = max(1e-3, math.hypot(dx, dy))
    ux, uy = dx / length, dy / length
    wx, wy = hx - ux * 2.8 * k, hy - uy * 2.8 * k
    sleeve = reg.full(smin(sd_capsule(x, y, sx, sy, ex, ey, 5.0 * k, 4.6 * k),
                           sd_capsule(x, y, ex, ey, wx, wy, 4.6 * k, 4.1 * k), 1.2 * k))
    ox, oy = 0.8 * k, 1.4 * k
    shadow = smin(sd_capsule(x - ox, y - oy, sx, sy, ex, ey, 5.0 * k),
                  sd_capsule(x - ox, y - oy, ex, ey, wx, wy, 4.6 * k), 1.2 * k)
    cv.darken(reg, 0.3 * soft_glow(np.maximum(reg.full(shadow), 0), 3.0 * k) * (y < DESK + 3.0))  # not the lamp
    n = normals(reg, sleeve, 4.0 * k, cv.px)
    fill(cv, light, reg, sleeve, look.uniform, n=n, wrap=0.3, gloss=0.08)
    along = (x - wx) * ux + (y - wy) * uy
    fill(cv, light, reg, np.maximum(sleeve, np.abs(along + 1.0 * k) - 0.9 * k), look.trim, n=n, wrap=0.35)
    if arm.hand_kind == "hidden":
        return
    if arm.hand_kind == "open":
        a = ((x - hx) * ux + (y - hy) * uy) / k  # along the forearm, out from the wrist (in hand units)
        b = (-(x - hx) * uy + (y - hy) * ux) / k  # across it
        side = 1.0 if ux >= 0 else -1.0
        palm = sd_ellipse(b, a, 0.0, -0.4, 3.6, 3.6)
        fingers = union(*(sd_capsule(b, a, fb * 0.8, 0.5, fb, 3.4 + reach, 1.05) for fb, reach in
                          ((-2.5, -0.5), (-0.85, 0.5), (0.85, 0.6), (2.45, -0.3))))
        thumb = sd_capsule(b, a, 2.5 * side, -1.6, 4.6 * side, 0.6, 1.25, 1.1)
        hand = smin(smin(palm, fingers, 0.9), thumb, 0.7) * k
    else:
        hand = sd_circle(x, y, hx - ux * 0.6 * k, hy - uy * 0.6 * k, 3.6 * k)
    fill(cv, light, reg, hand, look.skin * 0.94, 2.6 * k, wrap=0.35, warm=0.8)


def draw_mug(cv: Canvas, light: Light, colors: dict[str, Array], at: P, k: float = BODY) -> None:
    mx, my = at
    reg = cv.region(mx - 10 * k, my - 9 * k, mx + 10 * k, my + 9 * k)
    if reg is None:
        return
    x, y = reg.x, reg.y
    body = sd_box(x, y, mx, my, 5.0 * k, 5.6 * k, 1.8 * k)
    handle = np.maximum(np.abs(sd_circle(x, y, mx + 5.2 * k, my, 2.6 * k)) - 0.85 * k, mx + 4.2 * k - x)
    fill(cv, light, reg, np.minimum(body, handle), colors["mug"], 2.5 * k, gloss=0.4, shininess=20.0)
    fill(cv, None, reg, sd_ellipse(x, y, mx, my - 4.7 * k, 3.9 * k, 0.95 * k), colors["tea"])
    fill(cv, light, reg, np.maximum(body + 0.4 * k, np.abs(y - my - 0.5 * k) - 0.7 * k), colors["mug_band"],
         2.0 * k, alpha=0.9)
