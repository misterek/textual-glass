"""The drawing model for the pixel layers: grid, canvas, the ``Item`` base and
painting. No Textual imports.

A scene is a list of *items* (``textual_glass.items``): small frozen dataclasses
in cell units, so two scenes can be compared item by item and only the pixels of
changed items are redrawn. Each item knows its pixel bounding box and how to
draw itself into any rectangle of a layer (a :class:`Canvas`), so a full render
and a partial "redraw this rectangle" use the same code.

Items are either *static* (panels, tracks, rules: drawn into a cached base) or
*dynamic* (pills, gauge values, chart data: composed over the base when a
rectangle is edited). Layers are float32 while drawing: RGB for the opaque
backdrop, premultiplied RGBA for the overlay layers of modal screens.

Coordinates: ``Grid`` maps cells to layer pixels (``sx`` by ``sy`` pixels per
cell). Rectangles in items are ``(x, y, w, h)`` in cells, possibly fractional.
Colours are ``RGB`` tuples or ``"#rrggbb"`` strings; :func:`col` takes either.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

import numpy as np

from .palette import RGB, rgb

F32 = np.float32
Array = np.ndarray
Window = tuple[Array, Array, Array]  # a view of the canvas, its x and y pixel centres
Rect = tuple[int, int, int, int]
FRect = tuple[float, float, float, float]
Box = tuple[float, float, float, float]  # x0, y0, x1, y1 in layer pixels
Colour = str | RGB


def rgb_of(c: Colour) -> RGB:
    return rgb(c) if isinstance(c, str) else c


def col(c: Colour) -> np.ndarray:
    """A colour as float32 RGB in 0..1."""
    return np.array(rgb_of(c), F32) / 255


def smoothstep(e0: float, e1: float, x: Array) -> Array:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def sd_rrect(xs: Array, ys: Array, cx: float, cy: float, hw: float, hh: float, r: float) -> Array:
    """Signed distance to a rounded rectangle (negative inside)."""
    qx = np.abs(xs - cx) - (hw - r)
    qy = np.abs(ys - cy) - (hh - r)
    ox = np.maximum(qx, 0)
    oy = np.maximum(qy, 0)
    return np.sqrt(ox * ox + oy * oy) + np.minimum(np.maximum(qx, qy), 0) - r


def box_fade(xs: Array, ys: Array, box: Box, margin: float) -> Array:
    """1 inside ``box``, easing to exactly 0 in its outermost pixel over ``margin`` pixels."""
    dx = np.minimum(xs - box[0], box[2] - xs)
    dy = np.minimum(ys - box[1], box[3] - ys)
    return smoothstep(1.0, 1.0 + max(1.0, margin), np.minimum(dx, dy)).astype(F32)


def x_gradient(xs: Array, left: float, right: float, c1: Colour, c2: Colour) -> Array:
    """A left-to-right blend from ``c1`` to ``c2`` (a border, a bar)."""
    u = np.clip((xs - left) / max(1.0, right - left), 0, 1)[..., None]
    return col(c1) * (1 - u) + col(c2) * u


@dataclass(frozen=True)
class Grid:
    """Layer pixels per cell, and the layer size."""

    sx: float
    sy: float
    width: int
    height: int

    def box(self, rect: FRect, pad_x: float = 0.0, pad_y: float = 0.0) -> Box:
        x, y, w, h = rect
        return ((x - pad_x) * self.sx, (y - pad_y) * self.sy, (x + w + pad_x) * self.sx, (y + h + pad_y) * self.sy)

    def clip(self, box: Box) -> Rect | None:
        x0, y0 = max(0, int(math.floor(box[0]))), max(0, int(math.floor(box[1])))
        x1, y1 = min(self.width, int(math.ceil(box[2]))), min(self.height, int(math.ceil(box[3])))
        if x1 <= x0 or y1 <= y0:
            return None
        return (x0, y0, x1 - x0, y1 - y0)


class Canvas:
    """A float32 window onto a layer, positioned at (x, y) in layer pixels."""

    def __init__(self, data: np.ndarray, x: int, y: int) -> None:
        self.data = data
        self.x, self.y = x, y
        self.h, self.w = data.shape[:2]
        self.rgba = data.shape[2] == 4
        #: The current item's edge fade (set by ``Item.window``): every bit of
        #: light it draws is multiplied by it, so nothing is cut off at its box.
        self.mask: Array | None = None

    def _masked(self, view: Array, alpha: Array | float) -> Array:
        a = np.asarray(alpha, F32)
        mask = self.mask
        if mask is not None and mask.shape == view.shape[:2]:
            a = (a[..., 0] if a.ndim == 3 else a) * mask
        if a.ndim == 2:
            a = a[..., None]
        return a

    def window(self, box: Box) -> Window | None:
        """The part of the canvas inside ``box``, with pixel-centre coordinates."""
        x0 = max(self.x, int(math.floor(box[0])))
        y0 = max(self.y, int(math.floor(box[1])))
        x1 = min(self.x + self.w, int(math.ceil(box[2])))
        y1 = min(self.y + self.h, int(math.ceil(box[3])))
        if x1 <= x0 or y1 <= y0:
            return None
        view = self.data[y0 - self.y:y1 - self.y, x0 - self.x:x1 - self.x]
        xs = np.arange(x0, x1, dtype=F32)[None, :] + F32(0.5)
        ys = np.arange(y0, y1, dtype=F32)[:, None] + F32(0.5)
        return view, xs, ys

    def over(self, view: Array, color: Array, alpha: Array | float) -> None:
        a = self._masked(view, alpha)
        if self.rgba:
            view[..., :3] *= 1 - a
            view[..., :3] += np.asarray(color, F32) * a
            view[..., 3:] *= 1 - a
            view[..., 3:] += a
        else:
            view += (np.asarray(color, F32) - view) * a

    def glow(self, view: Array, color: Array, amount: Array | float) -> None:
        """Additive light on the opaque layer; plain coverage on overlay layers."""
        if self.rgba:
            self.over(view, color, np.clip(amount, 0, 1))
        else:
            view += np.asarray(color, F32) * self._masked(view, amount)


# ================================================================== items ==


class Item:
    """Base class: scene items are frozen dataclasses (hashable, comparable).

    Subclasses set ``dynamic = True`` when they change often (pills, values):
    the engine then keeps them out of the cached base and composes them on top
    when it edits a rectangle. ``ident`` names an item across frames (for
    example ``f"ring:{id(widget)}"``) so a change can be diffed precisely
    (:meth:`diff_boxes`).
    """

    dynamic: ClassVar[bool] = False
    #: Rows over which the item's light fades to nothing at its bounding box.
    #: An item must never draw anything at the edge of its box: whatever
    #: reaches the edge is cut off there, and the cut shows as a faint
    #: rectangle (worst when only that box is re-sent as an edit). Each item's
    #: own falloff must already end inside the box; this is the safety net.
    edge: ClassVar[float] = 0.22
    depth: int = 0
    clip: FRect | None = None
    ident: str = ""

    def bbox(self, g: Grid) -> Box:
        raise NotImplementedError

    def margin(self, g: Grid) -> float:
        """Pixels at the edge of the box that the safety fade touches. Items keep
        their light out of them, so the fade never changes what they draw."""
        return 1.0 + (max(1.0, self.edge * g.sy) if self.edge > 0 else 0.0)

    def visible_bbox(self, g: Grid) -> Box:
        """The bounding box, cut down to the item's clip rectangle (in cells)."""
        box = self.bbox(g)
        if self.clip is None:
            return box
        cb = g.box(self.clip)
        return (max(box[0], cb[0]), max(box[1], cb[1]), min(box[2], cb[2]), min(box[3], cb[3]))

    def diff_boxes(self, old: Item, g: Grid) -> list[Box] | None:
        """Pixels that differ from ``old`` (an item with the same ``ident``), or
        None for "everything in both bounding boxes"."""
        return None

    def window(self, c: Canvas, g: Grid) -> Window | None:
        """The canvas pixels this item may touch; also arms the edge fade.

        The fade follows the item's own box. A ``clip`` rectangle cuts hard, the
        way the widget's text is cut: what lies beyond it is meant to be hidden,
        and a full render cuts in the same place as an edit.
        """
        win = c.window(self.visible_bbox(g))
        c.mask = None
        if win is not None and self.edge > 0:
            _, xs, ys = win
            c.mask = box_fade(xs, ys, self.bbox(g), self.edge * g.sy)
        return win

    def draw_shadow(self, c: Canvas, g: Grid) -> None:
        return None

    def draw(self, c: Canvas, g: Grid) -> None:
        raise NotImplementedError


# =========================================================== painting ==


def paint(canvas: Canvas, grid: Grid, items: Iterable[Item]) -> None:
    """Draw items in order, depth by depth: each depth's shadows go under its bodies."""
    by_depth: dict[int, list[Item]] = {}
    for item in items:
        by_depth.setdefault(item.depth, []).append(item)
    for depth in sorted(by_depth):
        group = by_depth[depth]
        for item in group:
            item.draw_shadow(canvas, grid)
            canvas.mask = None
        for item in group:
            item.draw(canvas, grid)
            canvas.mask = None


def to_u8(data: np.ndarray) -> np.ndarray:
    """Float layer pixels to uint8 (premultiplied RGBA becomes straight alpha)."""
    if data.shape[2] == 4:
        a = data[..., 3:]
        rgb_ = data[..., :3] / np.maximum(a, 1e-4)
        data = np.concatenate([rgb_, a], axis=2)
    return (np.clip(data, 0, 1) * 255 + 0.5).astype(np.uint8)
