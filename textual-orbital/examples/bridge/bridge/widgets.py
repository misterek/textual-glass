"""The bridge's widgets: stations in an arc, the viewscreen, the captain's chair, and the deck that lays
them out and lights the room around them. Pixel widgets on textual-glass: the crew and their consoles are
sprites from the crew pack, the room is light in glass's backdrop (``Arc``, ``Pool`` and ``Pane`` items,
placed by ``bridge.room``), the labels are text, and without glass every sprite has a small text form in the
same cells (and there is no room).
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
from rich.style import Style
from textual.app import ComposeResult
from textual.events import Resize
from textual.strip import Strip
from textual.widget import Widget

from bridge import room
from bridge.crew import Pack
from textual_glass import SPRITE_MAX_PX, PixelContext, PixelWidget
from textual_glass.palette import Palette, border_pair, mix
from textual_glass.render import Box, Canvas, Colour, FRect, Grid, Item, col, x_gradient
from textual_glass.widgets import DIM, FAINT, SOFT, Part, ellipsize, placeholder_parts, strip, tone

RGB = tuple[int, int, int]

#: Lamp colours by role, from the theme (``dim`` is the text colour faded most of the way out).
LAMP_ROLES = ("working", "waiting", "error", "done", "dim")
#: A pose's glyph without glass (single-width characters only).
POSE_GLYPHS = {
    "empty": "·", "seated": "▪", "typing": "▸▸", "done": "✓", "hand_up": "▲", "waving": "▲▲",
    "standing": "▲▲▲", "jolt": "✕", "tea": "≈", "dozing": "z",
}
MAX_ENSIGNS = 3
#: An agent's tag after the age on the status line: the word, then a short form for where the word doesn't fit.
AGENT_TAGS = {"codex": ("codex", "cx")}


def lamp_color(role: str, palette: Palette) -> RGB:
    """The console lamp's colour for a role: working success, waiting warning, error error, done primary,
    and anything else (empty, seated, dozing) dim."""
    if role == "working":
        return palette.success
    if role == "waiting":
        return palette.warning
    if role == "error":
        return palette.error
    if role == "done":
        return palette.primary
    return mix(palette.foreground, palette.background, FAINT)


def hexed(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def centred(parts: list[Part], width: int) -> list[Part]:
    used = sum(len(t) for t, _ in parts)
    return [(" " * max(0, (width - used) // 2), ""), *parts]


class BridgeWidget(PixelWidget):
    """A pixel widget whose lines get the background behind it while glass is off (``render_line``
    strips are used as they are, so without this they would show the terminal's default background)."""

    def render_line(self, y: int) -> Strip:
        line = self.draw(y)
        if self.glass_on:
            return line
        return line.apply_style(Style(bgcolor=self.background_colors[1].rich_color))

    def draw(self, y: int) -> Strip:
        return Strip.blank(self.size.width)


@dataclass(frozen=True)
class StationView:
    """What one station shows. Equal views draw the same, so the app compares them to stay quiet."""

    member: str | None = None   # the crew member's id; None: nobody at the console
    name: str = ""              # the member's name (the text form shows it)
    pose: str = "empty"
    lamp: str = "dim"           # a role in LAMP_ROLES
    ensigns: int = 0            # running subagents, 0 to 3
    project: str = ""           # first label line, in the member's uniform colour
    uniform: RGB | None = None
    status: str = ""            # second label line: the state word, in the lamp's colour...
    age: str = ""               # ... and the age (fmt_age), dim
    spark: bool = False         # a tool failed a moment ago: a small mark after the age
    model: str = ""             # third label line, when the station has room for it
    leaving: bool = False       # the session ended: the console is empty and the labels are dim
    agent: str = ""             # the agent, faint after the age, when it isn't Claude Code ("codex")

    @property
    def waiting(self) -> bool:
        return self.lamp == "waiting"


class Station(BridgeWidget):
    """One console on the bridge: the crew pack's station sprite, and its labels under it."""

    def __init__(self, pack: Pack, index: int, *, id: str | None = None) -> None:
        super().__init__(id=id, classes="station")
        self.pack = pack
        self.index = index
        self.view = StationView()
        self.art = (10, 4)       # the sprite's size in cells
        self.label_rows = 2
        self.updates = 0         # how many times the view changed (the tests check quiet polls with it)

    def set_view(self, view: StationView) -> bool:
        """Show ``view``; returns whether anything changed (nothing is redrawn otherwise)."""
        if view == self.view:
            return False
        self.view = view
        self.updates += 1
        self.refresh()
        return True

    def set_art(self, cols: int, rows: int, label_rows: int) -> None:
        if (cols, rows, label_rows) != (*self.art, self.label_rows):
            self.art, self.label_rows = (cols, rows), label_rows
            self.refresh()

    def sprite_key(self, lamp: RGB) -> tuple[Any, ...]:
        """A small finite set: pack, member, pose, lamp colour, ensigns (the engine adds the size)."""
        v = self.view
        return ("bridge-station", self.pack.name, v.member, v.pose, lamp, min(MAX_ENSIGNS, v.ensigns))

    def draw(self, y: int) -> Strip:
        width = self.size.width
        cols, rows = self.art
        pal = self.theme_palette
        v = self.view
        lamp = lamp_color(v.lamp, pal)
        if y < rows:
            pad = (" " * max(0, (width - cols) // 2), "")
            image_id = None
            if self.glass is not None:
                pack, member, pose, ensigns = self.pack, v.member, v.pose, min(MAX_ENSIGNS, v.ensigns)

                def make(w: int, h: int) -> tuple[list[bytes], int]:
                    return pack.station(member, pose, lamp, ensigns, w, h)

                image_id = self.glass.sprite(self.sprite_key(lamp), cols, rows, make, waiter=self)
            if image_id is not None:
                return strip([pad, placeholder_parts(image_id, y, cols)], width)
            return strip([pad, *self._fallback(y, pal, lamp)], width)
        line = y - rows
        faded = tone(pal, FAINT)
        if line == 0:
            if not v.project:
                return Strip.blank(width)
            style = faded if v.leaving or v.uniform is None else f"bold {hexed(v.uniform)}"
            return strip(centred([(ellipsize(v.project, width), style)], width), width)
        if line == 1:
            if not v.status:
                return strip(centred([("vacant", tone(pal, 0.72))], width), width)
            status_style = faded if v.leaving else f"bold {hexed(lamp)}"
            parts: list[Part] = [(v.status, status_style)]
            if v.age:
                parts += [(" ", ""), (v.age, tone(pal, DIM))]
            if v.spark:
                parts += [(" ϟ", f"bold {hexed(pal.error)}")]
            if v.agent:  # the word, the short form where it doesn't fit, or nothing on the tightest stations
                used = sum(len(text) for text, _ in parts)
                fits = [t for t in AGENT_TAGS.get(v.agent, (v.agent,)) if used + 1 + len(t) <= width]
                parts += [(" " + fits[0], faded)] if fits else []
            return strip(centred(parts, width), width)
        if line == 2 and v.model and not v.leaving:
            return strip(centred([(ellipsize(v.model, width), tone(pal, FAINT))], width), width)
        return Strip.blank(width)

    def _fallback(self, y: int, pal: Palette, lamp: RGB) -> list[Part]:
        """The station in text, ``self.art`` big: the member's name, the pose's glyph, a console and its lamp."""
        cols, rows = self.art
        v = self.view
        body = hexed(v.uniform) if v.uniform else tone(pal, DIM)
        if y == rows - 1:
            console = "▔" * max(1, cols - 2)
            return [(" ", ""), (console[:-1], tone(pal, FAINT)), ("●", hexed(lamp))]
        if y == rows - 2:
            glyph = POSE_GLYPHS.get(v.pose, "?") if v.member else "·"
            ensigns = "·" * min(MAX_ENSIGNS, v.ensigns)
            return centred([(glyph, f"bold {body}"), (" " + ensigns if ensigns else "", tone(pal, SOFT))], cols)
        if y == rows - 3 and v.member:
            return centred([((v.name or v.member)[:cols], body)], cols)
        return []


#: A viewscreen whose frames change more than this share of the picture is shown as a still (its first
#: frame): see ``steady``.
STEADY = 0.05


def steady(frames: list[np.ndarray], gap: int) -> tuple[list[np.ndarray], int]:
    """The viewscreen's frames for its tiles: as they are while each changes only a little of the picture
    (stars twinkling, each inside one tile), else the first frame alone. Every tile is a terminal animation of
    its own, started when its upload lands, so a change across the whole picture (red alert's pulse) would
    show the tiles' seams as they pulse out of step."""
    first = frames[0][..., :3].astype(np.int16)
    for frame in frames[1:]:
        changed = float((np.abs(frame[..., :3].astype(np.int16) - first).max(axis=2) > 8).mean())
        if changed > STEADY:
            return frames[:1], gap
    return frames, gap


class Pictures:
    """The pack's viewscreen at its full pixel size, made once per (pack, alert, size) and shared by its
    tiles. Their makers run one after another in glass's worker thread; the lock keeps two from making it
    twice."""

    SIZE = 4

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[Any, ...], tuple[list[np.ndarray], int]] = OrderedDict()

    def get(self, pack: Pack, alert: bool, width: int, height: int) -> tuple[list[np.ndarray], int]:
        key = (id(pack), pack.name, alert, width, height)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
            frames, gap = pack.viewscreen(alert, width, height)
            arrays = [np.frombuffer(f, np.uint8).reshape(height, width, 4) for f in frames]
            made = self._cache[key] = steady(arrays, gap)
            while len(self._cache) > self.SIZE:
                self._cache.popitem(last=False)
            return made


PICTURES = Pictures()


def tile_columns(cols: int, cell_w: float) -> list[tuple[int, int]]:
    """``cols`` cells split into runs (first column, width) of at most ``SPRITE_MAX_PX`` terminal pixels on
    cells ``cell_w`` wide, as even as can be: one glass sprite each, so each is drawn at its full size."""
    most = max(1, int(SPRITE_MAX_PX // max(1.0, cell_w)))
    count = max(1, math.ceil(cols / most))
    edges = [round(k * cols / count) for k in range(count + 1)]
    return [(edges[k], edges[k + 1] - edges[k]) for k in range(count) if edges[k + 1] > edges[k]]


def tile_frames(frames: list[np.ndarray], x0: int, x1: int, width: int, height: int) -> list[bytes]:
    """Columns ``x0`` to ``x1`` of each frame, at ``width`` x ``height`` (nearest neighbour where glass
    asks for another size: a pixel off on fractional cells, or a screen taller than ``SPRITE_MAX_PX``)."""
    out = []
    for frame in frames:
        part = frame[:, x0:x1]
        h, w = part.shape[:2]
        if (h, w) != (height, width) and h and w:
            ys = np.minimum(((np.arange(height) + 0.5) * h / height).astype(np.intp), h - 1)
            xs = np.minimum(((np.arange(width) + 0.5) * w / width).astype(np.intp), w - 1)
            part = part[ys][:, xs]
        out.append(np.ascontiguousarray(part).tobytes())
    return out


class Viewscreen(BridgeWidget):
    """The big screen at the front of the bridge, at its full pixel size: column tiles of at most
    ``SPRITE_MAX_PX`` pixels, each a glass sprite cut from one picture of the whole screen (a single sprite
    would be capped at that size and stretched several times over). Red alert tints it.

    A tile shows as soon as it lands; until then the tile that was there shows (an alert sweeps across in a
    moment), or the text form on a new layout. Glass deletes a landed sprite no line shows within two frames,
    so tiles can't be held back until all have landed."""

    def __init__(self, pack: Pack, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.pack = pack
        self.alert = False
        self._shown: tuple[tuple[Any, ...], tuple[int | None, ...]] | None = None

    def set_alert(self, alert: bool) -> None:
        if alert != self.alert:
            self.alert = alert
            self.refresh()

    def tiles(self, width: int, height: int) -> list[tuple[int, int, int | None]]:
        """Each tile's first column, width and image id (None while it is on its way)."""
        glass = self.glass
        assert glass is not None
        cw, ch = glass.cell_pixels()
        runs = tile_columns(width, cw)
        full_w, full_h = max(2, round(width * cw)), max(2, round(height * ch))
        geometry = (width, height, full_w, full_h, tuple(runs))
        before = self._shown[1] if self._shown is not None and self._shown[0] == geometry else None
        pack, alert = self.pack, self.alert
        ids: list[int | None] = []
        for n, (x0, cols) in enumerate(runs):
            px0, px1 = round(x0 * cw), round((x0 + cols) * cw)

            def make(w: int, h: int, px0: int = px0, px1: int = px1) -> tuple[list[bytes], int]:
                frames, gap = PICTURES.get(pack, alert, full_w, full_h)
                return tile_frames(frames, px0, px1, w, h), gap

            key = ("bridge-viewscreen", pack.name, alert, n, width, full_w, full_h)
            image_id = glass.sprite(key, cols, height, make, waiter=self)
            if image_id is None and before is not None:
                image_id = before[n]
            ids.append(image_id)
        self._shown = (geometry, tuple(ids))
        return [(x0, cols, image_id) for (x0, cols), image_id in zip(runs, ids, strict=True)]

    def draw(self, y: int) -> Strip:
        width, height = self.size
        cells = self._fallback(y, width, height, self.theme_palette)
        if not self.glass_on:
            return strip(cells, width)
        parts: list[Part] = []
        for x0, cols, image_id in self.tiles(width, height):
            parts += cells[x0:x0 + cols] if image_id is None else [placeholder_parts(image_id, y, cols)]
        return strip(parts, width)

    def _fallback(self, y: int, width: int, height: int, pal: Palette) -> list[Part]:
        """The screen in text, a part per column: its edges, a few stars, and RED ALERT in red alert."""
        edge = hexed(pal.error) if self.alert else tone(pal, FAINT)
        if y == 0 or y == height - 1:
            return [("▁" if y == 0 else "▔", edge)] * width
        if y == height // 2 and self.alert:
            word, style = "RED ALERT", f"bold {hexed(pal.error)}"
            line = (" " * max(0, (width - len(word)) // 2) + word).ljust(width)[:width]
            return [(c, style if c != " " else "") for c in line]
        return [("·" if (x * 7 + y * 13) % 23 == 0 else " ", tone(pal, FAINT)) for x in range(width)]


class Chair(BridgeWidget):
    """The captain's chair (the user), and how many crew are waiting on them."""

    def __init__(self, pack: Pack, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.pack = pack
        self.waiting = 0
        self.art = (8, 3)

    def set_waiting(self, waiting: int) -> None:
        if waiting != self.waiting:
            self.waiting = waiting
            self.refresh()

    def set_art(self, cols: int, rows: int) -> None:
        if (cols, rows) != self.art:
            self.art = (cols, rows)
            self.refresh()

    def draw(self, y: int) -> Strip:
        width = self.size.width
        cols, rows = self.art
        pal = self.theme_palette
        waiting = min(self.waiting, 9)
        if y < rows:
            pad = (" " * max(0, (width - cols) // 2), "")
            image_id = None
            if self.glass is not None:
                pack = self.pack

                def make(w: int, h: int) -> tuple[list[bytes], int]:
                    return pack.chair(waiting, w, h)

                image_id = self.glass.sprite(("bridge-chair", pack.name, waiting), cols, rows, make, waiter=self)
            if image_id is not None:
                return strip([pad, placeholder_parts(image_id, y, cols)], width)
            if y == rows - 1:
                return strip([pad, ("\\_" + "_" * max(0, cols - 4) + "_/", tone(pal, DIM))], width)
            if y == rows - 2:
                return strip([pad, (" ▲" * min(3, waiting), f"bold {hexed(pal.warning)}")], width)
            return Strip.blank(width)
        if y == rows:
            if waiting:
                word = f"{self.waiting} need you" if width >= 11 else f"{self.waiting} wait"
                return strip(centred([(word, f"bold {hexed(pal.warning)}")], width), width)
            return strip(centred([("you", tone(pal, DIM))], width), width)
        return Strip.blank(width)


# -- the arc ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def overlaps(self, other: Rect) -> bool:
        return self.x < other.x + other.w and other.x < self.x + self.w and \
            self.y < other.y + other.h and other.y < self.y + self.h


@dataclass(frozen=True)
class Tier:
    """One size of everything: the station sprite, its label lines, the viewscreen and the chair."""

    art: tuple[int, int]
    label_rows: int
    label_width: int
    screen_rows: int
    chair: tuple[int, int]


#: Biggest first; the deck takes the biggest that fits without overlaps and stays crisp on the real cell
#: size (``crisp_tiers``): from 63 x 57 px (7 x 3 cells at 9 x 19 px) to 180 x 152 px (20 x 8). On Retina
#: cells (about 18 x 38 px) 10 x 4 is the biggest that glass draws unstretched.
TIERS = (
    Tier((20, 8), 3, 22, 7, (20, 8)),
    Tier((17, 7), 3, 20, 6, (17, 7)),
    Tier((14, 6), 2, 15, 4, (14, 6)),
    Tier((10, 4), 2, 14, 4, (10, 4)),
    Tier((7, 3), 2, 13, 3, (7, 3)),
)
#: Where the stations sit, left to right: degrees around the chair (0 is to its right).
ANGLES = (165, 135, 105, 75, 45, 15)
STATIONS = len(ANGLES)


@dataclass(frozen=True)
class Plan:
    tier: Tier
    viewscreen: Rect
    stations: tuple[Rect, ...]
    chair: Rect
    #: The ellipse the stations' centres sit on: centre x, y and radii x, y in cells (the room follows it).
    arc: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


def crisp_tiers(tiers: tuple[Tier, ...], cell: tuple[float, float] | None) -> tuple[Tier, ...]:
    """The tiers whose station and chair sprites glass draws at their full size on cells of ``cell``
    terminal pixels. Glass renders a sprite at most ``SPRITE_MAX_PX`` a side and lets the terminal stretch
    it, which blurs it; on a Retina screen (cells of about 18 x 38 px) a big station would get no more
    pixels than a smaller one, only softer ones. None (glass off): every tier."""
    if cell is None:
        return tiers
    cw, ch = cell
    ok = tuple(t for t in tiers
               if all(cols * cw <= SPRITE_MAX_PX and rows * ch <= SPRITE_MAX_PX for cols, rows in (t.art, t.chair)))
    return ok or tiers[-1:]


def plan_for(width: int, height: int, tiers: Iterable[Tier] = TIERS,
             cell: tuple[float, float] | None = None) -> Plan:
    """Lay the bridge out in ``width`` x ``height`` cells: the viewscreen across the top, the stations on
    an arc around the chair, the chair at the bottom centre. The biggest tier that fits wins, among those
    drawn crisp on cells of ``cell`` terminal pixels (``crisp_tiers``)."""
    tiers = crisp_tiers(tuple(tiers), cell)
    for tier in tiers:
        plan = _arc(width, height, tier)
        if plan is not None and _fits(plan, width, height):
            return plan
    plan = _arc(width, height, tiers[-1], force=True)
    assert plan is not None
    return plan


def _arc(width: int, height: int, tier: Tier, force: bool = False) -> Plan | None:
    cols, rows = tier.art
    sw = max(cols, tier.label_width)
    sh = rows + tier.label_rows
    top = tier.screen_rows + 1
    spread = math.sin(math.radians(75)) - math.sin(math.radians(15))
    ry = (height - sh - top) / spread
    if ry < sh and not force:
        return None
    ry = max(0.0, ry)
    cy = height - sh / 2 + ry * math.sin(math.radians(15))
    cx = width / 2
    rx = max(0.0, (cx - sw / 2) / math.cos(math.radians(15)))
    stations = []
    for angle in ANGLES:
        a = math.radians(angle)
        x = round(cx + rx * math.cos(a) - sw / 2)
        y = round(cy - ry * math.sin(a) - sh / 2)
        stations.append(Rect(max(0, min(width - sw, x)), max(0, min(height - sh, y)), sw, sh))
    ccols, crows = tier.chair
    cw, ch = max(ccols, 11), crows + 1
    chair = Rect(round(cx - cw / 2), max(0, height - ch), cw, ch)
    vw = min(width, max(20, round(width * 0.62)))
    screen = Rect(round((width - vw) / 2), 0, vw, tier.screen_rows)
    return Plan(tier, screen, tuple(stations), chair, (cx, cy, rx, ry))


def _fits(plan: Plan, width: int, height: int) -> bool:
    boxes = [*plan.stations, plan.chair]
    for i, a in enumerate(boxes):
        if a.x < 0 or a.y < plan.viewscreen.h or a.x + a.w > width or a.y + a.h > height:
            return False
        if any(a.overlaps(b) for b in boxes[i + 1:]):
            return False
    return True




def room_layout(plan: Plan, size: tuple[int, int]) -> room.Layout:
    """A plan for a deck of ``size`` cells, as the room needs it (plain tuples)."""
    def box(r: Rect) -> room.Box:
        return (r.x, r.y, r.w, r.h)

    return room.Layout(size, box(plan.viewscreen), tuple(box(r) for r in plan.stations), plan.tier.art,
                       plan.tier.label_rows, box(plan.chair), plan.tier.chair, plan.arc)


# -- the room: light in glass's backdrop --------------------------------------------------------------

#: How bright a station's pool of light is, by its lamp's role (``dim``: empty, seated, dozing, left).
POOL_STRENGTH = {"working": 0.48, "waiting": 0.56, "error": 0.58, "done": 0.4, "dim": 0.16}


def _px(e: room.Ellipse, g: Grid) -> room.Ellipse:
    """An ellipse in cells, in layer pixels."""
    cx, cy, rx, ry = e
    return (cx * g.sx, cy * g.sy, rx * g.sx, ry * g.sy)


@dataclass(frozen=True)
class Arc(Item):
    """A glowing elliptical arc: the rail in front of the stations (solid, with points of light along it)
    or one of the command well's rings (dashed, fainter). Lit over ``span`` (parametric degrees, 0 to the
    right, 90 up) and fading out at both ends, in a gradient from ``c1`` on the left to ``c2`` on the right,
    with a soft bloom that is gone ``reach`` rows from the line (rule 16)."""

    ellipse: room.Ellipse   # centre x, y and radii, in screen cells
    span: room.Span
    c1: Colour
    c2: Colour
    alpha: float = 0.8      # the line
    glow: float = 0.3       # the bloom
    width: float = 0.16     # the line, in cols (at least 1.2 layer pixels)
    reach: float = 0.5      # the bloom, in rows
    dash: float = 0.0       # dashes this many rows apart; 0: solid
    beads: int = 0          # points of light along it
    fill: float = 0.0       # a faint glow just inside it, fading out ``inside`` rows in
    inside: float = 2.5
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def bbox(self, g: Grid) -> Box:
        xs, ys = room.arc_points(self.ellipse, self.span)
        pad = 1.5 * self.reach * g.sy + 2 + self.margin(g)
        return (float(xs.min()) * g.sx - pad, float(ys.min()) * g.sy - pad,
                float(xs.max()) * g.sx + pad, float(ys.max()) * g.sy + pad)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        if win is None or self.span[1] <= self.span[0]:
            return
        view, xs, ys = win
        e = _px(self.ellipse, g)
        d, theta = room.ellipse_field(xs, ys, e)
        lit = room.along(theta, self.span)
        if self.dash > 0:
            lit = lit * room.dashes(theta, e, self.span, self.dash * g.sy)
        colour = x_gradient(xs, e[0] - e[2], e[0] + e[2], self.c1, self.c2)
        reach = self.reach * g.sy
        c.over(view, colour, room.line(d, max(1.2, self.width * g.sx)) * lit * self.alpha)
        c.glow(view, colour, room.bloom(d, 0.12 * g.sy, reach) * lit * self.glow)
        if self.fill > 0:
            c.glow(view, colour, room.inner(d, self.inside * g.sy) * room.along(theta, self.span) * self.fill)
        if self.beads > 0:
            self._draw_beads(c, view, xs, ys, e, g)

    def _draw_beads(self, c: Canvas, view: np.ndarray, xs: np.ndarray, ys: np.ndarray, e: room.Ellipse,
                    g: Grid) -> None:
        """Points of light evenly along the lit part of the arc, each drawn in a small window of its own."""
        t0, t1 = self.span[0] + room.ARC_FADE, self.span[1] - room.ARC_FADE
        if t1 <= t0:
            return
        radius, reach = max(1.0, 0.16 * g.sx), min(self.reach, 0.4) * g.sy
        mask = c.mask
        try:
            for a in np.radians(np.linspace(t0, t1, self.beads)):
                bx, by = e[0] + e[2] * math.cos(a), e[1] - e[3] * math.sin(a)
                cols = slice(int(np.searchsorted(xs[0], bx - reach - 1)), int(np.searchsorted(xs[0], bx + reach + 1)))
                rows = slice(int(np.searchsorted(ys[:, 0], by - reach - 1)),
                             int(np.searchsorted(ys[:, 0], by + reach + 1)))
                sub = view[rows, cols]
                if not sub.size:
                    continue
                c.mask = None if mask is None else mask[rows, cols]
                core, glow = room.bead(xs[:, cols], ys[rows], bx, by, radius, reach)
                tint = x_gradient(xs[:, cols], e[0] - e[2], e[0] + e[2], self.c1, self.c2)
                c.glow(sub, tint, glow * 0.45)
                c.over(sub, tint * 0.45 + 0.55, core * 0.9)
        finally:
            c.mask = mask


@dataclass(frozen=True)
class Pool(Item):
    """A soft pool of light on the floor (``glow``: centre x, y, half width, reach up and down, in screen
    cells): behind a console in its lamp's colour, or under the chair. Its light is exactly 0 at its edge,
    inside its box (rule 16)."""

    glow: room.Glow
    color: Colour
    strength: float = 0.3
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def px(self, g: Grid) -> room.Glow:
        cx, cy, rx, up, down = self.glow
        return (cx * g.sx, cy * g.sy, rx * g.sx, up * g.sy, down * g.sy)

    def bbox(self, g: Grid) -> Box:
        cx, cy, rx, up, down = self.px(g)
        m = 1 + self.margin(g)
        return (cx - rx - m, cy - up - m, cx + rx + m, cy + down + m)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        c.glow(view, col(self.color), room.pool(xs, ys, self.px(g)) * self.strength)


@dataclass(frozen=True)
class Path(Item):
    """A faint dotted path of light along the floor (``curve``: a cubic Bezier curve in screen cells), from
    a waiting station's console into the well and up to the chair. Static (rule 9): it appears and goes with
    the wait. Its dots fade in from the console and out into the chair, and their glow is gone ``reach`` rows
    out (rule 16)."""

    curve: room.Curve
    color: Colour
    strength: float = 0.5
    spacing: float = 0.42   # rows between dots
    reach: float = 0.26     # each dot's glow, in rows
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def _dots(self, g: Grid) -> list[tuple[float, float, float]]:
        return room.dots(room.moved(self.curve, 0.0, 0.0, g.sx, g.sy), self.spacing * g.sy)

    def bbox(self, g: Grid) -> Box:
        pts = room.curve_points(self.curve)
        pad = self.reach * g.sy + 2 + self.margin(g)
        return (float(pts[:, 0].min()) * g.sx - pad, float(pts[:, 1].min()) * g.sy - pad,
                float(pts[:, 0].max()) * g.sx + pad, float(pts[:, 1].max()) * g.sy + pad)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        colour = col(self.color)
        radius, reach = max(0.8, 0.09 * g.sx), self.reach * g.sy
        mask = c.mask
        try:
            for bx, by, weight in self._dots(g):
                cols = slice(int(np.searchsorted(xs[0], bx - reach - 1)), int(np.searchsorted(xs[0], bx + reach + 1)))
                rows = slice(int(np.searchsorted(ys[:, 0], by - reach - 1)),
                             int(np.searchsorted(ys[:, 0], by + reach + 1)))
                sub = view[rows, cols]
                if not sub.size:
                    continue
                c.mask = None if mask is None else mask[rows, cols]
                core, glow = room.bead(xs[:, cols], ys[rows], bx, by, radius, reach)
                c.glow(sub, colour, glow * 0.22 * weight * self.strength / 0.5)
                c.over(sub, colour * 0.6 + 0.4, core * weight * self.strength)
        finally:
            c.mask = mask


@dataclass(frozen=True)
class Pane(Item):
    """The viewscreen as a glass pane: a luminous rounded frame around the sprite's cells (``rect``, screen
    cells), open at the top so it hangs from the panel's border, with a halo and a faint spill of light under
    it. Nothing is drawn inside the sprite's cells."""

    rect: FRect
    c1: Colour
    c2: Colour
    strength: float = 0.7
    ident: str = ""
    depth: int = 0
    clip: FRect | None = None

    def geometry(self, g: Grid) -> room.PaneGeometry:
        x, y, w, h = self.rect
        return room.PaneGeometry(left=(x - 0.45) * g.sx, right=(x + w + 0.45) * g.sx, top=y * g.sy,
                                 bottom=(y + h + 0.28) * g.sy, radius=0.5 * g.sy, width=max(1.0, 0.13 * g.sx),
                                 reach=0.55 * g.sy, spill=1.1 * g.sy)

    def bbox(self, g: Grid) -> Box:
        p = self.geometry(g)
        m = self.margin(g) + p.width
        return (p.left - p.reach - m, p.top - m, p.right + p.reach + m, p.bottom + max(p.reach, p.spill) + m)

    def draw(self, c: Canvas, g: Grid) -> None:
        win = self.window(c, g)
        if win is None:
            return
        view, xs, ys = win
        p = self.geometry(g)
        edge, halo, spill = room.pane(xs, ys, p)
        colour = x_gradient(xs, p.left, p.right, self.c1, self.c2)
        # the bottom corners a little brighter, as glass's panels have them
        corner = (np.abs(xs - (p.left + p.right) / 2) > (p.right - p.left) / 2 - 2.6 * g.sx) & \
            (ys > p.bottom - 1.1 * g.sy)
        c.over(view, colour, np.clip(edge * self.strength * (1 + 0.3 * corner), 0, 1))
        c.glow(view, colour, halo * 0.16 + spill * 0.05)


def room_items(shape: room.Shape, origin: tuple[float, float], views: Iterable[StationView], waiting: int,
               alert: bool, pal: Palette, key: str = "", clip: FRect | None = None) -> list[Item]:
    """The room's items for a deck at ``origin`` (screen cells): the well's rings, the rail, a pool behind
    each station's console in its lamp's colour (``views``, in station order), a dotted path of light from each
    waiting station to the chair, the chair's pool (amber while anyone waits) and the viewscreen's pane. Red
    alert turns the rail, the rings and the pane red. Each has its own ``ident``, so a state change redraws only
    its own box."""
    ox, oy = origin
    views = list(views)

    def at(e: room.Ellipse) -> room.Ellipse:
        return (e[0] + ox, e[1] + oy, e[2], e[3])

    def lit(p: room.Glow) -> room.Glow:
        return (p[0] + ox, p[1] + oy, p[2], p[3], p[4])

    hot = border_pair(pal.error) if alert else (pal.primary, pal.secondary)
    items: list[Item] = []
    for i, (e, span) in enumerate(shape.rings):
        c1, c2 = (pal.error, pal.error) if alert else (pal.secondary, pal.primary)
        items.append(Arc(at(e), span, c1, c2, alpha=0.34 - 0.08 * i, glow=0.05, width=0.12, reach=0.3,
                         dash=0.75 + 0.35 * i, ident=f"bridge-ring{i}:{key}", depth=1, clip=clip))
    items.append(Arc(at(shape.rail), shape.rail_span, *hot, beads=5, fill=0.05, ident=f"bridge-rail:{key}", depth=1,
                     clip=clip))
    for i, (glow, view) in enumerate(zip(shape.pools, views, strict=False)):
        role = view.lamp if view.lamp in POOL_STRENGTH else "dim"
        items.append(Pool(lit(glow), lamp_color(role, pal), POOL_STRENGTH[role], ident=f"bridge-pool{i}:{key}",
                          depth=1, clip=clip))
    for i, (curve, view) in enumerate(zip(shape.paths, views, strict=False)):
        if view.lamp == "waiting":
            items.append(Path(room.moved(curve, ox, oy), pal.warning, ident=f"bridge-path{i}:{key}", depth=1,
                              clip=clip))
    chair = (pal.warning, 0.45) if waiting else (pal.primary, 0.14)
    items.append(Pool(lit(shape.chair), chair[0], chair[1], ident=f"bridge-chair-pool:{key}", depth=1, clip=clip))
    x, y, w, h = shape.pane
    pane = border_pair(pal.error if alert else pal.primary)
    items.append(Pane((x + ox, y + oy, w, h), *pane, ident=f"bridge-pane:{key}", depth=1, clip=clip))
    return items


class Deck(PixelWidget):
    """The bridge floor: lays out the viewscreen, the stations and the chair for its size, and lights the
    room around them in glass's backdrop."""

    DEFAULT_CSS = """
    Deck { width: 1fr; height: 1fr; }
    Deck > * { position: absolute; }
    """

    def __init__(self, pack: Pack, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.pack = pack
        self.viewscreen = Viewscreen(pack, id="viewscreen")
        self.stations = [Station(pack, i, id=f"station-{i}") for i in range(STATIONS)]
        self.chair = Chair(pack, id="chair")
        self.plan: Plan | None = None
        self.alert = False

    def compose(self) -> ComposeResult:
        yield self.viewscreen
        yield from self.stations
        yield self.chair

    def on_mount(self) -> None:
        self.relayout()

    def on_resize(self, event: Resize) -> None:
        self.relayout()

    def set_alert(self, alert: bool) -> None:
        """Red alert: the viewscreen tints, and the rail, the well's rings and the pane turn red."""
        self.viewscreen.set_alert(alert)
        if alert != self.alert:
            self.alert = alert
            self.refresh()

    def relayout(self) -> None:
        width, height = self.size
        if width <= 0 or height <= 0:
            return
        plan = plan_for(width, height, cell=self.cell_size())
        if plan == self.plan:
            return
        self.plan = plan
        self._place(self.viewscreen, plan.viewscreen)
        cols, rows = plan.tier.art
        for station, rect in zip(self.stations, plan.stations, strict=True):
            station.set_art(cols, rows, plan.tier.label_rows)
            self._place(station, rect)
        self.chair.set_art(*plan.tier.chair)
        self._place(self.chair, plan.chair)
        self.refresh()

    def cell_size(self) -> tuple[float, float] | None:
        """The terminal's cell in pixels while glass draws: the size glass draws sprites for (a font size
        change in Ghostty changes it and resizes the window, so ``on_resize`` lays out again)."""
        glass = getattr(self.app, "glass", None)
        if glass is None or not glass.active:
            return None
        return glass.cell_pixels()

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        """Glass builds its scene at the end of every Textual frame, so a station whose view changed (its
        ``refresh()`` makes a frame) is read here again in that same frame: its pool's colour follows its
        lamp, and only that pool's box is drawn again."""
        plan, region = self.plan, self.region
        if plan is None or not region.width or not region.height:
            return []
        shape = room.shape(room_layout(plan, (region.width, region.height)))
        return room_items(shape, (float(region.x), float(region.y)), (s.view for s in self.stations),
                          self.chair.waiting, self.alert, ctx.palette, key=str(id(self)), clip=self.visible_clip())

    @staticmethod
    def _place(widget: Widget, rect: Rect) -> None:
        widget.styles.offset = (rect.x, rect.y)
        widget.styles.width = rect.w
        widget.styles.height = rect.h
