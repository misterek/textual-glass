"""Pilot tests of Mission Console (``examples/console``) with glass forced on and
its output captured into the kitty model (see ``conftest.output``), and of the
widgets on their own with another app's stage names.

Pilot runs headless, so glass is forced on with ``start``; thread callbacks,
timers and the demo world run for real.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import app as console
import pytest
from demo import DemoProvider
from rules import INCIDENT, RELEASE_FAILED
from textual.app import App, ComposeResult
from textual.strip import Strip

from textual_glass import Glass, GlassImage, GlassPanel, SpriteView, kitty
from textual_glass.engine import MAIN, Z0
from textual_glass.items import Lane, Panel, Pill, Ring
from textual_glass.palette import rgb
from textual_glass.testing.pilot_helpers import Output, idle, settle, start
from textual_orbital import (
    Alert,
    AlertScreen,
    AttentionItem,
    BeaconRow,
    Brand,
    DetailScreen,
    EntityState,
    Event,
    EventLog,
    PriorityList,
    Track,
    TrackHealth,
    TrackItem,
    TrackTable,
    now,
)
from textual_orbital.theme import PALETTE

SIZE = (90, 28)


def make(speed: float = 1.0, dynamics: bool = False, alerts: bool = False, **kw: Any) -> console.MissionConsole:
    return console.MissionConsole(DemoProvider(speed=speed, dynamics=dynamics), alerts=alerts, notify="osc777", **kw)


async def ready(app: console.MissionConsole, pilot: Any) -> None:
    """Glass on, every source fetched once, everything settled."""
    await start(app, pilot)
    for _ in range(60):
        if app.snapshot is not None and all(s.last_ok for s in app.store.statuses()):
            break
        await asyncio.sleep(0.05)
    await idle(app, pilot)


def world(app: console.MissionConsole) -> Any:
    return app.provider.world  # type: ignore[attr-defined]


def _id(seg: Any) -> int:
    triplet = seg.style.color.triplet
    return (triplet.red << 16) | (triplet.green << 8) | triplet.blue


def _screen_line(app: Any, y: int) -> list[Any]:
    """Row ``y`` as Textual sends it: the top screen over the screens it shows through to."""
    update = app.screen._compositor.render_update(full=True, screen_stack=app._background_screens)
    return list(Strip.join(list(update.strips[y])))


def _text_colours(app: Any, y: int, x0: int, x1: int) -> set[str]:
    out, x = set(), 0
    for seg in _screen_line(app, y):
        if x0 <= x < x1 and seg.text.strip() and kitty.PLACEHOLDER not in seg.text and seg.style and seg.style.color:
            out.add(seg.style.color.name)
        x += seg.cell_length
    return out


def _placeholder_ids_on_screen(app: Any, y: int) -> set[int]:
    return {_id(seg) for seg in _screen_line(app, y) if kitty.PLACEHOLDER in seg.text}


def _panel_at(app: Any, selector: str, level: int = 0) -> Panel:
    r = app.screen.query_one(selector).region
    return next(p for p in app.glass.layers[(level, "main")].static
                if isinstance(p, Panel) and p.rect == (r.x, r.y, r.width, r.height))


# -- the console ------------------------------------------------------------------------


async def test_console_scene_and_clean_protocol(output: Output) -> None:
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        assert app.theme == "orbital" and app.glass.palette is not None and app.glass.palette.backdrop == "nebula"
        layer = app.glass.layers[MAIN]
        kinds = {type(i).__name__ for i in layer.static + layer.dynamic}
        assert {"Panel", "HeaderBar", "Signal", "Pill", "Ring", "Lane", "Dot", "Rule"} <= kinds
        panels = [i for i in layer.static if isinstance(i, Panel)]
        assert len(panels) == 4, "beacons, queue, gauges, releases"
        gauges = _panel_at(app, "#gauges-panel")
        assert (gauges.c1, gauges.c2) == (rgb(PALETTE["accent"]), rgb(PALETTE["accent_2"])), "cyan to violet"
        beacons = _panel_at(app, "#beacons-panel")
        assert beacons.c1 == rgb(PALETTE["amber"]), "the beacons panel turns amber while Claude waits"
        assert sum(1 for i in layer.dynamic if isinstance(i, Ring)) == 3
        lanes = [i for i in layer.static if isinstance(i, Lane)]
        assert len(lanes) == 5 and not lanes[-1].chevron, "five stages, the last without a chevron"
        model = output.model()
        assert model.errors == [] and app.glass.last_error is None
        backdrop = [p for p in model.placements.values() if p["z"] == Z0]
        assert len(backdrop) == 1 and (backdrop[0]["c"], backdrop[0]["r"]) == SIZE


async def test_idle_frames_send_nothing(output: Output) -> None:
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        mark = output.mark()
        for _ in range(5):
            app.glass.on_frame()
            app.glass.tick()
            await pilot.pause()
        app.query_one("#queue").refresh()  # a Textual frame where nothing pixel-related changed
        app.poll_all()  # the same data again: no change downstream
        await asyncio.sleep(0.3)
        await settle(app, pilot)
        assert output.since(mark) == ""


async def test_cursor_moves_with_partial_edits(output: Output) -> None:
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        mark = output.mark()
        await pilot.press("j")
        for _ in range(20):
            app.glass.tick()
            await asyncio.sleep(0.016)
        await settle(app, pilot)
        sent = output.since(mark)
        assert "a=t" not in sent, "moving the selection re-uploads nothing"
        assert re.search(r"a=f,i=\d+,r=1", sent), "the pill slides with in-place edits"
        pill = next(i for i in app.glass.layers[MAIN].dynamic if isinstance(i, Pill))
        queue = app.query_one("#queue", PriorityList)
        assert pill.rect[1] == queue.content_region.y + 1
        assert output.model().errors == []


async def test_every_page_renders(output: Output) -> None:
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        for key, expected, panel in (("2", "Dot", "#release-track-panel"), ("3", "Bars", "#log-panel"),
                                     ("4", "Ring", "#goals-panel"), ("1", "Signal", "#queue-panel")):
            await pilot.press(key)
            await idle(app, pilot)
            kinds = {type(i).__name__ for i in app.glass.layers[MAIN].dynamic}
            assert expected in kinds, (key, kinds)
            assert app.query_one(panel).region.area, panel
        assert app.query_one("#release-table", TrackTable).rows, "the releases table is filled"
        model = output.model()
        assert model.errors == [] and app.glass.last_error is None


async def test_beacons_are_terminal_played_sprites(output: Output) -> None:
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        beacons = app.query_one("#beacons", BeaconRow)
        strips = beacons.render_lines(beacons.region.reset_offset)
        ids = {_id(seg) for strip in strips for seg in strip if seg.text.startswith(kitty.PLACEHOLDER)}
        assert len(ids) >= 3, "placeholder cells for waiting, error and working beacons"
        model = output.model()
        for image_id in ids:
            assert image_id in model.images
            virtual = [p for (i, _), p in model.placements.items() if i == image_id and p["virtual"]]
            assert virtual, "a virtual placement for the placeholders"
        playing = [model.images[i] for i in ids if model.images[i]["state"] == 3]
        assert playing and all(len(img["frames"]) > 1 for img in playing), "waiting/working beacons animate"
        assert all(img["loops"] == 1 for img in playing), "they loop forever, in the terminal"


async def test_alert_popup_layers_dim_and_dismiss(output: Output) -> None:
    """The first alert is raised from a fetch result, which arrives through a
    thread callback: it can push a screen because it runs in the app's
    context (rule 13). Below it, text is dimmed by glass's filter and
    placeholder colours are left alone (rules 6, 12)."""
    app = make(alerts=True)
    async with app.run_test(size=SIZE) as pilot:
        await start(app, pilot)
        queue = app.query_one("#queue", PriorityList)
        beacons = app.query_one("#beacons", BeaconRow)
        for _ in range(60):
            if isinstance(app.screen, AlertScreen):
                break
            await asyncio.sleep(0.05)
        assert isinstance(app.screen, AlertScreen), "Claude is already waiting at startup"
        assert app.screen.alert.kind == "waiting" and app.screen.beacon == "waiting"
        await idle(app, pilot)
        assert {MAIN, (1, "main")} <= set(app.glass.layers) and 1 in app.glass.effects.scrims
        dialog = _panel_at(app, "#alert-dialog", level=1)
        assert dialog.dialog and dialog.c1 == rgb(PALETTE["amber"])
        assert isinstance(app.screen.query_one("#alert-picture"), SpriteView), "the drawn beacon"
        row_y = app.screen.query_one("#alert-dialog").region.bottom  # the first queue row below the dialog
        assert row_y < queue.content_region.bottom
        assert _text_colours(app, row_y, queue.region.x, queue.region.right), "queue text shows under the popup"
        await pilot.press("escape")
        await idle(app, pilot)
        bright = _text_colours(app, row_y, queue.region.x, queue.region.right)
        app.raise_alert(Alert("again", "waiting", "again", priority=100))
        await idle(app, pilot)
        dimmed = _text_colours(app, row_y, queue.region.x, queue.region.right)
        assert dimmed and dimmed.isdisjoint(bright), (bright, dimmed)
        shown = _placeholder_ids_on_screen(app, beacons.region.y)
        assert shown and shown <= set(app.glass.dimmables), "image ids survive the filter"
        assert "\x1b]777;notify;Mission Console: Needs you;fix-flaky-checkout-test" in output.text
        assert "\x1b]2;◉ 1 waiting · Mission Console\x07" in output.text
        await pilot.press("escape")
        await idle(app, pilot)
        await asyncio.sleep(0.3)
        app.glass.tick()
        assert not isinstance(app.screen, AlertScreen)
        assert (1, "main") not in app.glass.layers and not app.glass.effects.scrims
        assert output.model().errors == []
        await pilot.press("q")
        await pilot.pause()
    titles = re.findall(r"\x1b\]2;([^\x07]*)\x07", output.text)
    assert titles[0] == "Mission Console" and titles[-1] == "", "the title is cleared on exit"


async def test_alert_queue_snooze_and_resolve(output: Output) -> None:
    app = make(alerts=True)
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        assert isinstance(app.screen, AlertScreen)
        app.raise_alert(Alert("x", "custom", "custom one", priority=10))
        await pilot.press("s")  # snooze the Claude alert
        await idle(app, pilot)
        assert isinstance(app.screen, AlertScreen) and app.screen.alert.key == "x", "the next one shows"
        assert any(k.startswith("waiting:fix-flaky-checkout-test") for k in app.alert_queue.snoozed)
        await pilot.press("escape")
        await idle(app, pilot)
        # the waiting session moves on: its alert goes away, even from the snooze list
        world(app)._session("fix-flaky-checkout-test", EntityState.WORKING, "", "")
        app.poll_all()
        for _ in range(40):
            await asyncio.sleep(0.05)
            if not app.alert_queue.snoozed:
                break
        assert not app.alert_queue.snoozed
        # a new wait pops up, and closes by itself when the wait is answered elsewhere
        world(app)._session("rate-limit-middleware", EntityState.WAITING, "question", "Keep the old endpoint?")
        app.poll_all()
        for _ in range(40):
            await asyncio.sleep(0.05)
            if isinstance(app.screen, AlertScreen):
                break
        assert isinstance(app.screen, AlertScreen) and app.screen.alert.title == "rate-limit-middleware"
        world(app)._session("rate-limit-middleware", EntityState.WORKING, "", "")
        app.poll_all()
        for _ in range(40):
            await asyncio.sleep(0.05)
            if not isinstance(app.screen, AlertScreen):
                break
        assert not isinstance(app.screen, AlertScreen), "the popup resolves itself"
        assert app.alert_queue.active is None


async def test_alerts_follow_the_demo_dynamics(output: Output) -> None:
    app = make(speed=40.0, dynamics=True, alerts=True)
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        seen: set[str] = set()
        for _ in range(240):
            screen = app.screen
            if isinstance(screen, AlertScreen):
                seen.add(screen.alert.kind)
                assert screen.beacon == ("error" if screen.alert.kind in (RELEASE_FAILED, INCIDENT) else "waiting")
                await pilot.press("escape")
            await asyncio.sleep(0.05)
            if len(seen) == 3:
                break
        assert seen == {"waiting", RELEASE_FAILED, INCIDENT}
        assert output.model().errors == []


async def test_snooze_hides_a_row(output: Output) -> None:
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        queue = app.query_one("#queue", PriorityList)
        first = queue.highlighted
        await pilot.press("s")
        await settle(app, pilot)
        assert first is not None and first.key not in [getattr(r, "key", None) for r in queue.rows]
        assert "snoozed" in app.query_one("#keys").render_line(0).text
        await pilot.press("u")
        await settle(app, pilot)
        assert first.key in [getattr(r, "key", None) for r in queue.rows]


async def test_detail_help_sources_and_jump(output: Output) -> None:
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        await pilot.press("enter")  # the top row is a waiting Claude session: its record opens
        await idle(app, pilot)
        assert isinstance(app.screen, DetailScreen) and app.screen.kind == "CLAUDE CODE SESSION"
        await pilot.press("escape")
        await pilot.press("question_mark")
        await idle(app, pilot)
        assert type(app.screen).__name__ == "HelpScreen"
        await pilot.press("escape")
        await pilot.press("h")
        await idle(app, pilot)
        assert type(app.screen).__name__ == "SourcesScreen"
        await pilot.press("escape")
        for target, kind in ((("track", "GH:search@1.8.0"), "RELEASE"), (("item", "TT-2207"), "ITEM"),
                             (("entity", "dependency-bumps"), "CLAUDE CODE SESSION")):
            app.jump_to(target)
            await idle(app, pilot)
            assert isinstance(app.screen, DetailScreen) and app.screen.kind == kind, target
            await pilot.press("escape")
        app.jump_to(("track", "nothing"))
        await pilot.press("tab")  # to the releases track
        await pilot.press("tab")
        await idle(app, pilot)
        assert not isinstance(app.screen, DetailScreen)
        assert output.model().errors == [] and app.glass.last_error is None


@pytest.mark.parametrize("size, classes, visible, hidden", [
    ((70, 22), {"-narrow", "-short"}, ["#strip-panel"], ["#console-side"]),
    ((90, 28), {"-compact", "-mid"}, ["#console-side", "#gauges-panel", "#track-panel"],
     ["#strip-panel", "#preview-panel"]),
    ((140, 45), {"-wide", "-tall"}, ["#console-side", "#preview-panel"], ["#strip-panel"]),
])
async def test_breakpoints(output: Output, size: tuple[int, int], classes: set[str], visible: list[str],
                           hidden: list[str]) -> None:
    app = make()
    async with app.run_test(size=size) as pilot:
        await ready(app, pilot)
        assert classes <= set(app.screen.classes)
        for selector in visible:
            assert app.query_one(selector).region.area, selector
        for selector in hidden:
            assert not app.query_one(selector).region.area, selector
        queue = app.query_one("#queue", PriorityList)
        assert len(queue.rows) >= 9 and queue.size.height >= 10, "the queue keeps most of the window"
        assert queue.lines_per_row == (2 if size == (140, 45) else 1), "two-line rows when tall"
        if size == (70, 22):
            strip = app.query_one("#track-strip", Track)
            assert strip.summary_mode and "BUILD" in strip.render_line(0).text
            await pilot.press("2")  # its table was filled while the page had no size yet
            await idle(app, pilot)
            table = app.query_one("#release-table", TrackTable)
            assert table.scroll == 0 and "IN FLIGHT" in table.render_line(0).text, "the first section shows"
        assert output.model().errors == []


async def test_fallback_is_plain_text(output: Output) -> None:
    app = make(glass=False)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        for _ in range(60):
            if app.snapshot is not None:
                break
            await asyncio.sleep(0.05)
        await pilot.pause()
        assert not app.glass.active and not app.has_class("-textual-glass")
        assert "\x1b_G" not in output.text, "no graphics at all"
        panel = app.query_one("#queue-panel", GlassPanel)
        assert panel.styles.border.top[0] == "round", "a plain Textual border instead of glass"
        queue = app.query_one("#queue", PriorityList)
        assert "▂" in queue.render_line(0).text, "the signal meter falls back to block characters"
        track = app.query_one("#track", Track)
        lines = [track.render_line(y).text for y in range(track.size.height)]
        assert any("●" in line or "◉" in line for line in lines), "dots fall back to glyphs"
        beacons = app.query_one("#beacons", BeaconRow)
        assert "◉" in beacons.render_line(0).text and kitty.PLACEHOLDER not in beacons.render_line(0).text


async def test_ages_agree_at_minute_resolution(output: Output) -> None:
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        world(app)._session("rate-limit-middleware", EntityState.WAITING, "plan ready for review", "")
        app.poll_all()
        await asyncio.sleep(0.3)
        await idle(app, pilot)
        queue = app.query_one("#queue", PriorityList)
        beacons = app.query_one("#beacons", BeaconRow)
        rows = [queue.render_line(y).text for y in range(queue.size.height)]
        lines = [beacons.render_line(y).text for y in range(beacons.size.height)]
        row = next(r for r in rows if "rate-limit-middleware" in r)
        assert row.rstrip().endswith("<1m")
        assert any("WAITING <1m" in line for line in lines)
        assert not re.search(r"\b\d+s\b", " ".join(rows + lines)), "no seconds anywhere"


async def test_sprites_dim_under_popups_and_wake_after(output: Output) -> None:
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        engine = app.glass
        below = set(engine.dimmables)
        assert len(below) >= 4, "beacon and stuck-release pulse sprites carry a dimmed frame"
        model = output.model()
        for image_id in below:
            assert model.images[image_id]["gaps"][-1] < 0, "the dimmed copy is gapless"
            assert model.images[image_id]["cur"] == 0
        await pilot.press("question_mark")
        await idle(app, pilot)
        model = output.model()
        assert engine.dimmed == below
        for image_id in below:
            image = model.images[image_id]
            assert image["cur"] == len(image["frames"]) - 1, "shows the dimmed copy"
            assert image["state"] == 1, "and stops playing under the popup"
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


async def test_page_switch_lands_pixels_with_the_text(output: Output) -> None:
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        engine = app.glass
        console_layer = engine.layers[MAIN]
        await pilot.press("2")
        await pilot.pause()
        engine.on_frame()  # the end of Textual's frame (headless runs don't reach _end_update)
        releases_layer = engine.layers[MAIN]
        assert releases_layer is not console_layer and not releases_layer.busy, "rendered in the same frame"
        assert console_layer in engine.page_cache.values(), "the console's backdrop is kept"
        await idle(app, pilot)
        mark = output.mark()
        await pilot.press("1")
        await pilot.pause()
        engine.on_frame()
        assert engine.layers[MAIN] is console_layer
        sent = output.since(mark)
        assert "a=t" not in sent, "a page seen before comes back without an upload"
        assert re.search(rf"a=p,i={console_layer.image_id},", sent)
        await idle(app, pilot)
        model = output.model()
        assert model.errors == []
        assert len([p for p in model.placements.values() if p["z"] == Z0]) == 1, "only one backdrop is placed"


async def test_alert_pictures_load_on_demand(output: Output, tmp_path: Path) -> None:
    from PIL import Image

    picture = tmp_path / "beacon.png"
    Image.new("RGBA", (40, 40), (255, 194, 75, 255)).save(picture)
    app = make(alerts=True)
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        await pilot.press("escape")  # the startup alert
        app.raise_alert(Alert("pic", "custom", "with a picture", image=str(picture), headline="DEPLOYED"))
        for _ in range(60):
            await asyncio.sleep(0.05)
            if isinstance(app.screen, AlertScreen):
                break
        await idle(app, pilot)
        assert isinstance(app.screen, AlertScreen) and app.alert_images[str(picture)] is not None
        assert isinstance(app.screen.query_one("#alert-picture"), GlassImage)
        await pilot.press("escape")
        app.raise_alert(Alert("bad", "custom", "a broken picture", image=str(tmp_path / "missing.gif")))
        for _ in range(60):
            await asyncio.sleep(0.05)
            if isinstance(app.screen, AlertScreen):
                break
        assert isinstance(app.screen, AlertScreen) and app.screen.alert.key == "bad"
        assert isinstance(app.screen.query_one("#alert-picture"), SpriteView), "the drawn beacon instead"
        assert output.model().errors == []


# -- the widgets on their own -------------------------------------------------------------


class Pipeline(Glass, App[None]):
    """Another app's stages and event kinds: nothing in the widgets knows the console's."""

    def compose(self) -> ComposeResult:
        yield Track(("PLAN", "DO", "CHECK"), stuck_after_days=5, id="t")
        yield TrackTable(("PLAN", "DO", "CHECK"), id="table")
        yield EventLog(colors={"filed": "#ff0000"}, id="log")


async def test_widgets_take_any_stages_and_kinds(output: Output) -> None:
    t = now()
    items = [TrackItem("a", "XX", "first", "A-1", 0, TrackHealth.OK, t), TrackItem("b", "XX", "second", "B-2", 2,
                                                                                   TrackHealth.FAIL, t),
             TrackItem("c", "XX", "far", stage=9, health=TrackHealth.IDLE)]
    app = Pipeline()
    async with app.run_test(size=(80, 30)) as pilot:
        track, table, log = app.query_one(Track), app.query_one(TrackTable), app.query_one(EventLog)
        track.set_items(items, t)
        table.set_groups([("OPEN", items[:2]), ("PARKED", items[2:])], t)
        log.set_rows([Event(t, "filed", "XX", "a"), Event(t, "anything", "XX", "b")])
        await pilot.pause()
        text = "\n".join(track.render_line(y).text for y in range(track.size.height))
        assert "PLAN" in text and "CHECK" in text and "A-1" in text, "stage names, and labels after the dots"
        assert [len(lane) for lane in track.lanes] == [1, 0, 2], "a stage past the end goes in the last lane"
        assert track.selected_key == "b", "the failing item is selected first"
        assert "OPEN" in table.render_line(0).text and "check" in table.render_line(2).text
        assert log.kind_color("filed") == "#ff0000" and log.kind_color("anything") == log.kind_color("anything")
        await start(app, pilot)
        lanes = [i for i in app.glass.layers[MAIN].static if isinstance(i, Lane)]
        assert len(lanes) == 3
        track.styles.height = 2  # fewer rows than stages: a one-line summary
        await pilot.pause()
        assert track.summary_mode and "◉1" in track.render_line(0).text
        assert output.model().errors == []


class Widgets(App[None]):
    """Widgets on their own, sized by CSS (no glass)."""

    CSS = """
    PriorityList { width: 70; height: 12; }
    Track { width: 50; height: 4; }
    """

    def compose(self) -> ComposeResult:
        yield PriorityList(id="queue")
        yield Track(("PLAN", "DO", "CHECK"), id="track")
        yield Brand("BOARD", ["one"], id="brand")


async def test_queue_rows_take_two_lines_when_every_row_fits() -> None:
    items = [AttentionItem(f"k{i}", "XX", 5, "review", f"item {i}", where="repo", detail="detail") for i in range(6)]
    app = Widgets()
    async with app.run_test(size=(80, 20)) as pilot:
        queue = app.query_one(PriorityList)
        queue.set_items(items[:4], items[4:5])  # 4 rows, the BE AWARE header and 1 row: 12 lines fit in 12
        await pilot.pause()
        assert queue.row_lines == 2 and "repo · detail" in queue.render_line(1).text
        queue.set_items(items[:5], items[5:])  # 7 rows: 14 lines don't
        assert queue.row_lines == 1
        queue.styles.width = 50
        queue.set_items(items[:2])
        await pilot.pause()
        assert queue.row_lines == 1, "under 60 columns, one line"
        queue.two_line_setting = True
        assert queue.row_lines == 2, "two_line=True always"


async def test_track_caption_keeps_the_name_and_cuts_the_tail() -> None:
    t = now()
    item = TrackItem("a", "XX", "storefront checkout", "v2.3.1", 1, TrackHealth.WARN, t,
                     detail="changes requested by two reviewers")
    app = Widgets()
    async with app.run_test(size=(80, 20)) as pilot:
        track = app.query_one(Track)
        track.set_items([item], t)
        await pilot.pause()
        caption = track.render_line(3).text
        assert "v2.3.1 storefront checkout · changes requested" in caption and caption.endswith("…"), caption
        track.set_items([replace(item, title="a very long name for a release that keeps going and going")], t)
        caption = track.render_line(3).text
        assert caption.endswith("a very long name for a rele… · changes r…"), "the tail keeps its 10 characters"
        track.styles.width = 20
        await pilot.pause()
        assert " · " not in track.render_line(3).text and "a very" in track.render_line(3).text, "the tail goes"


async def test_brand_badge_takes_badge_changed_text_as_is() -> None:
    app = Widgets()
    async with app.run_test(size=(80, 20)) as pilot:
        brand = app.query_one(Brand)
        brand.set_badge("◉ 1 waiting")  # what OrbitalApp.badge_changed passes
        await pilot.pause()
        assert "◉ 1 waiting" in brand.render_line(0).text and "◉ ◉" not in brand.render_line(0).text
        brand.set_badge("2 WAITING")
        await pilot.pause()
        assert "  ◉ 2 WAITING" in brand.render_line(0).text, "a glyph in front of plain text"


@pytest.mark.parametrize("url", [None, "https://example.com/a"])
async def test_alert_hint_offers_open_only_with_a_link(url: str | None) -> None:
    app = App[None]()
    async with app.run_test(size=(90, 28)) as pilot:
        app.push_screen(AlertScreen(Alert("k", "test", "disk almost full", url=url)))
        await pilot.pause()
        hint = app.screen.query_one(".dialog-hint").render_line(0).text
        assert "jump to it" in hint and "esc dismiss" in hint
        assert ("o open" in hint) == (url is not None), hint
        if url is None:
            await pilot.press("o")
            await pilot.pause()
            assert isinstance(app.screen, AlertScreen), "o does nothing without a link"
