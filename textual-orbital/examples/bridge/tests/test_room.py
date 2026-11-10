"""The bridge room by itself: where its light goes (``bridge.room``: pure numpy, deterministic), and the glass
items that draw it (``Arc``, ``Pool``, ``Pane`` in ``bridge.widgets``). Each item's light ends inside its own
box (glass rule 16, copied from glass's own edge test, since an item defined in an app isn't covered by
glass's tests), a partial redraw matches a full one, the labels keep a dark floor, red alert recolours only
the rail, the rings and the pane, and each pool follows its station's lamp. No terminal. The room in the
running app is in ``test_app.py``."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from bridge import room
from bridge.widgets import (
    LAMP_ROLES,
    POOL_STRENGTH,
    TIERS,
    Arc,
    Pane,
    Pool,
    StationView,
    _fits,
    lamp_color,
    plan_for,
    room_items,
    room_layout,
)
from bridge.widgets import Path as LightPath
from textual_glass.engine import make_grid
from textual_glass.palette import Palette
from textual_glass.render import Canvas, Grid, Item, paint, to_u8

ROOT = Path(__file__).resolve().parent.parent
#: The orbital theme's colours (``textual_orbital.ORBITAL``).
PAL = Palette(dark=True, background=(7, 11, 24), surface=(13, 20, 48), panel=(22, 32, 61), foreground=(201, 214, 242),
              primary=(56, 225, 255), secondary=(164, 107, 255), accent=(255, 194, 75), success=(109, 255, 168),
              warning=(255, 194, 75), error=(255, 77, 109), bg_top=(13, 20, 48), bg_bottom=(7, 11, 24),
              backdrop="nebula")
#: Decks (the window less the panel's border and the keys line) and cell sizes: the harness's 9 x 19 px
#: cells at 70x22, 90x28 and 140x45, Retina cells (18 x 38 px), an odd size, and a cramped one.
CASES = [((68, 19), (9, 19)), ((88, 25), (9, 19)), ((138, 42), (9, 19)), ((88, 25), (18, 38)),
         ((138, 42), (18, 38)), ((101, 31), (9, 19)), ((54, 13), (9, 19))]
IDS = [f"{d[0]}x{d[1]}@{c[0]}x{c[1]}" for d, c in CASES]
#: A mix of states, one per station (the last one vacant).
VIEWS = [StationView("cy", lamp="working"), StationView("ash", lamp="waiting"), StationView("bo", lamp="error"),
         StationView("dee", lamp="done"), StationView("eli", lamp="dim"), StationView()]
#: Everyone waiting: a path of light from every station.
ALL_WAITING = [StationView(m, lamp="waiting") for m in ("cy", "ash", "bo", "dee", "eli", "fay")]


def items_for(deck: tuple[int, int], cell: tuple[float, float], alert: bool = False, waiting: int = 0,
              views: list[StationView] = VIEWS) -> tuple[list[Item], room.Layout]:
    """The room's items for a deck at (1, 1), as the deck yields them."""
    layout = room_layout(plan_for(*deck, cell=cell), deck)
    return room_items(room.shape(layout), (1.0, 1.0), views, waiting, alert, PAL, key="t"), layout


def render(items: list[Item], deck: tuple[int, int], cell: tuple[float, float]) -> tuple[np.ndarray, Grid]:
    """The items painted over black, on the layer glass would draw for a window around the deck."""
    grid = make_grid(deck[0] + 2, deck[1] + 3, *cell)
    data = np.zeros((grid.height, grid.width, 3), np.float32)
    paint(Canvas(data, 0, 0), grid, items)
    return data, grid


# ---------------------------------------------------------------- where the light goes --


def test_room_imports_nothing_from_textual() -> None:
    tree = ast.parse((ROOT / "bridge" / "room.py").read_text())
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any(n.split(".")[0].startswith("textual") for n in names), names
    code = ("import sys, bridge.room; "
            "bad = [m for m in sys.modules if m.split('.')[0].startswith('textual')]; print(bad); "
            "sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(("deck", "cell"), CASES, ids=IDS)
def test_the_rail_runs_between_the_stations_and_the_chair(deck: tuple[int, int], cell: tuple[float, float]) -> None:
    """The rail clears every station's sprite and labels by ``RAIL_CLEAR`` rows (in the arc's units) and,
    where the layout fits, passes outside the chair (in a cramped one, behind it); the well's rings lie
    inside it. Every lit arc ends inside the deck: above its bottom edge and inside its sides."""
    layout = room_layout(plan_for(*deck, cell=cell), deck)
    shape = room.shape(layout)
    cx, cy, rx, ry = layout.arc
    rail = shape.rail[2] / rx
    nearest = min(room._nearest(s, cx, cy, rx, ry) for s in layout.stations)
    chair = room._farthest(layout.chair, cx, cy, rx, ry)
    assert rail == pytest.approx(nearest - room.RAIL_CLEAR / ry)
    if deck != (54, 13):  # every layout but the cramped one
        assert rail > chair + 0.5 / ry, "outside the chair"
    assert shape.rail[:2] == (cx, cy) and shape.rail[3] / ry == pytest.approx(rail)
    assert [e[2] for e, _ in shape.rings] == sorted((e[2] for e, _ in shape.rings), reverse=True)
    for e, span in [(shape.rail, shape.rail_span), *shape.rings]:
        assert e[2] < shape.rail[2] + 1e-9
        assert 0 <= span[0] < 90 < span[1] <= 180
        xs, ys = room.arc_points(e, span)
        assert ys.max() <= deck[1] - room.FLOOR_EDGE + 1e-6, "the arc ends above the deck's bottom"
        assert xs.min() >= room.SIDE_EDGE - 1e-6 and xs.max() <= deck[0] - room.SIDE_EDGE + 1e-6
    assert len(shape.pools) == len(layout.stations) == 6
    for mid, _y, half, _up, _down in (*shape.pools, shape.chair):
        assert mid - half > 0 and mid + half < deck[0], "every pool's light ends inside the deck"
    assert shape.pane == layout.viewscreen


def test_shape_is_deterministic_and_cached() -> None:
    layout = room_layout(plan_for(88, 25, cell=(9, 19)), (88, 25))
    first = room.shape(layout)
    room.shape.cache_clear()
    assert room.shape(layout) == first
    assert room.shape(layout) is room.shape(layout)
    code = ("from tests.test_room import items_for, render; import hashlib; "
            "data = render(items_for((88, 25), (9, 19))[0], (88, 25), (9, 19))[0]; "
            "print(hashlib.sha256(data.tobytes()).hexdigest())")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    data, _ = render(items_for((88, 25), (9, 19))[0], (88, 25), (9, 19))
    assert done.stdout.strip() == hashlib.sha256(data.tobytes()).hexdigest(), done.stderr


def test_the_room_survives_odd_layouts() -> None:
    for deck in [(30, 8), (12, 4), (2, 1), (300, 80)]:
        items, _ = items_for(deck, (9, 19))
        data, _ = render(items, deck, (9, 19))
        assert np.isfinite(data).all()
    flat = room.Layout((40, 12), (8, 0, 24, 3), ((1, 4, 10, 5),), (7, 3), 2, (15, 8, 11, 4), (7, 3), (0, 0, 0, 0))
    shape = room.shape(flat)
    assert shape.rail[2] > 0 and len(shape.pools) == 1, "no arc: drawn about the middle"
    items = room_items(shape, (0.0, 0.0), [StationView()], 0, False, PAL)
    data, _ = render(items, (40, 12), (9, 19))
    assert np.isfinite(data).all() and data.max() > 0.1


# ---------------------------------------------------------------- the items (rule 16) --


def _box_border(img: np.ndarray, box: tuple[float, float, float, float]) -> np.ndarray:
    """The outermost ring of pixels inside ``box`` (glass's ``tests/test_units.py``)."""
    x0, y0 = int(np.floor(box[0])), int(np.floor(box[1]))
    x1, y1 = int(np.ceil(box[2])) - 1, int(np.ceil(box[3])) - 1
    region = img[y0:y1 + 1, x0:x1 + 1].astype(int)
    return np.concatenate([region[0], region[-1], region[:, 0], region[:, -1]])


def edge_samples() -> list[tuple[str, Item]]:
    """Every item class in the variant that draws the most light, on a deck at (3, 3) with room around
    it: the rail (beads and fill), a dashed ring, a waiting station's pool and its path of light, the chair's
    amber pool and the pane. The pane's box reaches above the deck, so the deck starts three rows down."""
    deck = (88, 25)
    layout = room_layout(plan_for(*deck, cell=(9, 19)), deck)
    items = room_items(room.shape(layout), (3.0, 3.0), VIEWS, 2, True, PAL, key="edge")
    picked = {"rail": next(i for i in items if i.ident.startswith("bridge-rail")),
              "ring": next(i for i in items if i.ident.startswith("bridge-ring")),
              "pool": next(i for i in items if i.ident.startswith("bridge-pool1")),
              "path": next(i for i in items if i.ident.startswith("bridge-path1")),
              "chair": next(i for i in items if i.ident.startswith("bridge-chair-pool")),
              "pane": next(i for i in items if i.ident.startswith("bridge-pane"))}
    assert {type(i) for i in picked.values()} == {Arc, Pool, LightPath, Pane}, "every item class is checked"
    return list(picked.items())


@pytest.mark.parametrize("grid_scale", [1.0, 0.747, 0.5], ids=["terminal", "capped", "half"])
@pytest.mark.parametrize("cell", [(9.0, 19.0), (18.0, 38.0)], ids=["9x19", "18x38"])
@pytest.mark.parametrize("fade", [True, False], ids=["with-box-fade", "own-falloff"])
@pytest.mark.parametrize(("name", "item"), edge_samples(), ids=[n for n, _ in edge_samples()])
def test_items_draw_nothing_at_the_edge_of_their_box(name: str, item: Item, fade: bool, cell: tuple[float, float],
                                                      grid_scale: float, monkeypatch: pytest.MonkeyPatch) -> None:
    """Glass rule 16, as glass checks its own items: each item alone on mid-grey (shadows count as well as
    light) and on a transparent overlay layer (any coverage shows), with and without glass's safety fade, on
    a layer at the terminal's resolution, one capped at ``MAX_PIXELS`` and the half resolution glass draws a
    new page at. Its box's outermost pixel ring must stay untouched."""
    if not fade:
        monkeypatch.setattr(Canvas, "_masked", lambda self, view, alpha: (
            np.asarray(alpha, np.float32)[..., None] if np.asarray(alpha).ndim == 2 else np.asarray(alpha, np.float32)))
    sx, sy = cell[0] * grid_scale, cell[1] * grid_scale
    cols, rows = 94, 31
    grid = Grid(sx, sy, math.ceil(cols * sx), math.ceil(rows * sy))
    w, h = grid.width, grid.height
    grey = np.full((h, w, 3), 0.25, np.float32)
    data = grey.copy()
    paint(Canvas(data, 0, 0), grid, [item])
    overlay = np.zeros((h, w, 4), np.float32)
    paint(Canvas(overlay, 0, 0), grid, [item])
    box = item.bbox(grid)
    assert box[0] >= 0 and box[1] >= 0 and box[2] <= w and box[3] <= h, "keep the sample inside the canvas"
    change = np.abs(to_u8(data).astype(int) - to_u8(grey).astype(int))
    assert change.max() > 8, f"the {name} is drawn"
    assert _box_border(change, box).max() <= 1, (name, int(_box_border(change, box).max()))
    assert _box_border(to_u8(overlay)[..., 3:], box).max() <= 1
    assert np.isfinite(data).all() and np.isfinite(overlay).all()


@pytest.mark.parametrize(("deck", "cell"), CASES, ids=IDS)
def test_a_partial_redraw_matches_the_full_one(deck: tuple[int, int], cell: tuple[float, float]) -> None:
    """Glass redraws rectangles of the backdrop (a pool whose lamp changed, a title on the panel's border)
    with the same items: their pixels there must be the ones a full render draws."""
    items, _ = items_for(deck, cell, alert=True, waiting=2, views=ALL_WAITING)
    full, grid = render(items, deck, cell)
    for x, y, rw, rh in [(37, 11, 301, 97), (0, 0, grid.width // 3, grid.height // 2),
                         (grid.width // 2 - 40, grid.height - 120, 160, 117)]:
        rw, rh = min(rw, grid.width - x), min(rh, grid.height - y)
        part = np.zeros((rh, rw, 3), np.float32)
        paint(Canvas(part, x, y), grid, items)
        assert np.array_equal(to_u8(part), to_u8(full[y:y + rh, x:x + rw])), (x, y, rw, rh)


# ---------------------------------------------------------------- what it shows --


@pytest.mark.parametrize(("deck", "cell"), CASES, ids=IDS)
def test_the_labels_keep_a_dark_floor(deck: tuple[int, int], cell: tuple[float, float]) -> None:
    """Text draws over the room: under every station's labels and the chair's, the light stays faint (the
    pools light the floor behind the consoles, the rail, the rings and the paths of light pass clear of the
    labels, even with everyone waiting). Not in a layout that doesn't fit (the cramped one): its stations
    already overlap each other's labels."""
    if not _fits(plan_for(*deck, cell=cell), *deck):
        pytest.skip("the stations overlap: nothing to keep clear")
    for alert, waiting, views in ((False, 0, VIEWS), (True, 2, VIEWS), (True, 6, ALL_WAITING)):
        items, layout = items_for(deck, cell, alert=alert, waiting=waiting, views=views)
        data, grid = render(items, deck, cell)
        light = to_u8(data).astype(np.float32) @ np.array([0.3, 0.59, 0.11], np.float32)
        rows = layout.art[1]
        boxes = [(x, y + rows, w, layout.label_rows) for x, y, w, _h in layout.stations]
        x, y, w, h = layout.chair
        boxes.append((x, y + layout.chair_art[1], w, h - layout.chair_art[1]))
        for bx, by, bw, bh in boxes:
            x0, x1 = int((1 + bx + 1) * grid.sx), int((1 + bx + bw - 1) * grid.sx)
            y0, y1 = int((1 + by + 0.15) * grid.sy), int((1 + by + bh) * grid.sy)
            under = light[y0:y1, x0:x1]
            peak = float(np.max(under)) if under.size else 0.0
            assert peak <= 40, (deck, cell, alert, (bx, by, bw, bh), peak)


def test_each_station_has_a_pool_in_its_lamps_colour() -> None:
    items, _ = items_for((88, 25), (9, 19))
    pools = [i for i in items if isinstance(i, Pool) and i.ident.startswith("bridge-pool")]
    assert [p.ident for p in pools] == [f"bridge-pool{n}:t" for n in range(6)], "an ident each: edited alone"
    for pool, view in zip(pools, VIEWS, strict=True):
        assert pool.color == lamp_color(view.lamp, PAL) and pool.strength == POOL_STRENGTH[view.lamp]
    assert set(POOL_STRENGTH) == set(LAMP_ROLES)
    assert POOL_STRENGTH["dim"] < min(v for k, v in POOL_STRENGTH.items() if k != "dim"), "dim pools stay dim"
    idents = [i.ident for i in items]
    assert len(set(idents)) == len(idents)
    # one station's state changes: only its pool's item does (and a hand going up lights its path)
    before = {i.ident: i for i in items}
    for lamp, extra in (("working", set()), ("waiting", {"bridge-path3:t"})):
        views = [*VIEWS[:3], dataclasses.replace(VIEWS[3], lamp=lamp), *VIEWS[4:]]
        after = {i.ident: i for i in items_for((88, 25), (9, 19), views=views)[0]}
        changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
        assert changed == {"bridge-pool3:t", *extra}, changed


def test_the_chair_turns_amber_while_anyone_waits() -> None:
    calm, _ = items_for((88, 25), (9, 19), waiting=0)
    waiting, _ = items_for((88, 25), (9, 19), waiting=1)
    diff = [(a, b) for a, b in zip(calm, waiting, strict=True) if a != b]
    assert len(diff) == 1
    before, after = diff[0]
    assert isinstance(after, Pool) and isinstance(before, Pool) and after.ident == "bridge-chair-pool:t"
    assert after.color == PAL.warning and before.color != PAL.warning and after.strength > before.strength


def test_red_alert_turns_the_rail_the_rings_and_the_pane_red() -> None:
    calm, _ = items_for((88, 25), (9, 19))
    alert, _ = items_for((88, 25), (9, 19), alert=True)
    changed = {a.ident for a, b in zip(calm, alert, strict=True) if a != b}
    assert changed == {i.ident for i in calm if isinstance(i, (Arc, Pane))}, "the pools keep their lamps"
    for a, b in zip(calm, alert, strict=True):
        if isinstance(b, (Arc, Pane)):
            assert isinstance(a, type(b))
            assert dataclasses.replace(b, c1=a.c1, c2=a.c2) == a, "the same shapes, only the colours change"
            assert b.c1 == PAL.error
    calm_px, _ = render([i for i in calm if not isinstance(i, Pool)], (88, 25), (9, 19))
    alert_px, _ = render([i for i in alert if not isinstance(i, Pool)], (88, 25), (9, 19))

    def red(p: np.ndarray) -> int:
        r, g, b = (to_u8(p)[..., i].astype(int) for i in range(3))
        return int(((r > 120) & (r > 2 * g) & (r > 2 * b)).sum())

    assert red(alert_px) > 20 * max(1, red(calm_px))


def test_the_room_is_light_not_paint() -> None:
    """Nothing opaque: over black the room leaves almost every pixel dark, and its brightest light is the rail,
    the beads and the pane's edge (thin lines), not filled areas."""
    items, _ = items_for((88, 25), (9, 19), alert=True, waiting=2)
    data, _ = render(items, (88, 25), (9, 19))
    luma = to_u8(data).astype(np.float32) @ np.array([0.3, 0.59, 0.11], np.float32)
    assert (luma < 24).mean() > 0.8, "mostly dark: the nebula shows through"
    assert (luma > 90).mean() < 0.01, "bright only in thin lines"


@pytest.mark.parametrize(("deck", "cell"), CASES, ids=IDS)
def test_a_path_of_light_leads_from_each_waiting_station_to_the_chair(deck: tuple[int, int],
                                                                      cell: tuple[float, float]) -> None:
    """One faint amber path per waiting station, its own item: it starts beside the console, at its foot,
    clear of its own station's box (and labels), and ends in the chair's sprite."""
    items, layout = items_for(deck, cell, views=VIEWS)
    paths = [i for i in items if isinstance(i, LightPath)]
    assert [p.ident for p in paths] == ["bridge-path1:t"], "only the waiting station has one"
    assert paths[0].color == PAL.warning
    for n, curve in enumerate(room.shape(layout).paths):
        (x0, y0), _p1, _p2, (x3, y3) = curve
        x, y, w, h = layout.stations[n]
        assert y < y0 < y + layout.art[1], "at its console's foot"
        if _fits(plan_for(*deck, cell=cell), *deck):
            assert not x - 0.25 < x0 < x + w + 0.25, "beside its station's box, not over its labels"
        cx, cy, cw, ch = layout.chair
        assert cx <= x3 <= cx + cw and cy <= y3 <= cy + layout.chair_art[1], "into the chair"
    # the deck passes its stations' views as a generator: the paths must see them too
    gen, _ = items_for(deck, cell, views=(v for v in VIEWS))  # type: ignore[arg-type]
    assert [i.ident for i in gen] == [i.ident for i in items]


def test_pools_grow_more_slowly_than_the_stations() -> None:
    """On a big window a pool stays a pool: its reach, relative to the station's sprite, shrinks as the
    stations grow."""
    ratios = []
    for tier in sorted(TIERS, key=lambda t: t.art):
        half = room._pool(100.0, 10.0, tier.art, 200)[2]
        ratios.append(half / tier.art[0])
    assert ratios == sorted(ratios, reverse=True)
    assert ratios[-1] <= 0.6
