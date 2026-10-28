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
from datetime import timedelta
from pathlib import Path
from typing import Any

import app as console
import pytest
from demo import DemoProvider
from rules import INCIDENT, RELEASE_FAILED
from textual.app import App, ComposeResult
from textual.strip import Strip

from textual_glass import Glass, GlassImage, GlassPanel, SpriteView, StatTile, kitty
from textual_glass.engine import MAIN, Z0, Z_FLOAT
from textual_glass.items import Lane, Panel, Pill, Ring
from textual_glass.palette import rgb
from textual_glass.testing.pilot_helpers import Output, idle, settle, start
from textual_orbital import (
    Alert,
    AlertScreen,
    AlertToast,
    AttentionItem,
    BeaconRow,
    Brand,
    DetailScreen,
    EntityState,
    Event,
    EventLog,
    PriorityList,
    SourceData,
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
    """Glass on, every source fetched once, everything settled, nothing reported."""
    await start(app, pilot)
    for _ in range(60):
        if app.snapshot is not None and all(s.last_ok for s in app.store.statuses()):
            break
        await asyncio.sleep(0.05)
    await idle(app, pilot)
    assert not app.errors, list(app.errors)


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


FLOAT = (0, "float")  # the bottom screen's float layer, where glass draws a toast


async def until(condition: Any, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {condition}")


async def test_toasts_take_no_keys_and_show_one_at_a_time(output: Output) -> None:
    """``alert_style="toast"``: "Claude needs you" stays a popup (``NeedsYouRule`` sets its
    style), other alerts toast in the bottom-right corner, on glass's float layer, one at a
    time with "+N more", while the keys stay with the dashboard; s snoozes, esc dismisses."""
    app = make(alerts=True, alert_style="toast")
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        assert isinstance(app.screen, AlertScreen) and app.alert_view is app.screen, "the startup wait: a popup"
        await pilot.press("escape")
        await idle(app, pilot)
        queue = app.query_one("#queue", PriorityList)
        layout = queue.region
        for key in ("one", "two", "three"):
            app.raise_alert(Alert(key, "custom", f"toast {key}", body=(f"{key}: first line", "second line"),
                                  headline="DEPLOYED", target=("item", "TT-2207")))
        await idle(app, pilot)
        toast = app.alert_view
        assert isinstance(toast, AlertToast) and toast.alert.key == "one" and app.screen is app.screen_stack[0]
        text = " ".join(str(w.render()) for w in toast.query("Static"))
        assert "toast one" in text and "one: first line" in text and "second line" not in text
        assert "+2 more" in text and "s snooze" in text and toast.border_title == "◉ DEPLOYED"
        assert (toast.region.right, toast.region.bottom) == (SIZE[0] - 1, SIZE[1] - 1), "bottom right, over the keys"
        assert toast.region.width == 40 and toast.region.height == 5
        assert queue.region == layout, "the page keeps its room"
        assert app.focused is queue, "no focus taken"
        panel = next(i for i in app.glass.layers[FLOAT].static if isinstance(i, Panel))
        assert panel.rect == (toast.region.x, toast.region.y, toast.region.width, toast.region.height)
        model = output.model()
        floats = [i for (i, _), p in model.placements.items() if p["z"] == Z0 + Z_FLOAT]
        assert len(floats) == 1, "one float image over the backdrop"
        sprite = _placeholder_ids_on_screen(app, toast.region.y + 1)
        assert sprite and sprite <= set(model.images), "the beacon, a sprite in the terminal"
        await pilot.press("j")
        await pilot.pause()
        assert queue.index == 1, "keys go to the dashboard"
        await pilot.press("s")  # the console binds s to finish_alert("snooze") while a toast shows
        await idle(app, pilot)
        assert "one" in app.alert_queue.snoozed and queue.index == 1, "the toast was snoozed, not a row"
        toast = app.alert_view
        assert isinstance(toast, AlertToast) and toast.alert.key == "two"
        await pilot.press("escape")
        await idle(app, pilot)
        assert isinstance(app.alert_view, AlertToast) and app.alert_view.alert.key == "three"
        app.finish_alert("dismiss")
        await idle(app, pilot)
        assert app.alert_view is None and app.alert_queue.active is None and FLOAT not in app.glass.layers
        model = output.model()
        assert not any(i in model.images for i in floats), "the float image is deleted"
        assert model.errors == [] and not app.errors


async def test_a_toast_times_out_resolves_and_jumps(output: Output) -> None:
    """A timeout counts as dismissed and the next alert shows; a rule's toast closes when its
    condition goes away; a click jumps to the alert's target."""
    app = make(alerts=True, alert_style="toast", toast_timeout=0.5)
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        await pilot.press("escape")  # the startup wait's popup
        app.raise_alert(Alert("short", "custom", "goes by itself", priority=60))
        app.raise_alert(Alert("next", "custom", "comes after it", priority=50))
        await until(lambda: isinstance(app.alert_view, AlertToast) and app.alert_view.alert.key == "next")
        assert "short" not in app.alert_queue.snoozed and app.alert_queue.active.key == "next"
        await until(lambda: app.alert_view is None)
        await idle(app, pilot)
        assert FLOAT not in app.glass.layers
        app.toast_timeout = 30.0
        world(app)._release("GH:search@1.8.0", TrackHealth.FAIL, "integration tests failed")
        app.poll_all()
        await until(lambda: isinstance(app.alert_view, AlertToast))
        toast = app.alert_view
        assert isinstance(toast, AlertToast) and toast.alert.kind == RELEASE_FAILED and toast.beacon == "error"
        world(app)._release("GH:search@1.8.0", TrackHealth.PENDING, "retrying")
        app.poll_all()
        await until(lambda: app.alert_view is None and not toast.is_attached)
        assert app.alert_queue.active is None and not app.alert_queue.snoozed, "resolved"
        app.raise_alert(Alert("go", "custom", "click me", target=("track", "GH:search@1.8.0")))
        await until(lambda: isinstance(app.alert_view, AlertToast))
        await pilot.pause()
        await pilot.click(AlertToast)
        await until(lambda: isinstance(app.screen, DetailScreen))
        assert app.screen.kind == "RELEASE" and app.alert_view is None
        assert output.model().errors == [] and not app.errors


async def test_a_toast_moves_to_the_popup_on_top_and_back(output: Output) -> None:
    """The toast sits on the screen on top: over a popup that opens, back on the dashboard
    when it closes, with the time it had left; the alert never gets lost on the way."""
    app = make(alerts=True, alert_style="toast")
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        await pilot.press("escape")
        app.raise_alert(Alert("x", "custom", "a toast", body=("over everything",)))
        await idle(app, pilot)
        first = app.alert_view
        assert isinstance(first, AlertToast)
        await pilot.press("question_mark")
        await idle(app, pilot)
        over = app.alert_view
        assert isinstance(over, AlertToast) and over is not first and over.parent is app.screen
        assert type(app.screen).__name__ == "HelpScreen" and not first.is_attached
        assert over.remaining() <= first.timeout and over.alert is first.alert
        assert (1, "float") in app.glass.layers, "drawn above the popup's dialog, not dimmed"
        await pilot.press("escape")  # the help screen closes, with the toast moved off it first
        await idle(app, pilot)
        back = app.alert_view
        assert isinstance(back, AlertToast) and back.parent is app.screen_stack[0] and back.alert.key == "x"
        assert FLOAT in app.glass.layers and (1, "float") not in app.glass.layers
        app.finish_alert("dismiss")
        await idle(app, pilot)
        assert app.alert_view is None and app.alert_queue.active is None
        assert output.model().errors == [] and not app.errors


async def test_a_toast_without_glass(output: Output) -> None:
    app = make(alerts=True, alert_style="toast", glass=False)
    async with app.run_test(size=SIZE) as pilot:
        await until(lambda: isinstance(app.screen, AlertScreen))
        await pilot.pause()
        await pilot.press("escape")
        app.raise_alert(Alert("x", "custom", "plain toast", body=("in any terminal",)))
        await until(lambda: isinstance(app.alert_view, AlertToast))
        await pilot.pause()
        toast = app.alert_view
        assert isinstance(toast, AlertToast) and toast.region.right == SIZE[0] - 1
        r = toast.region
        lines = ["".join(seg.text for seg in _screen_line(app, y))[r.x:r.right] for y in range(r.y, r.bottom)]
        assert lines[0].startswith("╭─ ◉ INCOMING TRANSMISSION") and lines[-1].startswith("╰"), lines
        assert "plain toast" in lines[1] and "in any terminal" in lines[2] and "◉" in lines[2], lines
        assert "\x1b_G" not in output.text, "no graphics at all"


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
        await pilot.press("z")  # what is snoozed
        await settle(app, pilot)
        assert isinstance(app.screen, DetailScreen) and app.screen.kind == "SNOOZED"
        listed = [str(value) for _, value in app.screen.fields]
        assert any(first.title in line and re.search(r"back in (59m|1h)$", line) for line in listed), listed
        await pilot.press("escape")
        await pilot.press("u")
        await settle(app, pilot)
        assert first.key in [getattr(r, "key", None) for r in queue.rows]
        await pilot.press("z")
        await settle(app, pilot)
        assert not isinstance(app.screen, DetailScreen), "nothing to list"


async def test_updated_is_the_last_good_fetch(output: Output) -> None:
    """The key bar's "updated HH:MM" is the last good fetch of any source: a publish
    after failed fetches re-stamps the snapshot (O5) but doesn't move it."""
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        earlier = now() - timedelta(hours=2)
        for status in app.store.statuses():
            app.store.apply(status.key, SourceData(), earlier)  # their last good fetch, two hours ago
            app.store.fail(status.key, "HTTP 503")
        app._publish(now())
        app.minute_tick(now())
        await settle(app, pilot)
        assert app.snapshot is not None and app.snapshot.taken_at > earlier + timedelta(hours=1)
        keys = app.query_one("#keys").render_line(0).text
        assert f"updated {earlier.astimezone():%H:%M}" in keys, keys


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
            if kind == "RELEASE":  # its run log is a lazy body, loaded in a thread
                body = app.screen.query_one("#detail-body")
                assert "loading…" in str(body.render())
                for _ in range(40):
                    if "run 8820 by mei" in str(body.render()):
                        break
                    await asyncio.sleep(0.05)
                assert "build ✓ passed  ·  test … running" in str(body.render())
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


async def test_the_week_page_steps_back_through_held_and_fetched_weeks(output: Output) -> None:
    """``[`` and ``]``: last week comes from the snapshot (``Snapshot.week(on)``), older weeks
    from the provider's ``history`` through ``OrbitalApp.fetch``, once each."""
    from pages import WeekPage

    from textual_orbital.model import monday_of

    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await ready(app, pilot)
        await pilot.press("3")
        await idle(app, pilot)
        page = app.query_one(WeekPage)
        panel = app.query_one("#log-panel", GlassPanel)
        this = monday_of(now().astimezone().date())
        assert str(panel.border_title).startswith("THIS WEEK")
        calls: list[tuple[Any, ...]] = []
        history = app.provider.history  # type: ignore[attr-defined]
        app.provider.history = lambda *args: calls.append(args) or history(*args)  # type: ignore[attr-defined]
        await pilot.press("left_square_bracket")
        await idle(app, pilot)
        assert "last week" in str(panel.border_title) and not calls, "held by the snapshot"
        log = app.query_one("#events", EventLog)
        last = timedelta(weeks=1)
        assert log.rows and all(this - last <= e.when.astimezone().date() < this for e in log.rows)
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert "2 weeks ago" in str(panel.border_title) and "loading…" in str(panel.border_title)
        for _ in range(60):
            if "loading" not in str(panel.border_title):
                break
            await asyncio.sleep(0.05)
        await idle(app, pilot)
        older = this - 2 * last
        assert calls == [(older - last, older + timedelta(days=6))], "that week and the one before, in a thread"
        assert log.rows and all(older <= e.when.astimezone().date() < older + last for e in log.rows)
        tile = app.query_one("#t-merged", StatTile)
        assert len(tile.chart.values) == 7, "a past week is whole"
        await pilot.press("right_square_bracket", "left_square_bracket")  # back and forth: fetched once
        await idle(app, pilot)
        assert len(calls) == 1 and "2 weeks ago" in str(panel.border_title)
        await pilot.press("right_square_bracket", "right_square_bracket", "right_square_bracket")
        await idle(app, pilot)
        assert str(panel.border_title).startswith("THIS WEEK") and page.week_on is None, "no further than now"
        assert not app.errors and output.model().errors == []


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


class Leaves(Widgets):
    """Records ``PriorityList.RowsLeft`` without subclassing the list."""

    def __init__(self) -> None:
        super().__init__()
        self.left: list[tuple[str, ...]] = []

    def on_priority_list_rows_left(self, message: PriorityList.RowsLeft) -> None:
        assert message.control is self.query_one(PriorityList)
        self.left.append(tuple(item.key for item in message.items))


async def test_queue_posts_the_rows_that_left() -> None:
    items = [AttentionItem(f"k{i}", "XX", 5, "review", f"item {i}") for i in range(5)]
    app = Leaves()
    async with app.run_test(size=(80, 20)) as pilot:
        queue = app.query_one(PriorityList)
        queue.set_items(items[:3], items[3:])
        await pilot.pause()
        assert app.left == [], "nothing left an empty list"
        queue.action_toggle_aware()  # folding the be-aware section is not leaving
        queue.set_items([items[0], items[2]], items[3:4])  # k1 snoozed, k4 gone from its source
        await pilot.pause()
        assert app.left == [("k1", "k4")], "in their old order"
        queue.set_items([items[1], items[0]], [items[2]])  # k1 back, k2 moved to be-aware: only k3 left
        queue.set_items([items[1], items[0]], [items[2]])  # the same rows: no message
        await pilot.pause()
        assert app.left == [("k1", "k4"), ("k3",)]


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
