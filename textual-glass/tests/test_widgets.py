"""The generic widgets (``textual_glass.widgets``): the same layout with and
without glass, the SelectList pill, GlassPanel titles and accents, clipping in
scrolling containers, and the strip helper (rule 15).

Pilot runs headless, so glass is forced on with ``start`` and the engine's
writes are captured by the ``output`` fixture.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Horizontal, VerticalScroll
from textual.content import Content
from textual.geometry import Region
from textual.widget import Widget
from textual.widgets import ContentSwitcher, Static

from textual_glass import Glass, kitty
from textual_glass import _textual_hooks as hooks
from textual_glass import items as R
from textual_glass import widgets as W
from textual_glass.engine import MAIN
from textual_glass.palette import border_pair, rgb
from textual_glass.render import Canvas, FRect, Item, paint, to_u8
from textual_glass.scene import PixelContext
from textual_glass.sprites import beacon_frames
from textual_glass.testing.pilot_helpers import Output, settle, start
from textual_glass.widgets import (
    BLOCKS,
    GlassPanel,
    InfoCard,
    Part,
    PixelWidget,
    ProgressArc,
    RingGauge,
    SelectList,
    SpriteView,
    StatTile,
    StatValue,
    fit,
    placeholder_parts,
    strip,
    text_meter,
)

SIZE = (100, 32)
BEACON = "#ffc24b"
DAYS = ("M", "T", "W", "T", "F", "S", "S")


@dataclass(frozen=True)
class Row:
    key: str
    title: str
    score: int


ROWS = [Row(f"r{i}", f"Row {i} [x]", 10 - i % 10) for i in range(30)]


class Queue(SelectList[Row]):
    """A list with a meter per row: a Signal item with glass, blocks without."""

    def render_row(self, row: Row, line: int, width: int, selected: bool) -> list[Part]:
        meter: Part = ("     ", "") if self.glass_on else (text_meter(row.score / 10, 5), BEACON)
        return fit([(row.title, ""), (" ", ""), meter], width, 0)

    def row_pixels(self, row: Row, rect: FRect, selected: bool, ctx: PixelContext) -> list[Item]:
        x, y, w, _ = rect
        return [R.Signal((x + w - 5, y, 5.0, 1.0), float(row.score), ctx.palette.warning, ident=f"sig:{row.key}")]


class Board(App[None]):
    """Every widget, with data."""

    CSS = """
    #top { height: 9; }
    #mid { height: 1fr; }
    #bottom { height: 9; }
    #top GlassPanel, #mid GlassPanel { width: 1fr; height: 1fr; }
    #top Horizontal { height: 1fr; }
    #beacon { width: 8; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.seen: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            with GlassPanel(title="GAUGES", id="gauges"), Horizontal():
                yield RingGauge("CPU", fraction=0.4, text="40%", id="cpu")
                yield RingGauge("MEM", fraction=0.8, text="80%", sublabel="of 16G", colors=("#ff8a3d", "#ff4d6d"),
                                id="mem")
            with GlassPanel(title="GOALS", id="goals"), Horizontal():
                yield ProgressArc(id="arc")
                yield SpriteView(("beacon", "waiting", BEACON), lambda w, h: beacon_frames(w, h, "waiting", BEACON),
                                 fallback_color=BEACON, id="beacon")
        with Horizontal(id="mid"):
            with GlassPanel(title="QUEUE", id="queue-panel"):
                yield Queue(id="queue")
            with GlassPanel(title="CARD", id="card-panel"):
                yield StatValue(versus="vs last week", id="stat")
                yield InfoCard(id="card")
        with Horizontal(id="bottom"):
            yield StatTile("LANDED", versus="vs last week", id="landed")
            yield StatTile("LATENCY", mode="area", colors=("#6dffa8", "#38e1ff"), id="latency")

    def on_mount(self) -> None:
        self.query_one(Queue).set_rows(ROWS)
        self.query_one(ProgressArc).set(0.62, "Migrate DB", "G-12 · ops")
        self.query_one(StatValue).set("12", 3)
        self.query_one(InfoCard).set_card("Row 0 [x]", [("where", "host-0"), ("score", "10")],
                                          "A body paragraph long enough to wrap across a few lines of the card.")
        self.query_one("#landed", StatTile).set(12, 3, [2, 4, 1, 0, 3, 1, 1], [3, 2, 2, 1, 2, 0, 0], DAYS, today=4)
        self.query_one("#latency", StatTile).set("38ms", -4, [5, 9, 4, 7, 12, 6, 8, 5], better="down")
        self.query_one(Queue).focus()

    def on_select_list_highlighted(self, message: SelectList.Highlighted) -> None:
        self.seen.append(("highlighted", message.row.key))

    def on_select_list_selected(self, message: SelectList.Selected) -> None:
        self.seen.append(("selected", message.row.key))


class GlassBoard(Glass, Board):
    pass


def _widgets(app: App[Any]) -> dict[str, Widget]:
    """The widgets whose text is compared, by name."""
    return {
        "cpu": app.query_one("#cpu"), "mem": app.query_one("#mem"), "arc": app.query_one("#arc"),
        "beacon": app.query_one("#beacon"), "queue": app.query_one("#queue"), "stat": app.query_one("#stat"),
        "card": app.query_one("#card"), "landed": app.query_one("#landed", StatTile).chart,
        "landed-stat": app.query_one("#landed", StatTile).stat, "latency": app.query_one("#latency", StatTile).chart,
    }


def _lines(widget: Widget) -> list[str]:
    strips = [widget.render_line(y) for y in range(widget.size.height)]
    assert all(s.cell_length == widget.size.width for s in strips), type(widget).__name__
    return [s.text for s in strips]


def _scene(app: Any) -> dict[str, Item]:
    layer = app.glass.layers[MAIN]
    return {item.ident: item for item in (*layer.static, *layer.dynamic) if item.ident}


# -- both modes --------------------------------------------------------------------------


async def test_every_widget_renders_in_both_modes_with_identical_layout(output: Output) -> None:
    plain_app = Board()
    async with plain_app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        regions = {w.id: w.region for w in plain_app.screen.walk_children() if w.id}
        plain = {name: _lines(w) for name, w in _widgets(plain_app).items()}
        queue = plain_app.query_one(Queue)
        assert any(seg.style and seg.style.bgcolor for seg in queue.render_line(0)), "the selected row's background"
        assert not any(seg.style and seg.style.bgcolor for seg in queue.render_line(1))
        assert plain_app.query_one("#gauges").styles.border_top[0] == "round", "a plain rounded border"
    assert output.chunks == [], "no graphics without glass"

    app = GlassBoard()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        assert {w.id: w.region for w in app.screen.walk_children() if w.id} == regions, "the same layout"
        glass = {name: _lines(w) for name, w in _widgets(app).items()}
        scene = _scene(app)
        palette = app.glass.palette
        widgets = _widgets(app)

        for name in ("cpu", "mem", "arc"):  # the value text is the same; the blocks become a ring
            assert any("▰" in line for line in plain[name]) and not any("▰" in line for line in glass[name]), name
            assert [line for line in plain[name] if "%" in line] == [line for line in glass[name] if "%" in line]
        cpu, mem, arc = widgets["cpu"], widgets["mem"], widgets["arc"]
        ring = scene[f"ring:{id(cpu)}"]
        assert isinstance(ring, R.Ring) and ring.value == 0.4
        r = cpu.content_region
        assert ring.rect[:3] == (r.x, r.y, r.width) and ring.rect[3] % 2 == 1 and ring.rect[3] < r.height, \
            "an odd number of rows, the label underneath"
        assert (ring.c1, ring.c2) == border_pair(palette.primary), "colours from the theme by default"
        mem_ring = scene[f"ring:{id(mem)}"]
        assert isinstance(mem_ring, R.Ring) and (mem_ring.c1, mem_ring.c2) == ("#ff8a3d", "#ff4d6d")
        assert "of 16G" in "".join(glass["mem"]) and "MEM" in "".join(glass["mem"])
        arc_ring = scene[f"arc:{id(arc)}"]
        assert isinstance(arc_ring, R.Ring) and arc_ring.sweep == 1.0 and arc_ring.value == 0.62
        assert "Migrate DB" in "".join(glass["arc"]) and "G-12 · ops" in "".join(glass["arc"])

        for name, item_type in (("landed", R.Bars), ("latency", R.Area)):
            assert any(ch in "".join(plain[name]) for ch in BLOCKS[1:]), f"{name}: eighth blocks without glass"
            assert not any(ch in "".join(glass[name]) for ch in BLOCKS[1:]), name
            chart = scene[f"{'bars' if item_type is R.Bars else 'area'}:{id(widgets[name])}"]
            assert isinstance(chart, item_type)
        bars = scene[f"bars:{id(widgets['landed'])}"]
        assert isinstance(bars, R.Bars) and bars.ghost == (3, 2, 2, 1, 2, 0, 0) and bars.today == 4
        assert "░" in "".join(plain["landed"]), "the ghost series without glass"
        assert plain["landed"][-1] == glass["landed"][-1] and "M" in glass["landed"][-1], "the labels row"

        assert "◉" in "".join(plain["beacon"]) and kitty.PLACEHOLDER not in "".join(plain["beacon"])
        assert kitty.PLACEHOLDER in "".join(glass["beacon"]), "the sprite's placeholder cells"

        for name in ("stat", "card", "landed-stat"):
            assert plain[name] == glass[name], f"{name}: text only, the same in both modes"
        assert "▲3 vs last week" in glass["stat"][0]
        assert "Row 0 [x]" in glass["card"][0] and any("WHERE" in line for line in glass["card"])

        queue = app.query_one(Queue)
        assert [line.split()[:3] for line in plain["queue"]] == [line.split()[:3] for line in glass["queue"]]
        assert any("▰" in line for line in plain["queue"]) and not any("▰" in line for line in glass["queue"])
        assert not any(seg.style and seg.style.bgcolor for seg in queue.render_line(0)), "the pill shows it instead"
        pill = scene[f"pill:{id(queue)}"]
        assert isinstance(pill, R.Pill) and pill.rect[1] == queue.content_region.y and pill.focused
        assert isinstance(scene["sig:r0"], R.Signal), "row pixels"
        assert f"sig:r{queue.visible_rows}" not in scene, "only rows on screen"

        for panel in app.query(GlassPanel):
            assert isinstance(scene[f"panel:{id(panel)}"], R.Panel), "the CSS scene draws every GlassPanel"
        assert output.model().errors == []


async def test_widgets_follow_the_theme(output: Output) -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        cpu = app.query_one("#cpu", RingGauge)

        def value_colour() -> str:
            line = cpu.render_line(cpu._ring_rows() // 2)
            return next(str(seg.style.color.name) for seg in line if seg.text.strip() and seg.style and seg.style.color)

        before = value_colour()
        app.theme = "solarized-light"
        await pilot.pause()
        assert value_colour() != before and cpu.theme_palette.dark is False


class Charts(App[None]):
    CSS = "Telemetry { height: 4; }"

    def compose(self) -> ComposeResult:
        yield W.Telemetry(id="bars")
        yield W.Telemetry(mode="area", id="area")


class GlassCharts(Glass, Charts):
    pass


@pytest.mark.parametrize("app_class", [Charts, GlassCharts])
async def test_telemetry_has_one_scale_in_both_modes_with_and_without_glass(output: Output,
                                                                          app_class: type[Charts]) -> None:
    """``scale_max``, or else max(1, the largest value or ghost): bars and area, pixels and blocks alike."""
    app = app_class()
    async with app.run_test(size=(40, 10)) as pilot:
        if isinstance(app, Glass):
            await start(app, pilot)
        charts = [app.query_one("#bars", W.Telemetry), app.query_one("#area", W.Telemetry)]
        for chart in charts:
            chart.set_series([0.2, 0.5], ghost=[0.4])
            assert chart.scale == 1.0, "values under 1: 1 fills the height"
            chart.set_series([2, 4], ghost=[8])
            assert chart.scale == 8.0, "the largest value or ghost"
            chart.set_series([2, 4], scale_max=16)
            assert chart.scale == 16.0
            with pytest.raises(ValueError):
                chart.set_series([2, 4], scale_max=0)
        bars, area = charts
        if isinstance(app, Glass):
            await settle(app, pilot)
            scene = _scene(app)
            bars_item, area_item = scene[f"bars:{id(bars)}"], scene[f"area:{id(area)}"]
            assert isinstance(bars_item, R.Bars) and bars_item.scale_max == 16.0
            assert isinstance(area_item, R.Area) and area_item.values == (2 / 16, 4 / 16)
        else:
            await pilot.pause()
            for chart in charts:  # 4 of 16 fills a quarter of the 4 rows: the bottom row and nothing above
                lines = _lines(chart)
                assert "█" in lines[3] and not any(ch in lines[2] for ch in BLOCKS[1:]), chart.mode
    tile = W.StatTile("X")
    tile.set(4, None, [2, 4], scale_max=16)
    assert tile.chart.scale == 16.0, "StatTile passes scale_max through"


async def test_idle_board_sends_nothing(output: Output) -> None:
    app = GlassBoard()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        mark = output.mark()
        for _ in range(5):
            app.glass.on_frame()
            app.glass.tick()
            await pilot.pause()
        for widget in app.query(PixelWidget):
            widget.refresh()  # Textual frames where nothing pixel-related changed
        await settle(app, pilot)
        assert output.since(mark) == ""
        assert not app.glass.last_scene.busy


# -- rule 16: every item the widgets yield ends inside its box -------------------------------


async def test_widget_items_draw_nothing_at_the_edge_of_their_box(output: Output) -> None:
    app = GlassBoard()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        grid = app.glass.grid
        items = [i for name, i in _scene(app).items() if re.match(r"(ring|arc|bars|area|pill|sig):", name)]
    assert {type(i) for i in items} == {R.Ring, R.Bars, R.Area, R.Pill, R.Signal}
    for item in items:
        x0, y0, x1, y1 = item.bbox(grid)
        ox, oy = math.floor(x0) - 2, math.floor(y0) - 2
        w, h = math.ceil(x1) - ox + 2, math.ceil(y1) - oy + 2
        grey = np.full((h, w, 3), 0.25, np.float32)
        data = grey.copy()
        paint(Canvas(data, ox, oy), grid, [item])
        overlay = np.zeros((h, w, 4), np.float32)
        paint(Canvas(overlay, ox, oy), grid, [item])
        bx0, by0 = math.floor(x0) - ox, math.floor(y0) - oy
        bx1, by1 = math.ceil(x1) - 1 - ox, math.ceil(y1) - 1 - oy
        for img, base in ((to_u8(data).astype(int), to_u8(grey).astype(int)), (to_u8(overlay)[..., 3:], 0)):
            change = np.abs(img - base)
            ring = np.concatenate([change[by0, bx0:bx1 + 1], change[by1, bx0:bx1 + 1],
                                   change[by0:by1 + 1, bx0], change[by0:by1 + 1, bx1]])
            assert ring.max() <= 1, (item.ident, int(ring.max()))
            assert change[:by0].max(initial=0) == 0 and change[by1 + 1:].max(initial=0) == 0, item.ident


# -- SelectList ---------------------------------------------------------------------------


def _pill(app: Any, widget: Widget) -> R.Pill:
    pill = _scene(app)[f"pill:{id(widget)}"]
    assert isinstance(pill, R.Pill)
    return pill


async def test_select_list_pill_slides_settles_and_goes_quiet(output: Output) -> None:
    app = GlassBoard()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        queue = app.query_one(Queue)
        queue._pill.duration = 0.5  # slow enough to catch it mid-slide
        y0 = queue.content_region.y
        await pilot.press("down")
        app.glass.tick()
        assert y0 < _pill(app, queue).rect[1] < y0 + 1, "between the rows"
        assert app.glass.last_scene.busy, "the engine keeps ticking while it slides"
        for _ in range(80):
            app.glass.tick()
            if not app.glass.last_scene.busy:
                break
            await asyncio.sleep(0.016)
        await settle(app, pilot)
        assert _pill(app, queue).rect[1] == y0 + 1 and not app.glass.last_scene.busy, "settled on the row"
        assert app.seen[-1] == ("highlighted", "r1")
        mark = output.mark()
        for _ in range(5):
            app.glass.on_frame()
            app.glass.tick()
            await pilot.pause()
        queue.refresh()
        await settle(app, pilot)
        assert output.since(mark) == "", "silent once it has settled"

        await pilot.press("enter")
        await pilot.pause()
        assert app.seen[-1] == ("selected", "r1")
        await pilot.press("pagedown")
        app.glass.tick()
        assert queue.index == 1 + queue.visible_rows and queue.scroll > 0
        assert not app.glass.last_scene.busy, "a page jumps"
        assert _pill(app, queue).rect[1] == y0 + queue.index - queue.scroll
        rows = {name for name in _scene(app) if name.startswith("sig:")}
        assert rows == {f"sig:r{i}" for i in range(queue.scroll, queue.scroll + queue.visible_rows)}
        await pilot.press("end")
        assert queue.highlighted == ROWS[-1]
        assert output.model().errors == []


async def test_select_list_pill_jumps_at_minimal_motion(output: Output) -> None:
    app = GlassBoard(glass_motion="minimal")
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        queue = app.query_one(Queue)
        await pilot.press("down")
        app.glass.tick()
        assert _pill(app, queue).rect[1] == queue.content_region.y + 1 and not app.glass.last_scene.busy


async def test_select_list_keeps_the_row_when_rows_change() -> None:
    app = Board()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        queue = app.query_one(Queue)
        queue.select_key("r5")
        queue.set_rows(ROWS[3:])
        assert queue.highlighted == ROWS[5] and queue.index == 2
        queue.set_rows([])
        await pilot.pause()
        assert queue.highlighted is None and "nothing here" in queue.render_line(0).text


@dataclass(frozen=True)
class Header:
    key: str
    title: str


#: Two sections: 0 FIRST, 1-15 r0-r14, 16 SECOND, 17-31 r15-r29.
SECTIONS: list[Any] = [Header("first", "FIRST"), *ROWS[:15], Header("second", "SECOND"), *ROWS[15:]]


class Sections(SelectList[Any]):
    """Rows under section headers, which the cursor skips."""

    def selectable(self, row: Any) -> bool:
        return isinstance(row, Row)

    def render_row(self, row: Any, line: int, width: int, selected: bool) -> list[Part]:
        return [(row.title, "")]


class Pages(App[None]):
    """A sectioned list on a page that isn't shown yet."""

    CSS = "Sections { height: 8; }"

    def compose(self) -> ComposeResult:
        with ContentSwitcher(initial="first"):
            yield Static("the first page", id="first")
            yield Sections(id="sections")

    def on_mount(self) -> None:
        self.query_one(Sections).set_rows(SECTIONS)


async def test_select_list_keeps_section_headers_in_view() -> None:
    """A header can't take the cursor, so scrolling up to a row brings the
    headers right above it along. Rows set while the list had no size yet (on
    a page not shown) used to stay scrolled past their first header, and Home
    in a long list never brought that header back."""
    app = Pages()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        listing = app.query_one(Sections)
        switcher = app.query_one(ContentSwitcher)
        assert listing.size.height == 0 and listing.highlighted == ROWS[0], "filled while hidden"

        def top() -> str:
            return listing.render_line(0).text.strip()

        switcher.current = "sections"
        await pilot.pause()
        assert listing.size.height == 8
        assert (listing.scroll, top()) == (0, "FIRST"), "the first header shows once the page does"
        listing.action_last()
        assert listing.scroll == len(SECTIONS) - 8
        listing.action_first()
        assert (listing.highlighted, listing.scroll, top()) == (ROWS[0], 0, "FIRST"), "Home brings it back"
        listing.action_last()
        listing.select_key("r15")
        assert (listing.index, listing.scroll, top()) == (17, 16, "SECOND"), "a header in the middle too"

        listing.select_key("r20")
        assert listing.scroll == 16
        switcher.current = "first"
        await pilot.pause()
        listing.set_rows(SECTIONS)  # new data while the page is hidden
        switcher.current = "sections"
        await pilot.pause()
        assert (listing.highlighted, listing.scroll) == (ROWS[20], 16), "the page comes back as it was"


# -- GlassPanel ---------------------------------------------------------------------------


class Titles(App[None]):
    CSS = """
    #panel { width: 30; height: 5; }
    #heavy { width: 30; height: 4; border: heavy $primary; }
    """

    def compose(self) -> ComposeResult:
        with GlassPanel(title="Tickets [urgent]", id="panel"):
            yield Static("body")
        with GlassPanel(title="Heavy", accent="#ff0000", id="heavy"):
            yield Static("body")


class GlassTitles(Glass, Titles):
    pass


async def test_glass_panel_title_fits_extras_escapes_markup_and_notches(output: Output) -> None:
    app = GlassTitles()
    async with app.run_test(size=(80, 12)) as pilot:
        await start(app, pilot)
        panel = app.query_one("#panel", GlassPanel)
        panel.set_title("Tickets [urgent]", ("12", "bold #ff8a3d"), ("and a long extra", "#8a93a6"))
        await settle(app, pilot)
        title = Content.from_markup(str(panel.border_title))
        assert title.plain == "Tickets [urgent]  12", "the brackets survive; the extra that doesn't fit is dropped"
        assert [(s.start, s.end, str(s.style)) for s in title.spans] == [(18, 20, "bold #ff8a3d")]
        assert "Tickets [urgent]  12" in panel.render_lines(panel.region.reset_offset)[0].text
        notch = _scene(app)[f"panel:{id(panel)}"]
        assert isinstance(notch, R.Panel)
        x = panel.region.x
        assert notch.titles == ((x + 2, x + 2 + len(title.plain) + 2),), "the title's notch in the glass"

        panel.styles.width = 60
        await settle(app, pilot)
        assert Content.from_markup(str(panel.border_title)).plain == "Tickets [urgent]  12  and a long extra"
        assert _scene(app)[f"panel:{id(panel)}"].titles == ((x + 2, x + 2 + 38 + 2),)  # type: ignore[attr-defined]


async def test_glass_panel_accent_colours_reach_the_glass(output: Output) -> None:
    app = GlassTitles()
    async with app.run_test(size=(80, 12)) as pilot:
        await start(app, pilot)
        panel = app.query_one("#panel", GlassPanel)
        palette = app.glass.palette

        def ends() -> tuple[Any, Any]:
            item = _scene(app)[f"panel:{id(panel)}"]
            assert isinstance(item, R.Panel)
            return item.c1, item.c2

        assert ends() == border_pair(palette.primary), "the CSS $primary border by default"
        panel.set_accent("#ff0000", "#0000ff")
        await settle(app, pilot)
        assert ends() == ((255, 0, 0), (0, 0, 255)), "a gradient from accent to accent_2"
        assert panel.styles.border_left == ("round", Color(255, 0, 0))
        assert panel.styles.border_right == ("round", Color(0, 0, 255))
        panel.set_accent((0, 255, 0))
        await settle(app, pilot)
        assert ends() == border_pair((0, 255, 0)), "one colour: glass shades it"
        panel.set_accent(None)
        await settle(app, pilot)
        assert ends() == border_pair(palette.primary), "back to the CSS"
        heavy = app.query_one("#heavy", GlassPanel)
        assert heavy.styles.border_top == ("heavy", Color(255, 0, 0)), "an accent given early keeps the CSS border type"
        heavy_item = _scene(app)[f"panel:{id(heavy)}"]
        assert isinstance(heavy_item, R.Panel) and heavy_item.edges[0] == R.HEAVY
        assert output.model().errors == []


def test_glass_panel_dialog_class() -> None:
    assert GlassPanel(dialog=True).has_class("-dialog")
    assert not GlassPanel().has_class("-dialog")
    assert rgb("#38e1ff") == (56, 225, 255)


# -- clipping ----------------------------------------------------------------------------


class Scroller(App[None]):
    CSS = """
    #scroll { height: 10; }
    RingGauge { height: 7; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="scroll"):
            for i in range(4):
                yield RingGauge("G", fraction=0.5, text="50%", id=f"g{i}")


class GlassScroller(Glass, Scroller):
    pass


async def test_widgets_in_a_scrolling_container_clip_their_pixels(output: Output) -> None:
    app = GlassScroller()
    async with app.run_test(size=(40, 14)) as pilot:
        await start(app, pilot)
        view = app.query_one("#scroll").content_region

        def ring(name: str) -> Any:
            return _scene(app).get(f"ring:{id(app.query_one(name))}")

        def visible(name: str) -> FRect:
            v = app.query_one(name).region.intersection(view)
            return (float(v.x), float(v.y), float(v.width), float(v.height))

        assert ring("#g0").clip is None, "all of it shows"
        assert ring("#g1").clip == visible("#g1"), "cut where its text is cut"
        assert ring("#g2") is None, "off screen: not collected"
        app.query_one("#scroll").scroll_to(y=7, animate=False)  # g1 now shows whole, g2 is cut
        await settle(app, pilot)
        assert ring("#g0") is None and ring("#g1").clip is None and ring("#g2").clip == visible("#g2")
        assert output.model().errors == []
    detached: Any = SimpleNamespace(screen=SimpleNamespace(), region=Region(1, 2, 3, 4))
    assert hooks.visible_region(detached) == Region(1, 2, 3, 4), "fails soft (rule 7)"


# -- strips (rule 15) ----------------------------------------------------------------------


@pytest.mark.parametrize("width", range(14))
def test_strip_never_crops_a_placeholder_run(width: int) -> None:
    """A placeholder run is one cell per ``U+10EEEE`` plus two zero-width
    diacritics. ``strip`` counts placeholders to measure it and draws a run
    whole or not at all: a cropped run would show the wrong columns."""
    image_id = 0x123456
    for run in (1, 3, 5):
        for before in ("", "ab", "abcdefg"):
            line = strip([(before, "#ffffff"), placeholder_parts(image_id, 2, run), ("tail", "#c0c0c0")], width)
            assert line.cell_length == width
            cells, runs = 0, 0
            for seg in line:
                if seg.text.startswith(kitty.PLACEHOLDER):
                    assert seg.text == kitty.placeholder_row(2, run), "whole runs only"
                    assert seg.style is not None and seg.style.color is not None
                    assert seg.style.color.triplet is not None and seg.style.color.triplet.hex == "#123456"
                    cells += run
                    runs += 1
                else:
                    cells += len(seg.text)
            assert cells == width
            assert runs == (1 if len(before[:width]) + run <= width else 0)


def test_fit_and_text_helpers() -> None:
    parts = fit([("> ", ""), ("a title that is long", "bold"), (" 5m", "")], 14, 1)
    assert "".join(t for t, _ in parts) == "> a title … 5m" and len("".join(t for t, _ in parts)) == 14
    assert "".join(t for t, _ in fit([("x", ""), ("y", "")], 6, 0)) == "x    y"
    assert text_meter(0.5, 4) == "▰▰▱▱" and text_meter(2.0, 3) == "▰▰▰"
    assert W.ellipsize("abc", 2) == "a…" and W.ellipsize("abc", 0) == ""
    line = strip([("abc", "bold")], 5, bg="#102030")
    assert all(seg.style is not None and seg.style.bgcolor is not None for seg in line), "the background fills the line"


def test_tween_eases_and_settles() -> None:
    tween = W.Tween(0.0, duration=0.2)
    tween.set(1.0, now=10.0)
    assert tween.busy(10.1) and 0.0 < tween.value(10.1) < 1.0
    assert not tween.busy(10.25) and tween.value(10.25) == 1.0
    tween.set(3.0, animate=False)
    assert tween.value(0.0) == 3.0 and not tween.busy(0.0)


# -- rule 14 ------------------------------------------------------------------------------


def test_widgets_add_no_attribute_textual_has() -> None:
    """Rule 14: a widget attribute that Textual already has (``Widget.colors``
    is a read-only property) breaks Textual. Only its public hooks are overridden."""
    overrides = {"DEFAULT_CSS", "BINDINGS", "can_focus", "render_line", "on_resize", "on_click", "on_focus",
                 "on_blur", "on_mount"}
    dunder = re.compile(r"^__.*__$")
    textual_made = set(vars(type("Plain", (Widget,), {})))
    classes = [W.PixelWidget, W.SelectList, W.RingGauge, W.Telemetry, W.StatValue, W.StatTile, W.ProgressArc,
               W.SpriteView, W.InfoCard, W.GlassPanel]
    for cls in classes:
        added = {n for n in set(vars(cls)) - textual_made - overrides if not dunder.match(n)}
        clashes = {n for n in added if hasattr(Widget, n)}
        assert not clashes, (cls.__name__, clashes)
    instances: list[Widget] = [
        W.SelectList(), W.RingGauge("x"), W.Telemetry(), W.StatValue(), W.StatTile("x"), W.ProgressArc(),
        W.SpriteView(("k",), lambda w, h: ([], 0)), W.InfoCard(), W.GlassPanel(),
    ]
    plain = set(vars(Widget()))
    for widget in instances:
        clashes = {n for n in set(vars(widget)) - plain if hasattr(Widget, n)}
        assert not clashes, (type(widget).__name__, clashes)
