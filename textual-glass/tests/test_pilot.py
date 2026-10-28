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
