"""Textual pilot tests: the scene read from computed styles, the line filter, the
engine's output (fed through the kitty model), live toggling and motion levels.

Pilot runs headless, so glass is forced on with ``activate(force=True)`` and the
engine's writes are captured instead of going to a terminal.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import numpy as np
import pytest
from textual.geometry import Region
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, DataTable
from textual.widgets._footer import FooterKey

from textual_glass import items as R
from textual_glass import kitty
from textual_glass.detection import Support
from textual_glass.engine import MAIN, Z0
from textual_glass.filter import NO_RULES, GlassFilter
from textual_glass.palette import to_rgb
from textual_glass.render import Canvas, paint, to_u8
from textual_glass.testing.pilot_helpers import Output, idle, settle, start
from textual_glass.widgets import GlassImage, PixelWidget, RingGauge, SpriteView

from sample_app import BADGE, Dialog, GlassSample, Sample

SIZE = (100, 32)


# -- off by default -----------------------------------------------------------------


async def test_unsupported_terminal_changes_nothing(output: Output) -> None:
    from textual import _border

    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert not app.glass.active
        assert not app.has_class("-textual-glass")
        assert _border.BORDER_CHARS["round"][0][0] == "╭"
        table = app.query_one(DataTable)
        assert not any(isinstance(f, GlassFilter) for f in table.get_line_filters())
        assert app.screen.styles.background.a == 1
    assert output.chunks == []


async def test_plain_app_and_glass_app_lay_out_identically() -> None:
    async def regions(app_class):  # type: ignore[no-untyped-def]
        app = app_class()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            return [(type(w).__name__, w.region) for w in app.screen.walk_children()]

    assert await regions(Sample) == await regions(GlassSample)


# -- the scene ----------------------------------------------------------------------


async def test_scene_follows_computed_styles(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        variables = app.theme_variables
        scene = app.glass.last_scene
        panels = {p.rect: p for p in scene.levels[0].static if isinstance(p, R.Panel)}
        table = app.query_one(DataTable)
        card2 = app.query_one("#card2")
        t = table.region
        tp = panels[(t.x, t.y, t.width, t.height)]
        # #table:focus { border: round $accent } -> the panel uses the accent colour
        assert tp.c1 == to_rgb(variables["accent"]) and tp.focused
        c = card2.region
        assert panels[(c.x, c.y, c.width, c.height)].c1 == to_rgb(variables["warning"])
        # the title notch sits where Textual draws the title
        (x0, x1), = tp.titles
        assert x0 == t.x + 2 and x1 == t.x + 2 + len(" Needs Attention ")
        # blur the table: its border falls back to $primary and it loses focus glow
        app.query_one(Button).focus()
        await settle(app, pilot)
        panels = {p.rect: p for p in app.glass.last_scene.levels[0].static if isinstance(p, R.Panel)}
        tp = panels[(t.x, t.y, t.width, t.height)]
        assert tp.c1 == to_rgb(variables["primary"]) and not tp.focused


async def test_table_header_zebra_and_pill_track_the_cursor(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        table = app.query_one(DataTable)
        content = table.content_region

        def items():  # type: ignore[no-untyped-def]
            level = app.glass.last_scene.levels[0]
            return level.static + level.dynamic

        header = [i for i in items() if isinstance(i, R.HeaderStrip)]
        assert header and header[0].rect == (content.x, content.y, content.width, 1)
        pill = next(i for i in items() if isinstance(i, R.Pill))
        assert pill.rect[1] == content.y + 1  # first data row, under the header
        zebra = next(i for i in items() if isinstance(i, R.Rows))
        assert zebra.rows[0][0] == content.y + 1 and zebra.rows[1][0] == content.y + 3
        await pilot.press("down", "down", "down")
        app.glass.set_motion("minimal")  # no slide: the pill jumps straight there
        await settle(app, pilot)
        pill = next(i for i in items() if isinstance(i, R.Pill))
        assert pill.rect[1] == content.y + 4
        # scroll far down: the pill follows the cursor row on screen
        await pilot.press("end")
        await settle(app, pilot)
        pill = next(i for i in items() if isinstance(i, R.Pill))
        row_y = content.y + 1 + table.cursor_row - int(table.scroll_y)
        assert pill.rect[1] == row_y and content.y < row_y < content.bottom


async def test_pill_slides_in_subtle_motion(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        assert app.glass.motion == "subtle"
        table = app.query_one(DataTable)
        y0 = table.content_region.y + 1
        await pilot.press("down", "down")
        await pilot.pause()
        app.glass.on_frame()  # the slide starts here
        await asyncio.sleep(0.05)
        app.glass.tick()
        pill = next(i for i in app.glass.last_scene.levels[0].dynamic if isinstance(i, R.Pill))
        assert y0 < pill.rect[1] < y0 + 2  # part way there
        await asyncio.sleep(0.25)
        app.glass.tick()
        pill = next(i for i in app.glass.last_scene.levels[0].dynamic if isinstance(i, R.Pill))
        assert pill.rect[1] == y0 + 2


async def test_footer_progress_and_scrollbar_chrome(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        level = app.glass.last_scene.levels[0]
        badges = [i for i in level.static if isinstance(i, R.Badge)]
        assert len(badges) == len(app.query(FooterKey))
        progress = next(i for i in level.dynamic if isinstance(i, R.Progress))
        assert progress.fraction == pytest.approx(0.6)
        thumbs = [i for i in level.dynamic if isinstance(i, R.Thumb)]
        assert thumbs and thumbs[0].vertical


# -- the line filter -------------------------------------------------------------------


def backgrounds(widget) -> list[list[str | None]]:  # type: ignore[no-untyped-def]
    strips = widget.render_lines(Region(0, 0, widget.size.width, widget.size.height))
    out = []
    for strip in strips:
        row = []
        for segment in strip:
            bg = segment.style.bgcolor if segment.style else None
            row.append(None if bg is None else ("default" if bg.is_default else bg.name))
        out.append(row)
    return out


async def test_filter_reveals_glass_but_keeps_explicit_backgrounds(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        table = app.query_one(DataTable)
        rows = backgrounds(table)
        flat = {bg for row in rows for bg in row}
        assert "default" in flat
        assert any(bg is not None and bg.lower() == BADGE for bg in flat)  # the score badge keeps its colour
        explicit = {bg for bg in flat if bg not in (None, "default") and bg.lower() != BADGE}
        assert not explicit, explicit  # header, zebra, cursor and surface colours are all gone
        # border glyphs are blank, the title is still text
        top = "".join(seg.text for seg in table.render_lines(Region(0, 0, table.size.width, 1))[0])
        assert "╭" not in top and "─" not in top and "Needs Attention" in top
        # a Button is skipped: its cells keep their colours
        assert app.glass.rules(app.query_one(Button)) is NO_RULES


async def test_modal_is_transparent_and_dimmed(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        await pilot.press("d")
        await settle(app, pilot)
        screen = app.screen
        assert screen.styles.background.a == 0  # no colour tint: glass shows through
        rules = app.glass.rules(screen)
        assert rules.dim is not None
        assert {k for k in app.glass.layers} >= {(0, "main"), (1, "main")}
        assert 1 in app.glass.effects.scrims
        model = output.model()
        z = sorted(p["z"] for p in model.placements.values() if not p["virtual"])
        assert all(v < kitty.Z_BELOW_BACKGROUND for v in z), z
        image = app.screen.query_one(GlassImage)
        assert image.glass_on and image._image_id in model.images
        line = image.render_line(0)
        assert kitty.PLACEHOLDER in line.text
        await pilot.press("escape")
        await settle(app, pilot)
        await asyncio.sleep(0.25)
        app.glass.tick()
        assert (1, "main") not in app.glass.layers and not app.glass.effects.scrims


# -- engine output ---------------------------------------------------------------------------


async def test_output_is_valid_and_uses_partial_edits(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        uploads = output.model().counts.get("t", 0)
        await pilot.press("down")
        await settle(app, pilot)
        await asyncio.sleep(0.2)
        app.glass.tick()
        model = output.model()
        assert not model.errors, model.errors
        assert model.no_quiet == 0 and model.queries == 0
        assert model.counts.get("t", 0) == uploads  # moving the cursor re-uploads nothing
        assert model.edits >= 1
        backdrop = [p for p in model.placements.values() if p["z"] == -2_000_000_000]
        assert len(backdrop) == 1 and (backdrop[0]["c"], backdrop[0]["r"]) == SIZE


async def test_idle_app_sends_nothing(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        await asyncio.sleep(0.3)
        app.glass.tick()
        before = len(output.text)
        for _ in range(5):
            await pilot.pause()
            app.glass.on_frame()
        assert len(output.text) == before


async def test_live_toggle_restores_plain_textual(output: Output) -> None:
    from textual import _border

    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        assert _border.BORDER_CHARS["round"][0][0] == " "
        app.action_glass_toggle()
        await pilot.pause()
        assert not app.glass.active and not app.has_class("-textual-glass")
        assert _border.BORDER_CHARS["round"][0][0] == "╭"
        model = output.model()
        assert not model.images and not model.placements  # everything deleted
        assert kitty.RESET_DEFAULT_BACKGROUND in output.text
        table = app.query_one(DataTable)
        assert not any(isinstance(f, GlassFilter) for f in table.get_line_filters())
        app.glass.activate(force=True)  # the action can't re-enable under pilot's headless driver
        await settle(app, pilot)
        assert app.glass.active and output.model().placements


async def test_motion_levels(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        app.action_glass_motion("full")
        await settle(app, pilot)
        amb = app.glass.effects.ambient
        assert amb.shimmer and amb.glints and amb.halo_shown is not None
        model = output.model()
        assert model.images[amb.shimmer]["state"] == 3  # the terminal loops it
        assert not model.errors, model.errors
        app.action_glass_motion("minimal")
        await settle(app, pilot)
        assert app.glass.effects.ambient.shimmer is None
        assert amb.shimmer not in output.model().images
        app.action_glass_motion()  # cycles
        assert app.glass.motion == "subtle"


async def test_system_commands() -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        titles = [c.title for c in app.get_system_commands(app.screen)]
        assert not any(t.startswith("Glass") for t in titles)  # unsupported: nothing to offer
        app.glass.support = Support(True, "test")
        titles = [c.title for c in app.get_system_commands(app.screen)]
        assert "Glass: on" in titles
        app.glass.activate(force=True)
        titles = [c.title for c in app.get_system_commands(app.screen)]
        assert {"Glass: motion minimal", "Glass: motion subtle", "Glass: motion full", "Glass: off"} <= set(titles)


async def test_glass_image_falls_back_to_half_blocks() -> None:
    app = Sample()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("d")
        await pilot.pause()
        image = app.screen.query_one(GlassImage)
        assert image.size == (16, 8)
        text = "".join(image.render_line(y).text for y in range(8))
        assert "▀" in text and kitty.PLACEHOLDER not in text


async def test_theme_change_recolours_the_glass(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        await pilot.press("d")
        await settle(app, pilot)
        await asyncio.sleep(0.25)
        app.glass.tick()
        before = app.glass.palette
        scrim = app.glass.effects.scrims[1]
        assert scrim.shown == scrim.frames
        app.theme = "solarized-light"
        await settle(app, pilot)
        after = app.glass.palette
        assert after != before and not after.dark
        assert output.text.count("\x1b]11;#") >= 2  # the window padding follows the theme
        new_scrim = app.glass.effects.scrims[1]
        assert new_scrim.image_id != scrim.image_id and new_scrim.shown == new_scrim.frames  # no fade-in flicker
        model = output.model()
        assert not model.errors, model.errors
        assert scrim.image_id not in model.images


async def test_scrolling_moves_the_pill_with_the_rows(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        table = app.query_one(DataTable)
        await pilot.press("down", "down", "down", "down", "down")
        await settle(app, pilot)
        await asyncio.sleep(0.2)
        app.glass.tick()
        before = next(i for i in app.glass.last_scene.levels[0].dynamic if isinstance(i, R.Pill)).rect[1]
        table.scroll_to(y=3, animate=False)  # the wheel, not the cursor
        await pilot.pause()
        app.glass.on_frame()
        after = next(i for i in app.glass.last_scene.levels[0].dynamic if isinstance(i, R.Pill)).rect[1]
        assert after == before - 3  # no slide: it moved with the rows in the same frame


async def test_animated_image_is_played_by_the_terminal(output: Output) -> None:
    from textual.app import App, ComposeResult

    from textual_glass import Glass

    from sample_app import checker

    frames = [bytes(reversed(checker())), checker()]

    class Animated(Glass, App):
        def compose(self) -> ComposeResult:
            yield GlassImage(checker(), 16, 16, frames=frames, gap_ms=120)

    app = Animated()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        image = app.query_one(GlassImage)
        await settle(app, pilot)
        model = output.model()
        uploaded = model.images[image._image_id]
        assert len(uploaded["frames"]) == 4 and uploaded["state"] == 3  # 3 frames, then the dimmed copy
        assert uploaded["gaps"][-1] < 0 and min(uploaded["gaps"][:3]) > 0, "playback skips the dimmed copy"
        assert not model.errors, model.errors
        virtual = [p for (i, _), p in model.placements.items() if i == image._image_id and p["virtual"]]
        assert virtual and (virtual[0]["c"], virtual[0]["r"]) == (16, 8)


@pytest.mark.parametrize("kwargs, env, expected", [
    ({}, {}, "subtle"),
    ({"glass_motion": "full"}, {"TEXTUAL_GLASS_MOTION": "minimal"}, "full"),
    ({}, {"TEXTUAL_GLASS_MOTION": "full"}, "full"),
    ({}, {"TEXTUAL_ANIMATIONS": "none"}, "minimal"),
    ({}, {"TEXTUAL_GLASS_MOTION": "bogus"}, "subtle"),
])
def test_motion_precedence(monkeypatch: pytest.MonkeyPatch, kwargs: dict, env: dict, expected: str) -> None:
    from textual import constants

    monkeypatch.delenv("TEXTUAL_GLASS_MOTION", raising=False)
    monkeypatch.setattr(constants, "TEXTUAL_ANIMATIONS", env.pop("TEXTUAL_ANIMATIONS", "full"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert GlassSample(**kwargs).glass.motion == expected


def test_class_attribute_options() -> None:
    class Calm(GlassSample):
        GLASS_MOTION = "minimal"
        GLASS_BACKGROUND = ("#203040", "#101820")
        GLASS = False

    app = Calm()
    assert app.glass.motion == "minimal"
    assert app.glass.options.background == ("#203040", "#101820")
    assert not app.glass.detect().enabled  # GLASS = False never turns on


# -- PixelMixin widgets, sprites, dimming and pages ----------------------------------------


def _id(segment: Any) -> int:
    triplet = segment.style.color.triplet
    return (triplet.red << 16) | (triplet.green << 8) | triplet.blue


def _placeholder_ids(widget: Widget) -> set[int]:
    strips = widget.render_lines(widget.region.reset_offset)
    return {_id(seg) for strip in strips for seg in strip if seg.text.startswith(kitty.PLACEHOLDER)}


async def test_pixel_widgets_join_the_scene_and_diff_by_ident(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        gauge = app.query_one(RingGauge)
        layer = app.glass.layers[MAIN]
        ring = next(i for i in layer.dynamic if isinstance(i, R.Ring))
        r = gauge.content_region
        assert ring.rect == (r.x, r.y, r.width, r.height) and ring.ident == f"ring:{id(gauge)}"
        panels = [i for i in layer.static if isinstance(i, R.Panel)]
        assert panels and all(p.ident.startswith("panel:") for p in panels), "CSS panels carry an ident"
        uploads = output.model().counts.get("t", 0)
        mark = output.mark()
        gauge.set(0.8, "80%")
        await settle(app, pilot)
        sent = output.since(mark)
        assert re.search(r"a=f,i=\d+,r=1", sent), "a new value is an in-place edit"
        assert output.model().counts.get("t", 0) == uploads
        assert next(i for i in app.glass.layers[MAIN].dynamic if isinstance(i, R.Ring)).value == 0.8


@pytest.mark.parametrize("motion", ["minimal", "subtle"])
async def test_idle_frames_send_nothing(output: Output, motion: str) -> None:
    app = GlassSample(glass_motion=motion)
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        await idle(app, pilot)
        assert app.glass.motion == motion
        mark = output.mark()
        for _ in range(5):
            app.glass.on_frame()
            app.glass.tick()
            await pilot.pause()
        app.query_one(RingGauge).refresh()  # a Textual frame where nothing pixel-related changed
        app.query_one(DataTable).refresh()
        await settle(app, pilot)
        assert output.since(mark) == ""


async def test_cursor_moves_with_partial_edits(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        mark = output.mark()
        await pilot.press("down")
        for _ in range(20):
            app.glass.tick()
            await asyncio.sleep(0.016)
        await settle(app, pilot)
        sent = output.since(mark)
        assert "a=t" not in sent, "moving the selection re-uploads nothing"
        assert re.search(r"a=f,i=\d+,r=1", sent), "the pill slides with in-place edits"
        pill = next(i for i in app.glass.layers[MAIN].dynamic if isinstance(i, R.Pill))
        assert pill.rect[1] == app.query_one(DataTable).content_region.y + 2
        assert output.model().errors == []


async def test_edited_backdrop_equals_a_full_render(output: Output) -> None:
    """After many in-place edits (pill slides, a panel losing and regaining
    focus, a gauge value), what the terminal holds (the model applies edits
    with Ghostty's composition rules) is exactly a fresh full render."""
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        gauge = app.query_one(RingGauge)
        for step in ("down", "down", "up", "tab", "gauge", "tab", "down", "gauge"):
            if step == "gauge":
                gauge.set((gauge.fraction + 0.35) % 1, "")
            else:
                await pilot.press(step)
            for _ in range(12):
                app.glass.tick()
                await asyncio.sleep(0.016)
        await idle(app, pilot)
        engine = app.glass
        layer = engine.layers[MAIN]
        assert layer.grid == engine.grid
        data = engine._fresh(False, layer.rect, engine._background_for(layer.grid))
        canvas = Canvas(data, layer.rect[0], layer.rect[1])
        paint(canvas, layer.grid, layer.static)
        paint(canvas, layer.grid, layer.dynamic)
        model = output.model()
        assert model.edits > 5
        shown = model.images[layer.image_id]["frames"][0][..., :3].astype(int)
        assert np.abs(shown - to_u8(data).astype(int)).max() == 0


async def test_beacons_are_terminal_played_sprites(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        beacon = app.query_one(SpriteView)
        ids = _placeholder_ids(beacon)
        assert len(ids) == 1, "placeholder cells for the beacon"
        (image_id,) = ids
        model = output.model()
        virtual = [p for (i, _), p in model.placements.items() if i == image_id and p["virtual"]]
        assert virtual and (virtual[0]["c"], virtual[0]["r"]) == tuple(beacon.size)
        image = model.images[image_id]
        assert image["state"] == 3 and len(image["frames"]) > 2, "a waiting beacon animates"
        assert image["loops"] == 1, "forever, in the terminal"
        assert app.glass.sprite(("beacon", "waiting", "#ffc24b"), *beacon.size, lambda w, h: ([], 0)) == image_id, \
            "the same key and size share one image"
        app.glass.deactivate()
        assert app.glass.sprite(("beacon", "waiting", "#ffc24b"), 4, 2, lambda w, h: ([], 0)) is None
        assert any("◉" in beacon.render_line(y).text for y in range(beacon.size.height)), "a glyph without glass"


async def test_sprites_dim_under_popups_and_wake_after(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        below = set(engine.dimmables)
        beacon_id, = _placeholder_ids(app.query_one(SpriteView))
        still_id, = _placeholder_ids(app.query_one("#still", GlassImage))
        assert below == {beacon_id, still_id}, "the sprite and the still image carry a dimmed frame"
        model = output.model()
        for image_id in below:
            image = model.images[image_id]
            assert image["gaps"][-1] < 0, "the dimmed copy is gapless"
            assert image["cur"] == 0
        gauge = app.query_one(RingGauge)
        assert isinstance(gauge, PixelWidget)
        gauge_y = gauge.region.y + gauge.size.height // 2  # the value's row
        beacon_y = app.query_one(SpriteView).region.y
        plain_text = _text_colours(app, gauge_y, gauge.region.x, gauge.region.right)
        await pilot.press("d")
        await idle(app, pilot)
        model = output.model()
        assert engine.dimmed == below, "only images below the popup are dimmed"
        for image_id in below:
            image = model.images[image_id]
            assert image["cur"] == len(image["frames"]) - 1, "shows the dimmed copy"
            assert image["state"] == 1, "and stops playing under the popup"
        # rule 12: a PixelWidget's own strips are dimmed by the glass filter, placeholders are not
        dimmed_text = _text_colours(app, gauge_y, gauge.region.x, gauge.region.right)
        assert dimmed_text and dimmed_text.isdisjoint(plain_text), (plain_text, dimmed_text)
        on_screen = _placeholder_ids_on_screen(app, beacon_y)
        assert beacon_id in on_screen and on_screen <= below, "image ids survive the filter"
        mark = output.mark()
        await pilot.press("escape")
        await idle(app, pilot)
        model = output.model()
        assert not engine.dimmed
        for image_id in below:
            image = model.images[image_id]
            assert image["cur"] == 0
            assert image["state"] == (3 if len(image["frames"]) > 2 else 1), "animations play again"
        assert "a=t" not in output.since(mark), "nothing is uploaded again"
        assert model.errors == []


def _screen_line(app: Any, y: int) -> list[Any]:
    """Row ``y`` as Textual sends it: the top screen over the screens it shows through to."""
    update = app.screen._compositor.render_update(full=True, screen_stack=app._background_screens)
    return list(Strip.join(list(update.strips[y])))


def _text_colours(app: Any, y: int, x0: int, x1: int) -> set[str]:
    out, x = set(), 0
    for seg in _screen_line(app, y):
        if x0 <= x < x1 and seg.text.strip() and seg.style and seg.style.color:
            out.add(seg.style.color.name)
        x += seg.cell_length
    return out


def _placeholder_ids_on_screen(app: Any, y: int) -> set[int]:
    return {_id(seg) for seg in _screen_line(app, y) if kitty.PLACEHOLDER in seg.text}


async def test_page_switch_lands_pixels_with_the_text(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        first = engine.layers[MAIN]
        mark = output.mark()
        await pilot.press("2")
        await pilot.pause()
        engine.on_frame()  # the end of Textual's frame (headless runs don't reach _end_update)
        second = engine.layers[MAIN]
        assert second is not first and not second.busy and second.image_id is not None, "rendered in the same frame"
        sent = output.since(mark)
        assert f"a=t,i={second.image_id}," in sent and re.search(rf"a=p,i={second.image_id},", sent)
        panels = {p.rect for p in second.static if isinstance(p, R.Panel)}
        table = app.query_one(DataTable).region
        assert {(w.region.x, w.region.y, w.region.width, w.region.height) for w in app.query(".pane")} <= panels
        assert (table.x, table.y, table.width, table.height) not in panels
        assert second.grid is not None and engine.grid is not None
        assert second.grid.width < engine.grid.width, "at half resolution first"
        assert first in engine.page_cache.values(), "the first page's backdrop is kept"
        await idle(app, pilot)
        assert engine.layers[MAIN].grid == engine.grid, "then the full render from the worker replaces it"
        mark = output.mark()
        await pilot.press("1")
        await pilot.pause()
        engine.on_frame()
        assert engine.layers[MAIN] is first
        sent = output.since(mark)
        assert "a=t" not in sent, "a page seen before comes back without an upload"
        assert re.search(rf"a=p,i={first.image_id},", sent)
        await idle(app, pilot)
        model = output.model()
        assert model.errors == []
        shown = [p for (i, _), p in model.placements.items() if p["z"] == Z0]
        assert len(shown) == 1, "only one backdrop is placed"


async def test_theme_change_lands_in_the_same_frame(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        before = engine.layers[MAIN].image_id
        app.theme = "solarized-light"
        await pilot.pause()
        engine.on_frame()
        layer = engine.layers[MAIN]
        assert layer.image_id != before and not layer.busy, "the new colours are placed in this frame"
        assert layer.grid is not None and engine.grid is not None and layer.grid.width < engine.grid.width
        await idle(app, pilot)
        assert engine.layers[MAIN].grid == engine.grid and not output.model().errors


async def test_background_job_done_can_push_a_screen(output: Output) -> None:
    """Rule 13: ``done`` runs in the app's context, where Textual's active app is set."""
    import threading

    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        threads: list[int] = []

        def work() -> int:
            threads.append(threading.get_ident())
            return 7

        def done(result: int) -> None:
            threads.append(threading.get_ident())
            app.push_screen(Dialog())

        app.glass.background_job(work, done)
        await idle(app, pilot)
        await pilot.pause()
        assert isinstance(app.screen, Dialog)
        assert threads[0] != threads[1] == threading.get_ident()
        assert app.glass.errors == 0


async def test_app_class_css_needs_scoped_css_false(output: Output) -> None:
    """Rule 11: ``DEFAULT_CSS`` is scoped to its widget, so a rule that starts at
    the App (``App.-textual-glass Probe``) only matches with ``SCOPED_CSS = False``."""
    from textual.app import App, ComposeResult

    from textual_glass import Glass

    rule = "App.-textual-glass {name} {{ background: #ff0000; }}"

    class Scoped(Widget):
        DEFAULT_CSS = rule.format(name="Scoped")

    class Unscoped(Widget):
        SCOPED_CSS = False
        DEFAULT_CSS = rule.format(name="Unscoped")

    class Probe(Glass, App):
        def compose(self) -> ComposeResult:
            yield Scoped()
            yield Unscoped()

    app = Probe()
    async with app.run_test(size=(40, 10)) as pilot:
        await start(app, pilot)
        await pilot.pause()
        red = to_rgb("#ff0000")
        assert to_rgb(app.query_one(Unscoped).styles.background) == red
        assert to_rgb(app.query_one(Scoped).styles.background) != red


async def test_a_new_layout_lands_in_the_same_frame(output: Output) -> None:
    """The same panels in new places (no new page) still land with the text:
    the backdrop is rendered at half resolution inside the frame, then in full."""
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        layer = engine.layers[MAIN]
        key = layer.page_key
        app.query_one("#cards").styles.height = 14
        await pilot.pause()
        engine.on_frame()
        assert engine.layers[MAIN] is layer and layer.page_key == key and not layer.busy
        assert layer.grid is not None and engine.grid is not None and layer.grid.width < engine.grid.width
        table = app.query_one(DataTable).region
        assert (table.x, table.y, table.width, table.height) in {p.rect for p in layer.static if isinstance(p, R.Panel)}
        await idle(app, pilot)
        assert layer.grid == engine.grid and not output.model().errors


async def test_cached_image_is_reused_across_popups(output: Output) -> None:
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        await pilot.press("d")
        await idle(app, pilot)
        first = app.screen.query_one(GlassImage)._image_id
        await pilot.press("escape")
        await idle(app, pilot)
        assert first in app.glass.image_cache.values() and first in output.model().images, "kept in the terminal"
        mark = output.mark()
        await pilot.press("d")
        await idle(app, pilot)
        assert app.screen.query_one(GlassImage)._image_id == first
        assert not re.search(rf"a=t,i={first}\b", output.since(mark)), "the second popup uploads no picture"
        assert not output.model().errors


# -- a long day: the timer, the error budget, sprite churn, suspend and resume ----------------------


def _ticking(app: Any) -> bool:
    """Is the 60 Hz timer running? (Textual's ``Timer`` keeps that in a private event.)"""
    timer = app.glass._timer
    return timer is not None and timer._active.is_set()


async def test_timer_stays_paused_after_deactivate_when_a_render_lands_late(output: Output,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker render still running when glass turns off lands afterwards: it must
    not wake the 60 Hz timer (``kick`` does nothing while glass is off), and a tick
    that was already due pauses it again."""
    import threading

    import textual_glass.engine as engine_mod

    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        gate, running = threading.Event(), threading.Event()
        real_paint = engine_mod.paint

        def slow_paint(*args: Any) -> None:
            if threading.current_thread() is not threading.main_thread():
                running.set()
                gate.wait(5)
            real_paint(*args)

        monkeypatch.setattr(engine_mod, "paint", slow_paint)
        await pilot.resize_terminal(90, 28)  # a new backdrop size: rendered in the worker
        await pilot.pause()
        engine.on_frame()
        assert running.wait(5), "a worker render is in progress"
        executor = engine._executor
        assert executor is not None
        engine.deactivate()
        assert not _ticking(app)
        mark = output.mark()
        gate.set()
        executor.shutdown(wait=True)  # the render finishes and calls back to the app thread
        await asyncio.sleep(0.05)
        await pilot.pause()
        assert engine.pending_jobs == 0
        assert not _ticking(app), "the late render did not wake the timer"
        assert "\x1b_G" not in output.since(mark), "and its result was dropped"
        engine.kick()
        assert not _ticking(app), "kick does nothing while glass is off"
        engine._timer.resume()  # a resume that slipped through anyway
        engine.tick()
        assert not _ticking(app), "the next tick pauses it again"


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def test_errors_count_in_a_sliding_window_and_glass_comes_back(output: Output,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """More than five errors within a minute turn glass off; scattered errors never
    do. After turning itself off, glass tries again with a growing delay, at most
    three times, then stays off; a retry that stays healthy resets the count."""
    import textual_glass.engine as engine_mod

    monkeypatch.setattr(engine_mod, "RETRY_DELAYS", (0.05, 0.1, 0.15))
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.clock = FakeClock()
        notes: list[str] = []
        monkeypatch.setattr(engine, "_log", lambda text, error=False: notes.append(text))
        boom = RuntimeError("boom")

        for _ in range(12):  # twelve errors over eight hours: harmless
            engine._failed(boom)
            clock.now += 8 * 3600 / 12
        assert engine.active and engine.errors == 12

        async def burst() -> None:
            for _ in range(6):
                engine._failed(boom)
                clock.now += 5
            assert not engine.active, "six errors in half a minute turn glass off"
            assert not output.model().images, "and every image is deleted"

        async def comes_back() -> None:
            for _ in range(100):
                await asyncio.sleep(0.02)
                if engine.active:
                    break
            assert engine.active, "glass tried again"
            await idle(app, pilot)
            assert engine.layers[MAIN].image_id in output.model().images, "and drew everything again"

        delays = engine_mod.RETRY_DELAYS
        await burst()
        assert engine.retry_in == delays[0]
        await comes_back()
        clock.now += engine_mod.HEALTHY + 1  # the retry stayed healthy: the next problem is a new one
        await burst()
        assert engine.retry_in == delays[0], "the count of retries starts again"
        await comes_back()
        for attempt in (2, 3):
            clock.now += 60  # back on for only a minute: these retries are in a row
            await burst()
            assert engine.retry_in == delays[attempt - 1]
            await comes_back()
        clock.now += 60
        await burst()
        assert engine.retry_in is None, "after three retries in a row, glass stays off"
        await asyncio.sleep(0.4)
        assert not engine.active
        assert sum("trying again in" in n for n in notes) == 4, notes
        assert sum("trying glass again" in n for n in notes) == 4, notes
        assert any("for good" in n for n in notes), notes

        engine.activate(force=True)  # by hand: a fresh budget
        await idle(app, pilot)
        await burst()
        assert engine.retry_in == delays[0]
        engine.deactivate()  # turning glass off by hand cancels the retry
        assert engine.retry_in is None
        await asyncio.sleep(0.2)
        assert not engine.active
        assert not output.model().errors


async def test_errors_in_worker_callbacks_reach_the_error_budget(output: Output) -> None:
    """A callback from the worker thread that raises is counted (and logged), not lost in asyncio."""
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)

        def done(result: int) -> None:
            raise ValueError("broken done")

        app.glass.background_job(lambda: 1, done)
        await idle(app, pilot)
        assert app.glass.errors == 1 and "broken done" in (app.glass.last_error or "")
        assert app.glass.pending_jobs == 0 and app.glass.active


async def test_turning_off_inside_a_frame_writes_no_sync_markers(output: Output,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """When the error budget runs out inside Textual's frame, the deletes are written
    inside that frame: sync markers of glass's own would end Textual's update early."""
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        for _ in range(5):
            engine._failed(RuntimeError("almost"))

        def broken(now: float) -> bool:
            raise RuntimeError("the last straw")

        monkeypatch.setattr(engine, "_sync", broken)
        mark = output.mark()
        engine.on_frame()
        assert not engine.active
        sent = output.since(mark)
        assert kitty.RESET_DEFAULT_BACKGROUND in sent and "a=d" in sent
        assert kitty.SYNC_START not in sent and kitty.SYNC_END not in sent


class Dots(PixelWidget):
    """A row of sprites on its first line (like a row of status beacons), a caption on the second."""

    DEFAULT_CSS = "Dots { height: 2; width: 30; }"

    def __init__(self, colours: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.colours = colours

    def render_line(self, y: int) -> Strip:
        from textual_glass.sprites import pulse_frames
        from textual_glass.widgets import placeholder_parts, strip

        width = self.size.width
        if y or self.glass is None:
            return strip([(f"{len(self.colours)} dots", "")], width)
        parts = []
        for colour in self.colours:
            image_id = self.glass.sprite(("dot", colour), 2, 1, lambda w, h, c=colour: pulse_frames(w, h, c, frames=3),
                                         waiter=self)
            parts += [placeholder_parts(image_id, 0, 2) if image_id is not None else ("o ", colour), (" ", "")]
        return strip(parts, width)


def _churn_app() -> Any:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal

    from textual_glass import Glass
    from textual_glass.sprites import beacon_frames

    class Churn(Glass, App[None]):
        CSS = "Horizontal { height: 4; } SpriteView { width: 1fr; } Static { width: 1fr; }"

        def compose(self) -> ComposeResult:
            from textual.widgets import Static

            with Horizontal():
                yield SpriteView(("beacon", "waiting", "#ffc24b"),
                                 lambda w, h: beacon_frames(w, h, "waiting", "#ffc24b"), id="view")
                yield Static("x")
            yield Dots(["#ff0000", "#00ff00", "#0000ff"], id="dots")

    return Churn()


def _sprite_images(model: Any) -> set[int]:
    return {i for (i, _), p in model.placements.items() if p["virtual"]}


async def test_sprites_are_freed_when_no_widget_shows_them(output: Output) -> None:
    """G1: switching keys, resizing and removing widgets never piles up sprites:
    the engine's tables and the terminal's images stay bounded, a sprite another
    widget still shows stays, and nothing is uploaded twice."""
    from textual_glass.sprites import beacon_frames

    app = _churn_app()
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        engine = app.glass
        view, dots = app.query_one("#view", SpriteView), app.query_one("#dots", Dots)

        def check(most: int) -> None:
            ready = {s.image_id for s in engine.sprites.values() if s.ready}
            assert len(engine.sprites) <= most, len(engine.sprites)
            assert set(engine.dimmables) == ready, "only live sprites are dimmable"
            model = output.model()
            assert _sprite_images(model) == ready, "the terminal holds exactly the sprites in use"
            assert set(model.images) <= engine.ids.live and not model.errors

        await idle(app, pilot)
        check(4)
        colours = [f"#{r:02x}80c0" for r in range(0, 250, 8)]  # 32 keys
        for colour in colours:
            view.set_sprite(("beacon", "waiting", colour), lambda w, h, c=colour: beacon_frames(w, h, "waiting", c))
            await idle(app, pilot)
        check(4)
        for cols, rows in [(80, 24), (100, 30), (110, 32), (84, 26)] * 4:  # the view's size, and so its key, changes
            await pilot.resize_terminal(cols, rows)
            await idle(app, pilot)
        check(4)
        assert len(engine.ids.live) <= 12, len(engine.ids.live)

        # a widget with several sprites: dropping one keeps the others, without uploading them again
        red, green, blue = (_placeholder_ids_in(dots, 0, engine, colour) for colour in dots.colours)
        mark = output.mark()
        dots.colours = ["#ff0000", "#00ff00"]
        dots.refresh(Region(0, 1, dots.size.width, 1))  # only the caption line: the cached first line still shows blue
        await idle(app, pilot)
        assert blue in output.model().images, "a sprite still on screen (a cached line) stays"
        dots.refresh()
        await idle(app, pilot)
        model = output.model()
        assert blue not in model.images and {red, green} <= set(model.images)
        assert not re.search(rf"a=t,i=({red}|{green}),", output.since(mark)), "the others are not uploaded again"
        check(3)

        await view.remove()
        await idle(app, pilot)
        check(2)
        assert len(engine.sprites) == 2, "the removed view's sprite is gone"


def _placeholder_ids_in(widget: Widget, y: int, engine: Any, colour: str) -> int:
    return engine.sprites[("dot", colour, 2, 1, *_sprite_px(engine, 2, 1))].image_id


def _sprite_px(engine: Any, cols: int, rows: int) -> tuple[int, int]:
    from textual_glass.engine import SPRITE_MAX_PX

    cell_w, cell_h = engine.cell_pixels()
    scale = min(1.0, SPRITE_MAX_PX / max(cols * cell_w, rows * cell_h))
    return max(2, round(cols * cell_w * scale)), max(2, round(rows * cell_h * scale))


async def test_a_sprite_whose_maker_raises_is_tried_again_later(output: Output) -> None:
    """G8: the error is counted, the widget keeps its fallback glyph, nothing is
    retried in a hot loop, and a later render after the delay tries again."""
    from textual.app import App, ComposeResult

    from textual_glass import Glass
    from textual_glass.sprites import beacon_frames

    calls: list[int] = []

    def flaky(width: int, height: int) -> tuple[list[bytes], int]:
        calls.append(width)
        if len(calls) == 1:
            raise RuntimeError("the maker broke")
        return beacon_frames(width, height, "idle", "#38e1ff")

    class One(Glass, App[None]):
        CSS = "SpriteView { width: 4; height: 2; }"

        def compose(self) -> ComposeResult:
            yield SpriteView(("flaky",), flaky, id="view")

    app = One()
    clock = app.glass.clock = FakeClock()
    async with app.run_test(size=(40, 10)) as pilot:
        await start(app, pilot)
        engine = app.glass
        view = app.query_one(SpriteView)
        assert engine.errors == 1 and "the maker broke" in (engine.last_error or "")
        assert not engine.sprites and any("◉" in view.render_line(y).text for y in range(2)), "the fallback shows"
        for _ in range(5):
            view.refresh()
            await settle(app, pilot)
        assert len(calls) == 1 and not engine.sprites and engine.pending_jobs == 0, "no new try before the delay"
        clock.now += 6
        view.refresh()
        await idle(app, pilot)
        assert len(calls) == 2 and engine.errors == 1
        (sprite,) = engine.sprites.values()
        assert sprite.ready and _placeholder_ids(view) == {sprite.image_id}
        assert engine.active and not output.model().errors


async def test_a_long_day_keeps_every_table_bounded(output: Output) -> None:
    """Hours of resizes, page switches, popups, theme changes and the odd error:
    glass stays on and nothing it keeps grows."""
    app = GlassSample()
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        engine = app.glass
        clock = engine.clock = FakeClock()
        sizes = [(100, 32), (90, 28), (120, 36), (84, 26)]
        themes = ["nord", "gruvbox", "textual-dark"]

        def tables() -> dict[str, int]:
            return dict(images=len(engine.ids.live), sprites=len(engine.sprites), dimmables=len(engine.dimmables),
                        pages=len(engine.page_cache), cached=len(engine.image_cache), layers=len(engine.layers),
                        halos=len(engine.effects.ambient.halos), showing=len(engine._showing), bgs=len(engine._bgs),
                        tweens=len(engine.tweens._state), scrims=len(engine.effects.scrims))

        seen: list[dict[str, int]] = []
        for hour in range(8):
            await pilot.resize_terminal(*sizes[hour % len(sizes)])
            await idle(app, pilot)
            for key in ("2", "1", "d", "escape", "down"):
                await pilot.press(key)
                await idle(app, pilot)
            if hour % 3 == 2:
                app.theme = themes[hour // 3]
                await idle(app, pilot)
            if hour == 4:
                app.action_glass_motion("full")
                await idle(app, pilot)
            engine._failed(RuntimeError("a hiccup"))
            clock.now += 3600
            seen.append(tables())
        assert engine.active and engine.errors == 8, "an error an hour never turns glass off"
        first, last = seen[1], seen[-1]
        peak = {key: max(t[key] for t in seen) for key in first}
        assert peak["sprites"] <= 1 and peak["pages"] <= 4 and peak["cached"] <= 4 and peak["bgs"] <= 2, peak
        assert peak["halos"] <= 4 and peak["scrims"] <= 1 and peak["layers"] <= 2, peak
        assert last["images"] <= first["images"] + 8, (first, last)  # full motion adds shimmer, glints and a halo
        model = output.model()
        assert set(model.images) <= engine.ids.live and not model.errors
