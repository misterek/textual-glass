"""Pure-function tests: protocol encoding, PNG, palette, rendering, items, sprites, detection."""

from __future__ import annotations

import base64
import inspect
import io
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from textual_glass import backdrop, items, kitty, render, sprites
from textual_glass import detection as detect_mod
from textual_glass.engine import make_grid, merge_rects
from textual_glass.items import (
    BAR,
    HEAVY,
    LINE,
    NONE,
    Area,
    Badge,
    Bars,
    Dot,
    HeaderBar,
    HeaderStrip,
    Lane,
    Meter,
    Panel,
    Pill,
    Progress,
    Ring,
    Rows,
    Rule,
    Signal,
    Spine,
    Surface,
    Thumb,
)
from textual_glass.palette import NEBULA, SPACE, Palette, border_pair, hue_shift, rgb
from textual_glass.render import Canvas, Grid, Item, paint, to_u8
from textual_glass.sprites import (
    BEACON_STATES,
    beacon_frames,
    dim_frame,
    glint_frames,
    halo_frames,
    pulse_frames,
    shimmer_frames,
)

# -- protocol -------------------------------------------------------------------


def parse(stream: str) -> list[tuple[dict[str, str], str]]:
    out = []
    for control, payload in re.findall(r"\x1b_G([^;\x1b]*);?([^\x1b]*)\x1b\\", stream):
        keys = dict(kv.split("=", 1) for kv in control.split(",") if kv)
        out.append((keys, payload))
    return out


def test_every_command_is_quiet_and_chunks_are_whole() -> None:
    payload = os.urandom(20_000)
    cmds = parse(kitty.command(dict(a="t", i=7, f=32, s=1, v=1), payload))
    assert len(cmds) > 1
    assert all(keys["q"] == "2" for keys, _ in cmds)
    assert cmds[0][0]["a"] == "t" and cmds[0][0]["m"] == "1"
    assert all(set(keys) <= {"m", "q"} for keys, _ in cmds[1:])
    assert cmds[-1][0]["m"] == "0"
    assert all(len(data) % 4 == 0 for _, data in cmds[:-1])
    assert base64.b64decode("".join(data for _, data in cmds)) == payload


def test_frame_continuations_repeat_a_f() -> None:
    cmds = parse(kitty.command(dict(a="f", i=7, r=1), os.urandom(10_000)))
    assert all(keys.get("a") == "f" for keys, _ in cmds)


def test_placement_saves_and_restores_the_cursor() -> None:
    cmd = kitty.place(9, 3, 4, 10, 5, -2_000_000_000)
    assert cmd.startswith("\x1b7\x1b[5;4H") and cmd.endswith("\x1b8")
    keys, _ = parse(cmd)[0]
    assert keys["C"] == "1" and keys["z"] == "-2000000000"


def test_png_roundtrip() -> None:
    rng = np.random.default_rng(1)
    for channels in (3, 4):
        pixels = rng.integers(0, 256, (37, 53, channels), dtype=np.uint8)
        decoded = np.asarray(Image.open(io.BytesIO(kitty.png(pixels))).convert("RGBA" if channels == 4 else "RGB"))
        assert np.array_equal(decoded, pixels)


def test_placeholders_encode_rows_and_columns() -> None:
    row = kitty.placeholder_row(2, 3)
    assert row.count(kitty.PLACEHOLDER) == 3
    assert row[1] == chr(kitty.DIACRITICS[2]) and row[2] == chr(kitty.DIACRITICS[0])
    assert kitty.id_color(0x123456) == "rgb(18,52,86)"


def test_animation_uploads_frames_before_playing() -> None:
    frames = [bytes(4 * 4)] * 3
    cmds = parse(kitty.animation(5, 2, 2, frames, 50))
    actions = [keys["a"] for keys, _ in cmds]
    assert actions == ["t", "f", "f", "a", "a"]
    assert cmds[-1][0].get("s") == "3"


# -- layout helpers -----------------------------------------------------------------


def test_merge_rects() -> None:
    assert merge_rects([(0, 0, 10, 10), (5, 5, 10, 10), (100, 100, 5, 5)]) == [(0, 0, 15, 15), (100, 100, 5, 5)]
    assert len(merge_rects([(i * 50, 0, 10, 10) for i in range(30)])) == 1


def test_grid_caps_the_backdrop_size() -> None:
    g = make_grid(160, 45, 16, 34)  # a Retina-sized window
    assert g.width * g.height <= 2_400_000 * 1.01
    assert abs(g.width / g.height - (160 * 16) / (45 * 34)) < 0.01


def test_grid_and_rect_helpers() -> None:
    g = make_grid(140, 45, 16, 34)
    assert g.width * g.height <= 2_400_000 + 140 * 45
    assert merge_rects([(0, 0, 10, 10), (12, 0, 10, 10), (100, 100, 5, 5)]) == [(0, 0, 22, 10), (100, 100, 5, 5)]


# -- palette and rendering ------------------------------------------------------------


def palette() -> Palette:
    return Palette(dark=True, background=(18, 18, 18), surface=(30, 30, 30), panel=(36, 47, 56),
                   foreground=(224, 224, 224), primary=(1, 120, 212), secondary=(0, 69, 120),
                   accent=(254, 166, 43), success=(78, 191, 113), warning=(254, 166, 43), error=(186, 60, 91),
                   bg_top=(30, 40, 50), bg_bottom=(10, 12, 15))


def test_structural_colours() -> None:
    pal = palette()
    assert pal.is_structural((30, 30, 30)) and pal.is_structural((36, 47, 56))
    assert not pal.is_structural((185, 60, 91))  # a badge
    assert not pal.is_structural((1, 120, 212))  # the cursor


def test_border_gradient_shifts_hue() -> None:
    c1, c2 = border_pair((1, 120, 212))
    assert c1 != c2
    assert hue_shift((255, 0, 0), 120)[1] > 200


def test_paint_is_finite_and_items_stay_in_their_boxes() -> None:
    pal = palette()
    g = Grid(9, 19, 900, 380)
    data = backdrop.gradient(900, 380, pal)
    before = data.copy()
    scene: list[Item] = [
        Panel((2, 2, 40, 12), (LINE,) * 4, *border_pair(pal.error), pal.surface, titles=((4, 20),), focused=True),
        Pill((3, 5, 38, 1), pal.primary),
        Progress((50, 3, 30, 1), 0.6, pal.warning, pal.accent),
        Thumb((98, 0, 1, 20), 3.0, 5.0, True, (90, 120, 160)),
    ]
    paint(Canvas(data, 0, 0), g, scene)
    assert np.isfinite(data).all()
    changed = np.abs(data - before).max(axis=2) > 1e-6
    ys, xs = np.nonzero(changed)
    union = [min(i.bbox(g)[0] for i in scene), min(i.bbox(g)[1] for i in scene),
             max(i.bbox(g)[2] for i in scene), max(i.bbox(g)[3] for i in scene)]
    assert xs.min() >= union[0] - 1 and xs.max() <= union[2] + 1
    assert ys.min() >= union[1] - 1 and ys.max() <= union[3] + 1


def test_title_notch_cuts_the_border() -> None:
    g = Grid(10, 20, 600, 200)

    def top_line(titles: tuple[tuple[float, float], ...]) -> np.ndarray:
        data = np.zeros((200, 600, 3), np.float32)
        panel = Panel((0, 0, 60, 10), (LINE,) * 4, (255, 0, 0), (255, 0, 0), None, titles=titles)
        paint(Canvas(data, 0, 0), g, [panel])
        return data[10, :, 0]  # the row through the top border line

    plain, notched = top_line(()), top_line(((5, 25),))
    assert plain[100:200].mean() > 0.3
    assert notched[100:200].mean() < 0.05
    assert notched[400:500].mean() > 0.3


def test_overlay_canvas_is_straight_alpha() -> None:
    data = np.zeros((40, 40, 4), np.float32)
    canvas = Canvas(data, 0, 0)
    win = canvas.window((0, 0, 40, 40))
    assert win is not None
    canvas.over(win[0], np.array([1.0, 0.5, 0.0], np.float32), np.full((40, 40), 0.5, np.float32))
    out = to_u8(data)
    assert tuple(out[0, 0]) == (255, 128, 0, 128)


def test_colours_are_hex_strings_or_rgb() -> None:
    g = Grid(9, 19, 400, 200)

    def draw(item: Item) -> np.ndarray:
        data = np.zeros((200, 400, 3), np.float32)
        paint(Canvas(data, 0, 0), g, [item])
        return data

    assert rgb("#38e1ff") == (56, 225, 255)
    assert np.array_equal(draw(Pill((2, 2, 20, 1), "#ff4d6d")), draw(Pill((2, 2, 20, 1), (255, 77, 109))))
    hexes = Panel((1, 3, 30, 6), (LINE,) * 4, "#38e1ff", "#a46bff", "#16203d", focused=True)
    tuples = Panel((1, 3, 30, 6), (LINE,) * 4, (56, 225, 255), (164, 107, 255), (22, 32, 61), focused=True)
    assert np.array_equal(draw(hexes), draw(tuples))


def test_render_layer_has_no_textual_imports() -> None:
    for module in (render, items, sprites, backdrop):
        source = inspect.getsource(module)
        assert not re.search(r"^\s*(from|import) textual\b", source, re.M), module.__name__


# -- palette from the theme ----------------------------------------------------------


def fake_app(variables: dict[str, str], dark: bool = True) -> object:
    return SimpleNamespace(get_css_variables=lambda: variables, current_theme=SimpleNamespace(dark=dark))


def test_palette_reads_the_glass_theme_variables() -> None:
    pal = Palette.from_app(fake_app({  # type: ignore[arg-type]
        "background": "#070b18", "glass-backdrop": "nebula", "glass-scrim-alpha": "0.62",
        "glass-space-top": "#112233", "glass-space-bottom": "#000102",
        "glass-nebula-1": "#5b2a9e", "glass-nebula-2": "#0e6a78", "glass-nebula-3": "#1b3d8f",
        "glass-nebula-strength": "0",
    }))
    assert pal.background == (7, 11, 24) and pal.backdrop == "nebula" and pal.scrim_alpha == 0.62
    assert pal.space == ((0x11, 0x22, 0x33), (0, 1, 2)) and pal.nebula == NEBULA and pal.nebula_strength == 0.0
    plain = Palette.from_app(fake_app({}))  # type: ignore[arg-type]
    assert (plain.backdrop, plain.scrim_alpha, plain.space, plain.nebula, plain.nebula_strength) == (
        "gradient", 0.5, SPACE, NEBULA, 1.0)
    assert Palette.from_app(fake_app({}, dark=False)).scrim_alpha == 0.3  # type: ignore[arg-type]
    odd = Palette.from_app(fake_app({"glass-backdrop": "plasma", "glass-scrim-alpha": "lots"}))  # type: ignore[arg-type]
    assert odd.backdrop == "gradient" and odd.scrim_alpha == 0.5


def test_backdrop_follows_the_palette() -> None:
    pal = palette()
    assert np.array_equal(backdrop.draw(160, 90, pal), backdrop.gradient(160, 90, pal))
    space = replace(pal, backdrop="nebula")
    assert np.array_equal(backdrop.draw(160, 90, space), backdrop.nebula(160, 90, space))
    calm = backdrop.nebula(160, 90, replace(space, nebula_strength=0.0))
    assert not np.array_equal(calm, backdrop.nebula(160, 90, space))
    top = backdrop.nebula(160, 90, replace(space, nebula_strength=0.0, space=((40, 0, 0), (0, 0, 0))), stars=0)[0, 80]
    assert top[0] > 0.1 > top[1]  # the space gradient starts at glass-space-top


def test_background_is_dim_and_deterministic() -> None:
    a, b = backdrop.nebula(160, 90, palette()), backdrop.nebula(160, 90, palette())
    assert np.array_equal(a, b) and float(a.mean()) < 0.2


# -- items ------------------------------------------------------------------------------

GRID = Grid(8.0, 16.0, 400, 320)
CYAN, VIOLET = "#38e1ff", "#a46bff"


def draw_alone(items_: list[Item], rgba: bool = False) -> np.ndarray:
    data = np.zeros((320, 400, 4 if rgba else 3), np.float32)
    paint(Canvas(data, 0, 0), GRID, items_)
    return to_u8(data)


def test_items_draw_inside_their_bounding_boxes() -> None:
    scene: list[Item] = [
        Panel((2, 2, 30, 10), (LINE,) * 4, CYAN, VIOLET, "#16203d", titles=((4, 12),), focused=True),
        Pill((3, 4, 20, 1), "#ff4d6d"), Ring((34, 2, 12, 6), 0.6, CYAN, "#7c8cff"), Signal((3, 6, 5, 1), 7, "#ff8a3d"),
        Meter((3, 8, 10, 1), 0.4, CYAN, VIOLET),
        Bars((10, 12, 20, 6), (1, 3, 0, 2), CYAN, VIOLET, (2, 2, 2, 2), today=1),
        Dot(30, 16.5, "#6dffa8", trail=2.0, ring=True)]
    for item in scene:
        img = draw_alone([item])
        ys, xs = np.nonzero(img.any(axis=2))
        assert len(xs), item
        x0, y0, x1, y1 = item.bbox(GRID)
        assert min(xs) >= int(x0) - 1 and max(xs) <= x1 + 1 and min(ys) >= int(y0) - 1 and max(ys) <= y1 + 1, item


def test_rgba_layers_stay_transparent_where_nothing_is_drawn() -> None:
    img = draw_alone([Panel((10, 5, 20, 8), (LINE,) * 4, CYAN, VIOLET, "#16203d", dialog=True)], rgba=True)
    assert img[0, 0, 3] == 0 and img[160, 200, 3] > 200


def test_panel_focus_change_is_four_border_strips() -> None:
    a = Panel((2, 2, 30, 10), (LINE,) * 4, CYAN, VIOLET, "#16203d", titles=((4, 12),), ident="p")
    b = replace(a, focused=True)
    boxes = b.diff_boxes(a, GRID)
    assert boxes is not None and len(boxes) == 4
    area = sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in boxes)
    full = (lambda bb: (bb[2] - bb[0]) * (bb[3] - bb[1]))(a.bbox(GRID))
    assert area < 0.7 * full
    title_only = replace(a, titles=((4, 14),)).diff_boxes(a, GRID)
    assert title_only and all(y1 - y0 <= 1.5 * GRID.sy for _, y0, _, y1 in title_only)
    assert replace(a, fill="#000000").diff_boxes(a, GRID) is None


PANEL = Panel((3, 2, 36, 12), (LINE,) * 4, CYAN, VIOLET, "#16203d", titles=((6, 16),), subtitles=((20, 30),))
PANEL_CHANGES: list[tuple[Panel, Panel]] = [
    (PANEL, replace(PANEL, focused=True)),
    (replace(PANEL, focused=True), PANEL),
    (PANEL, replace(PANEL, c1="#ff4d6d", c2="#ffc24b")),
    (replace(PANEL, focused=True), replace(PANEL, focused=True, strength=0.4)),
    (PANEL, replace(PANEL, titles=((6, 22),))),
    (PANEL, replace(PANEL, titles=(), subtitles=((10, 14),))),
    (replace(PANEL, dialog=True, edges=(HEAVY,) * 4), replace(PANEL, dialog=True, edges=(HEAVY,) * 4, focused=True)),
    (replace(PANEL, rect=(3, 2, 36, 3), focused=True), replace(PANEL, rect=(3, 2, 36, 3))),
    (replace(PANEL, edges=(NONE, NONE, NONE, BAR), dialog=True),
     replace(PANEL, edges=(NONE, NONE, NONE, BAR), dialog=True, c1="#6dffa8")),
]


@pytest.mark.parametrize("rgba", [False, True], ids=["backdrop", "overlay"])
@pytest.mark.parametrize("old, new", PANEL_CHANGES)
def test_panel_diff_boxes_cover_every_changed_pixel(old: Panel, new: Panel, rgba: bool) -> None:
    """Redrawing only ``diff_boxes`` over the old render gives exactly a fresh render
    of the new panel: the engine edits those boxes in place on the terminal."""
    g = Grid(9.0, 19.0, 440, 340)
    base = np.zeros((340, 440, 4), np.float32) if rgba else backdrop.gradient(440, 340, palette())
    boxes = new.diff_boxes(old, g)
    assert boxes is not None
    edited = base.copy()
    paint(Canvas(edited, 0, 0), g, [old])
    for box in boxes:
        rect = g.clip(box)
        if rect is None:
            continue
        x, y, w, h = rect
        patch = base[y:y + h, x:x + w].copy()
        paint(Canvas(patch, x, y), g, [new])
        edited[y:y + h, x:x + w] = patch
    fresh = base.copy()
    paint(Canvas(fresh, 0, 0), g, [new])
    assert np.array_equal(to_u8(edited), to_u8(fresh))


# -- no light at the box edges (rule 16) --------------------------------------------------

#: At least one of every item class, in the variants that draw the most light (focus, glows,
#: trails, dialogs with shadows) and some that sit right at the ends of their range.
EDGE_SAMPLES: list[Item] = [
    Panel((4, 3, 30, 10), (LINE,) * 4, CYAN, VIOLET, "#16203d", titles=((6, 14),)),
    Panel((4, 3, 30, 10), (LINE,) * 4, CYAN, VIOLET, "#16203d", focused=True),
    Panel((4, 3, 30, 10), (HEAVY,) * 4, CYAN, VIOLET, "#16203d", 0.9, dialog=True, focused=True),
    Panel((4, 3, 30, 10), (BAR,) * 4, (254, 166, 43), (255, 90, 50), None, dark=False, focused=True),
    Panel((4, 3, 30, 4), (NONE, NONE, NONE, BAR), (254, 166, 43), (255, 90, 50), (30, 30, 30), 0.9, dialog=True),
    Panel((4, 3, 30, 3), (NONE, NONE, LINE, NONE), CYAN, VIOLET, None, focused=True),
    Surface((6, 4, 30, 8), (30, 30, 30), 0.6),
    Surface((6, 4, 30, 8), (30, 30, 30), 0.9, dialog=True, rim=(1, 120, 212)),
    Surface((6, 4, 30, 8), (240, 240, 240), 0.9, dialog=True, rim=(1, 120, 212), dark=False),
    HeaderStrip((5, 6, 40, 1), (36, 47, 56), CYAN, VIOLET),
    HeaderStrip((5, 6, 40, 2), (220, 220, 220), CYAN, VIOLET, dark=False),
    Rows(5, 40, ((8, 1), (10, 1), (12, 2)), (224, 224, 224), 0.6),
    Pill((5, 6, 20, 1), "#ff4d6d"), Pill((5, 6, 20, 2), CYAN, focused=False),
    Pill((5, 6.4, 20, 1), CYAN, solid=True),
    Badge((5, 6, 6, 1), (254, 166, 43)), Badge((5, 6, 1, 1), (254, 166, 43)),
    Progress((6, 12, 20, 1), 1.0, (254, 166, 43), (255, 120, 60)),
    Progress((6, 12, 20, 1), 0.02, (254, 166, 43), (255, 120, 60), dark=False),
    Thumb((50, 2, 1, 20), 0.0, 5.0, True, (90, 120, 160), active=True),
    Thumb((50, 2, 2, 20), 15.0, 5.0, True, (90, 120, 160)),
    Thumb((6, 20, 40, 1), 0.0, 9.0, False, (90, 120, 160), active=True),
    Thumb((6, 20, 40, 1), 31.0, 9.0, False, (90, 120, 160)),
    HeaderBar((2, 3, 50, 1), CYAN, VIOLET),
    Rule((4, 27, 30, 1), CYAN),
    Ring((36, 3, 12, 7), 0.97, CYAN, "#7c8cff"), Ring((36, 3, 12, 7), 0.5, CYAN, "#7c8cff", sweep=1.0, ticks=0),
    Signal((5, 9, 5, 1), 10, "#ff4d6d"), Signal((5, 10, 5, 1), 5, "#ffc24b"),
    Meter((6, 12, 12, 1), 1.0, CYAN, VIOLET), Meter((6, 13, 12, 1), 0.3, CYAN, VIOLET),
    Area((2, 22, 40, 6), (0.2, 1.0, 0.4, 0.95), CYAN, VIOLET),
    Bars((8, 15, 20, 6), (9, 3, 0, 2, 9), CYAN, VIOLET, (9, 9, 9, 9, 9), today=1),
    Lane((4, 26, 30, 1), CYAN),
    Dot(40, 15.5, "#ffc24b", trail=2.5), Dot(40, 17.5, "#ff6ec7", trail=1.0, ring=True), Dot(44, 17.5, "#6dffa8"),
    Dot(44, 19.5, "#6dffa8", hollow=True),
    Spine(30, 20, 25, CYAN, VIOLET),
]


def item_classes() -> set[type]:
    return {c for c in vars(items).values()
            if isinstance(c, type) and issubclass(c, Item) and c is not Item and c.__module__ == items.__name__}


def test_every_item_class_is_checked_at_its_edges() -> None:
    classes = item_classes()
    assert len(classes) >= 18
    assert {type(i) for i in EDGE_SAMPLES} == classes, "add a sample of every new item to EDGE_SAMPLES"


def _box_border(img: np.ndarray, box: tuple[float, float, float, float]) -> np.ndarray:
    """The outermost ring of pixels inside ``box``."""
    x0, y0 = int(np.floor(box[0])), int(np.floor(box[1]))
    x1, y1 = int(np.ceil(box[2])) - 1, int(np.ceil(box[3])) - 1
    region = img[y0:y1 + 1, x0:x1 + 1].astype(int)
    return np.concatenate([region[0], region[-1], region[:, 0], region[:, -1]])


@pytest.mark.parametrize("grid", [Grid(8.0, 16.0, 640, 560), Grid(7.3, 15.7, 640, 560)], ids=["8x16", "7.3x15.7"])
@pytest.mark.parametrize("fade", [True, False], ids=["with-box-fade", "own-falloff"])
@pytest.mark.parametrize("item", EDGE_SAMPLES, ids=lambda i: type(i).__name__)
def test_items_draw_nothing_at_the_edge_of_their_box(item: Item, fade: bool, grid: Grid,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Light that reaches an item's bounding box is cut off there and shows as a
    faint rectangle in Ghostty (it did, around flight-track dots and the pill).
    Each item's glow must end inside its box by itself (``own-falloff``): the
    box fade in ``Item.window`` is only a safety net, and a square one.

    Drawn on mid-grey, so shadows count as well as light, and on a transparent
    overlay layer, where any coverage shows."""
    if not fade:
        monkeypatch.setattr(Canvas, "_masked", lambda self, view, alpha: (
            np.asarray(alpha, np.float32)[..., None] if np.asarray(alpha).ndim == 2 else np.asarray(alpha, np.float32)))
    w, h = grid.width, grid.height
    grey = np.full((h, w, 3), 0.25, np.float32)
    data = grey.copy()
    paint(Canvas(data, 0, 0), grid, [item])
    overlay = np.zeros((h, w, 4), np.float32)
    paint(Canvas(overlay, 0, 0), grid, [item])
    box = item.bbox(grid)
    assert box[0] >= 0 and box[1] >= 0 and box[2] <= w and box[3] <= h, "keep the sample inside the canvas"
    change = np.abs(to_u8(data).astype(int) - to_u8(grey).astype(int))
    alpha = to_u8(overlay)[..., 3:]
    if isinstance(item, HeaderBar):
        # a header runs along the window's top, left and right edges, where nothing is
        # cut: only its bottom side matters
        rows = slice(int(np.floor(box[0])), int(np.ceil(box[2])))
        bottom = int(np.ceil(box[3])) - 1
        border, coverage = change[bottom, rows], alpha[bottom, rows]
    else:
        border, coverage = _box_border(change, box), _box_border(alpha, box)
    assert border.max() <= 1, (item, int(border.max()))
    assert coverage.max() <= 1, (item, int(coverage.max()))


# -- sprites (rule 17) ---------------------------------------------------------------------

SPRITE_SIZES = [(32, 34), (16, 17), (18, 38), (64, 68), (9, 19)]
NO_WINDOW_EDGE = (False, False, False, False)
#: Frames, their size, and which sides (top, right, bottom, left) lie on the window's edge.
Frames = tuple[list[bytes], int, int, tuple[bool, bool, bool, bool]]


def _sprite_cases() -> list[tuple[str, str, Callable[[], Frames]]]:
    cases: list[tuple[str, str, Callable[[], Frames]]] = []
    for w, h in SPRITE_SIZES:
        for state in BEACON_STATES:
            def beacon(w: int = w, h: int = h, s: str = state) -> Frames:
                return beacon_frames(w, h, s, "#ffc24b")[0], w, h, NO_WINDOW_EDGE
            cases.append(("beacon_frames", f"{state}-{w}x{h}", beacon))
        for colour in ("#ff4d6d", (255, 194, 75)):
            def pulse(w: int = w, h: int = h, c: str | tuple[int, int, int] = colour) -> Frames:
                return pulse_frames(w, h, c)[0], w, h, NO_WINDOW_EDGE
            cases.append(("pulse_frames", f"{w}x{h}", pulse))
        for phase in (0.0, 0.5):  # the flash peaks at frame 10 or frame 0
            def glint(w: int = w, h: int = h, p: float = phase) -> Frames:
                width, height, frames = glint_frames(w, h, 20, p, (224, 224, 224))
                return frames, width, height, NO_WINDOW_EDGE
            cases.append(("glint_frames", f"cell {w}x{h} phase {phase}", glint))

        def shimmer(w: int = w, h: int = h) -> Frames:
            width, height, frames = shimmer_frames(w + 40, h, 28, (255, 255, 255))
            return frames, width, height, (True, True, True, True)  # stretched over the whole window
        cases.append(("shimmer_frames", f"window {w + 40}x{h}", shimmer))
    cols, rows = 120, 40
    for sx, sy in ((9.0, 19.0), (8.0, 16.0), (7.3, 15.7), (16.0, 34.0)):
        for rect in ((10, 5, 40, 12), (0, 0, 30, 10), (95, 30, 25, 10), (50, 20, 3, 3)):
            def halo(sx: float = sx, sy: float = sy, rect: tuple[int, int, int, int] = rect) -> Frames:
                (x, y, w, h), width, height, frames = halo_frames(Grid(sx, sy, 1, 1), rect, cols, rows, (1, 120, 212),
                                                                  CYAN, frames=16, res=0.35)
                return frames, width, height, (y == 0, x + w == cols, y + h == rows, x == 0)
            cases.append(("halo_frames", f"{sx}x{sy} {rect}", halo))
    return cases


SPRITE_CASES = _sprite_cases()


def test_every_sprite_maker_is_checked_at_several_sizes() -> None:
    makers = {name for name in dir(sprites) if name.endswith("_frames")} - {"solid_frames"}  # a 1x1 scrim
    checked = {name: sum(1 for n, _, _ in SPRITE_CASES if n == name) for name in makers}
    assert len(makers) >= 5
    assert all(count >= 3 for count in checked.values()), f"check every maker at several sizes: {checked}"


@pytest.mark.parametrize("make", [c[2] for c in SPRITE_CASES], ids=[f"{c[0]}-{c[1]}" for c in SPRITE_CASES])
def test_sprite_frames_are_transparent_at_their_border(make: Callable[[], Frames]) -> None:
    """A sprite fills whole cells; anything opaque at its border shows as a
    tinted square (the first stuck-PR pulses did, mid-pulse). A side on the
    window's edge shows no cut (the backdrop ends there too) and is skipped."""
    frames, w, h, window_edges = make()
    for k, frame in enumerate(frames):
        alpha = np.frombuffer(frame, np.uint8).reshape(h, w, 4)[..., 3]
        sides = [alpha[0], alpha[:, -1], alpha[-1], alpha[:, 0]]  # top, right, bottom, left
        border = [side for side, on_edge in zip(sides, window_edges, strict=True) if not on_edge]
        if border:
            assert np.concatenate(border).max() == 0, (k, int(np.concatenate(border).max()))


def test_sprites() -> None:
    for state in BEACON_STATES:
        frames, gap = beacon_frames(32, 34, state, "#ffc24b")
        assert all(len(f) == 32 * 34 * 4 for f in frames) and gap > 0
        assert (len(frames) > 1) == (state in ("working", "waiting", "error"))
    frames, _ = pulse_frames(16, 17, "#ff4d6d")
    assert len(frames) > 1 and len(set(frames)) > 1
    assert sprites.solid_frames((10, 20, 30), [0.0, 1.0]) == [bytes([10, 20, 30, 0]), bytes([10, 20, 30, 255])]
    assert sprites.solid("#0a141e", 0.5) == bytes([10, 20, 30, 128])


def test_dim_frames_and_gapless_upload() -> None:
    frame = bytes([200, 100, 50, 255, 10, 20, 30, 0])
    assert dim_frame(frame, 0.5) == bytes([100, 50, 25, 255, 5, 10, 15, 0])
    cmd = kitty.animation(7, 1, 2, [bytes(8), bytes(8)], 90, gapless=[bytes(8)])
    keys = re.findall(r"\x1b_G([^;\x1b]*)", cmd)
    frames = [k for k in keys if k.startswith("a=f")]
    assert frames[-1].endswith("z=-1,q=2"), "the dimmed copy is gapless: playback skips it"
    assert cmd.index("z=-1") < cmd.index("s=3"), "uploaded before playback starts"


# -- engine plumbing, filter and hooks (rules 3, 6, 7, 14) --------------------------------


async def test_background_job_runs_done_on_the_app_thread() -> None:
    """Rule 3: ``work`` runs in the worker thread; ``done`` (which may write
    graphics) runs on the app's thread, in the app's context."""
    import asyncio
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    from textual_glass.engine import GlassEngine, Options

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker")
    marker.set("app")
    engine = GlassEngine(SimpleNamespace(), Options())  # type: ignore[arg-type]
    engine._loop = asyncio.get_running_loop()
    engine._app_context = contextvars.copy_context()
    engine._executor = ThreadPoolExecutor(max_workers=1)
    engine.active = True
    seen: dict[str, object] = {}
    finished = asyncio.Event()

    def work() -> int:
        seen["work"] = threading.get_ident()
        return 42

    def done(result: int) -> None:
        seen.update(done=threading.get_ident(), result=result, context=marker.get(None))
        finished.set()

    async def from_another_context() -> None:  # like a Textual worker: active_app etc. unset
        marker.set("elsewhere")
        engine.background_job(work, done)

    await asyncio.get_running_loop().create_task(from_another_context(), context=contextvars.Context())
    await asyncio.wait_for(finished.wait(), 5)
    assert seen["work"] != threading.get_ident() == seen["done"]
    assert seen["result"] == 42 and seen["context"] == "app"
    assert engine.pending_jobs == 0
    engine._executor.shutdown()


def test_dim_rules_leave_placeholder_cells_alone() -> None:
    """Rule 6: under a modal the filter dims text, but a placeholder cell's
    foreground colour is an image id and must come through untouched."""
    from rich.segment import Segment
    from rich.style import Style

    from textual_glass.filter import Rules

    image = Segment(kitty.placeholder_row(0, 3), Style(color=kitty.id_color(0x123456)))
    text = Segment("ok", Style(color="#ffffff", bgcolor="#204060"))
    owned = Segment("  ", Style(bgcolor="#101010"))
    rules = Rules(owned=frozenset({(16, 16, 16)}), dim=((0, 0, 0), 0.5))
    out = rules.apply([image, text, owned])
    assert out[0] == image
    assert out[1].style is not None and out[1].style.color is not None and out[1].style.bgcolor is not None
    assert out[1].style.color.triplet == (127, 127, 127) and out[1].style.bgcolor.triplet == (16, 32, 48)
    assert out[2].style is not None and out[2].style.bgcolor is not None and out[2].style.bgcolor.is_default

    # A placeholder on a structural background (a beacon on the screen under a popup): the background
    # goes to the default so the glass shows, the foreground (the image id) stays exactly as it was.
    on_glass = Segment(kitty.placeholder_row(0, 3), Style(color=kitty.id_color(0x123456), bgcolor="#101010"))
    (kept,) = rules.apply([on_glass])
    assert kept.text == on_glass.text and kept.style is not None and on_glass.style is not None
    assert kept.style.color == on_glass.style.color
    assert kept.style.bgcolor is not None and kept.style.bgcolor.is_default


class _Without:
    """``target`` as a future Textual might give it: without some private attributes."""

    def __init__(self, target: object, *names: str) -> None:
        self._target, self._names = target, names

    def __getattr__(self, name: str) -> object:
        if name in self._names:
            raise AttributeError(name)
        return getattr(self._target, name)


async def test_every_hook_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 7: each hook into Textual's private API, with that private attribute
    gone, returns its fallback instead of raising, and a glass app whose hooks
    all lose their private attributes stays up."""
    import textual
    from textual.app import App
    from textual.widget import Widget

    import textual_glass._textual_hooks as hooks
    from textual_glass.testing.pilot_helpers import settle, start

    from sample_app import GlassSample

    class Bare:  # weak-referenceable, unlike SimpleNamespace
        pass

    bare = Bare()
    assert hooks.has_driver(bare) is False  # type: ignore[arg-type]
    assert hooks.driver_write(bare, "x") is False  # type: ignore[arg-type]
    hooks.wrap_end_update(bare, lambda: None)()  # type: ignore[arg-type]
    assert hooks.option_lines(bare) == []  # type: ignore[arg-type]
    assert hooks.row_label_width(bare) == 0  # type: ignore[arg-type]
    assert hooks.border_label_length(SimpleNamespace(border_title="[b]Hi[/b]", border_subtitle=None)) == 2  # type: ignore[arg-type]
    screen = SimpleNamespace(_compositor=SimpleNamespace(), walk_children=lambda kind: [])
    assert hooks.visible_widgets(screen) == []  # type: ignore[arg-type]
    reshaped = SimpleNamespace(_compositor=SimpleNamespace(visible_widgets={"widget": "no (region, clip) pair"}),
                               walk_children=lambda kind: [])
    assert hooks.visible_widgets(reshaped) == [], "a mapping of another shape falls back too"  # type: ignore[arg-type]
    with monkeypatch.context() as m:
        m.setattr(textual, "_border", SimpleNamespace())
        glyphs = hooks.BorderGlyphs()
        assert glyphs.blank(["round"]) is False
        glyphs.restore()
        assert not glyphs.active
    from textual import _border

    for odd in ((("+", "-", "+", "!"),) * 3, (("+", "-", "+"),) * 4, ((1, 2, 3),) * 3, "+-+|||+-+"):
        with monkeypatch.context() as m:  # Textual reads this table outside any try: never write a wrong shape
            table = {**_border.BORDER_CHARS, "round": odd}
            m.setattr(_border, "BORDER_CHARS", table)
            glyphs = hooks.BorderGlyphs()
            assert glyphs.blank(["round", "heavy"]) is False, odd
            assert not glyphs.active and table["round"] == odd and table["heavy"] == _border.BORDER_CHARS["heavy"]
    with monkeypatch.context() as m:
        m.delattr(Widget, "get_line_filters")
        hooks.add_filter_provider(bare, lambda widget: None)  # type: ignore[arg-type]
        hooks.remove_filter_provider(bare)  # type: ignore[arg-type]

    real = {name: getattr(hooks, name) for name in (
        "visible_widgets", "border_label_length", "option_lines", "row_label_width", "wrap_end_update")}
    monkeypatch.setattr(textual, "_border", SimpleNamespace())
    monkeypatch.setattr(hooks, "visible_widgets", lambda s: real["visible_widgets"](_Without(s, "_compositor")))
    monkeypatch.setattr(hooks, "border_label_length", lambda w, title=True: real["border_label_length"](
        _Without(w, "_border_title", "_border_subtitle"), title))
    monkeypatch.setattr(hooks, "option_lines", lambda o: real["option_lines"](_Without(o, "_lines")))
    monkeypatch.setattr(hooks, "row_label_width", lambda t: real["row_label_width"](
        _Without(t, "_row_label_column_width")))
    monkeypatch.setattr(hooks, "wrap_end_update", lambda a, f: real["wrap_end_update"](_Without(a, "_end_update"), f))
    app = GlassSample()
    async with app.run_test(size=(100, 32)) as pilot:
        await start(app, pilot)
        for key in ("down", "d", "escape", "2", "1"):
            await pilot.press(key)
            await settle(app, pilot)
        assert app.glass.active and app.glass.errors == 0, app.glass.last_error
        assert app.glass.layers, "the scene is still drawn (from the public widget tree)"
    assert not hasattr(App, "_glass_never_there")

    # A border table Textual can still read but glass can't patch safely: skipped, with a log line.
    import types

    monkeypatch.undo()
    from textual import _border

    monkeypatch.setattr(_border, "BORDER_CHARS", types.MappingProxyType(dict(_border.BORDER_CHARS)))
    app = GlassSample()
    notes: list[str] = []
    app.glass._log = lambda text, error=False: notes.append(text)  # type: ignore[method-assign]
    async with app.run_test(size=(100, 32)) as pilot:
        await start(app, pilot)
        assert app.glass.active and app.glass.errors == 0, app.glass.last_error
        assert _border.BORDER_CHARS["round"][0][0] == "╭", "the borders keep their glyphs"
        assert any("border" in note for note in notes), notes


def test_terminal_write_sends_through_textual_s_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """``terminal_write`` is the public way to send other escapes (notifications,
    the title): Textual's driver writer, glass on or off, failing soft."""
    import textual_glass
    import textual_glass._textual_hooks as hooks

    class Driver:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def write(self, data: str) -> None:
            self.sent.append(data)

    class Bare:
        _driver: Driver | None = None

    app = Bare()
    assert textual_glass.terminal_write(app, "\x07") is False, "no driver yet"  # type: ignore[arg-type]
    driver = app._driver = Driver()
    assert textual_glass.terminal_write(app, "\x1b]2;title\x07")  # type: ignore[arg-type]
    assert textual_glass.terminal_write(app, "") is False  # type: ignore[arg-type]
    assert driver.sent == ["\x1b]2;title\x07"]
    seen: list[str] = []
    monkeypatch.setattr(hooks, "driver_write", lambda a, data: seen.append(data) is None)
    assert textual_glass.terminal_write(app, "\x07") and seen == ["\x07"], "the hook is looked up per call"  # type: ignore[arg-type]


def test_mixins_add_no_attribute_textual_has() -> None:
    """Rule 14: ``Glass`` and ``PixelMixin`` add attributes Textual doesn't
    have; a clash (``App._context`` is a method, ``Widget.colors`` a property)
    breaks Textual. Only Textual's public hooks are overridden on purpose."""
    from textual.app import App
    from textual.widget import Widget

    from textual_glass import Glass, PixelMixin

    from sample_app import GlassSample, Sample

    overrides = {"BINDINGS", "__init__", "run_async", "exit", "get_system_commands"}
    dunder = re.compile(r"^__.*__$")
    textual_made = set(vars(type("Plain", (App,), {})))  # what Textual adds to every App subclass
    added = {name for name in set(vars(Glass)) - textual_made if not dunder.match(name) or name == "__init__"}
    clashes = {name for name in added - overrides if hasattr(App, name)}
    assert not clashes, clashes
    instance = set(vars(GlassSample())) - set(vars(Sample()))
    assert instance == {"glass"} and not hasattr(App, "glass")
    pixel = {name for name in vars(PixelMixin) if not dunder.match(name)}
    assert pixel == {"pixel_items", "glass", "glass_on", "render_lines"}
    assert not any(hasattr(Widget, name) for name in pixel - {"render_lines"}), "render_lines is wrapped on purpose"


# -- detection ---------------------------------------------------------------------------


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detect_mod, "_is_tty", lambda: True)


def test_override_off_wins(tty: None) -> None:
    assert not detect_mod.detect({"TEXTUAL_GLASS": "off", "TERM": "xterm-ghostty"}).enabled


def test_override_on(tty: None) -> None:
    assert detect_mod.detect({"TEXTUAL_GLASS": "on", "TERM": "xterm-256color"}, query=False).enabled


def test_env_hints(tty: None) -> None:
    for env in ({"TERM": "xterm-ghostty"}, {"TERM": "xterm-kitty"},
                {"TERM": "xterm-256color", "TERM_PROGRAM": "ghostty"},
                {"TERM": "xterm-256color", "KITTY_WINDOW_ID": "3"}):
        assert detect_mod.detect(env, query=False).enabled, env


def test_multiplexers_and_dumb_terminals_are_off(tty: None) -> None:
    assert not detect_mod.detect({"TERM": "tmux-256color", "TERM_PROGRAM": "ghostty"}).enabled
    assert not detect_mod.detect({"TERM": "xterm-ghostty", "TMUX": "/tmp/x"}).enabled
    assert not detect_mod.detect({"TERM": "dumb"}).enabled


def test_no_tty_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detect_mod, "_is_tty", lambda: False)
    assert not detect_mod.detect({"TERM": "xterm-ghostty"}).enabled


def _answering_pty(reply: bytes | None) -> tuple[str, threading.Thread, int]:
    master, slave = os.openpty()
    path = os.ttyname(slave)

    def serve() -> None:
        data = b""
        deadline = time.monotonic() + 2
        while b"\x1b[c" not in data and time.monotonic() < deadline:
            try:
                data += os.read(master, 1024)
            except OSError:
                return
        if reply is not None:
            os.write(master, reply)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return path, thread, slave


@pytest.mark.parametrize("reply, expected", [
    (b"\x1b_Gi=31;OK\x1b\\\x1b[?62;22c", True),
    (b"\x1b[?62;22c", False),
    (None, None),
])
def test_query(reply: bytes | None, expected: bool | None) -> None:
    path, thread, slave = _answering_pty(reply)
    t0 = time.monotonic()
    answer = detect_mod.query_terminal(path, timeout=0.3)
    elapsed = time.monotonic() - t0
    thread.join(1)
    os.close(slave)
    assert answer is expected
    assert elapsed < 0.6  # never hangs, even when nothing answers


def test_query_falls_back_when_nothing_answers(tty: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detect_mod, "query_terminal", lambda path, timeout: None)
    support = detect_mod.detect({"TERM": "xterm-256color"})
    assert not support.enabled and "did not answer" in support.reason
