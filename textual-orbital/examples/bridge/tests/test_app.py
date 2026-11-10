"""The bridge app in Textual's pilot, with glass forced on and its output checked by the kitty model."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from textual.color import Color

from bridge import crew, hook
from bridge.app import BridgeApp, main
from bridge.fake import FakeBridge
from bridge.widgets import (
    PICTURES,
    POOL_STRENGTH,
    STEADY,
    TIERS,
    Arc,
    Deck,
    Pane,
    Pool,
    Rect,
    Station,
    Viewscreen,
    lamp_color,
    plan_for,
    steady,
    tile_columns,
    tile_frames,
)
from bridge.widgets import Path as LightPath
from textual_glass.engine import MAIN, SPRITE_MAX_PX, placeholder_ids
from textual_glass.kitty import PLACEHOLDER
from textual_glass.testing.pilot_helpers import Output, idle, settle, start

T0 = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)
SIZES = [(70, 22), (90, 28), (140, 45)]


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def fake_app(tmp_path: Path, at: float = 12.5, **kwargs: Any) -> tuple[BridgeApp, FakeBridge, Clock]:
    """The demo on a fake wall clock; a poll interval of an hour, so the tests call ``poll()`` themselves."""
    clock = Clock()
    fake = FakeBridge(tmp_path, 20, clock=clock, start=T0, at=at)
    return BridgeApp(fake=fake, poll=3600, **kwargs), fake, clock


def station_of(app: BridgeApp, sid: str) -> Station:
    return app.deck.stations[app.bridge.chart.station_of[sid]]


def station_keys(app: BridgeApp) -> set[tuple[Any, ...]]:
    return {key[:6] for key in app.glass.sprites if key[0] == "bridge-station"}


ROOM = (Arc, Pool, LightPath, Pane)


def room_items(app: BridgeApp) -> dict[str, Arc | Pool | LightPath | Pane]:
    """The room's items in the bottom screen's backdrop, by ident: all static (glass caches them in the
    backdrop's base), never dynamic."""
    layer = app.glass.layers[MAIN]
    assert not any(isinstance(item, ROOM) for item in layer.dynamic), "the room is static"
    found = {item.ident: item for item in layer.static if isinstance(item, ROOM)}
    assert len(found) == sum(1 for item in layer.static if isinstance(item, ROOM)), "an ident each"
    return found


def backdrop_edits(app: BridgeApp, written: str) -> list[tuple[int, int, int, int]]:
    """The in-place edits (``a=f,r=1``) of the backdrop's image in ``written``: x, y, width, height."""
    image = app.glass.layers[MAIN].image_id
    out = []
    for keys in re.findall(r"\x1b_G([^;\x1b]*)", written):
        kv = dict(pair.split("=", 1) for pair in keys.split(",") if "=" in pair)
        if kv.get("a") == "f" and kv.get("r") == "1" and kv.get("i") == str(image):
            out.append((int(kv.get("x", 0)), int(kv.get("y", 0)), int(kv["s"]), int(kv["v"])))
    return out


def flourishes(app: BridgeApp, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Record every flourish the app plays (and still play it)."""
    played: list[tuple[str, Any]] = []
    real = app.glass.flourish

    def spy(name: str, at: Any = None, to: Any = None, color: Any = None) -> bool:
        played.append((name, at))
        return real(name, at=at, to=to, color=color)

    monkeypatch.setattr(app.glass, "flourish", spy)
    return played


async def test_stations_get_sprites(tmp_path: Path, output: Output) -> None:
    app, _, _ = fake_app(tmp_path)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        keys = station_keys(app)
        for station in app.deck.stations:
            lamp = lamp_color(station.view.lamp, station.theme_palette)
            assert station.sprite_key(lamp) in keys, station.view
            assert PLACEHOLDER in station.render_line(0).text, "the art is a placeholder sprite"
        poses = {s.view.pose for s in app.deck.stations}
        assert {"waving", "jolt", "empty"} <= poses, poses
        assert any(k[0] == "bridge-viewscreen" for k in app.glass.sprites)
        assert any(k[0] == "bridge-chair" and k[2] == 2 for k in app.glass.sprites), "two crew wait on the chair"
        api = station_of(app, "fake-api-gateway")
        rows = api.art[1]
        assert "api-gateway" in api.render_line(rows).text and "allow?" in api.render_line(rows + 1).text
        assert len(keys) <= 6, "one sprite per station, no leftovers"
    model = output.model()
    assert model.errors == [] and not model.images, "a clean protocol, and every image deleted on exit"


async def test_a_state_change_swaps_the_sprite_key(tmp_path: Path, output: Output) -> None:
    app, fake, _ = fake_app(tmp_path)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        api = station_of(app, "fake-api-gateway")
        palette = api.theme_palette
        before = api.sprite_key(lamp_color(api.view.lamp, palette))
        assert api.view.pose == "waving" and before in station_keys(app)
        fake.answer_all()
        app.poll()
        await idle(app, pilot)
        after = api.sprite_key(lamp_color(api.view.lamp, palette))
        assert api.view.pose == "typing" and after != before
        assert after[3] == "typing" and after[4] == palette.success, "a working lamp is the theme's success colour"
        await idle(app, pilot)
        keys = station_keys(app)
        assert after in keys and before not in keys, "the old pose's sprite is freed"
    assert output.model().errors == []


async def test_idle_is_silent(tmp_path: Path, output: Output) -> None:
    app, fake, clock = fake_app(tmp_path)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        fake.toggle_pause()
        app.poll()
        await idle(app, pilot)
        mark = output.mark()
        updates = [s.updates for s in app.deck.stations]
        title = app.query_one("#bridge").border_title
        for _ in range(6):
            clock.t += 1.0  # paused: the bridge's clock stands still
            app.poll()
            await settle(app, pilot)
        assert output.since(mark) == "", "no bytes while nothing changes"
        assert [s.updates for s in app.deck.stations] == updates, "no widget touched"
        assert app.query_one("#bridge").border_title == title


async def test_live_ages_change_only_at_minute_resolution(tmp_path: Path, output: Output) -> None:
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=5, seconds=10)  # the age says 5m for the next 50 seconds
    for sid, state in (("one", "working"), ("two", "done")):
        (tmp_path / f"{sid}.json").write_text(json.dumps({
            "session_id": sid, "state": state, "since": since.isoformat(), "updated": now.isoformat(),
            "started": since.isoformat(), "cwd": f"/work/{sid}"}))
    app = BridgeApp(state_dir=tmp_path, poll=3600)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        assert app.bridge.fake is None and {s.view.age for s in app.deck.stations if s.view.project} == {"5m"}
        mark = output.mark()
        updates = [s.updates for s in app.deck.stations]
        for _ in range(4):
            app.poll()
            await settle(app, pilot)
        assert output.since(mark) == "" and [s.updates for s in app.deck.stations] == updates
        assert "live" in str(app.query_one("#keys").render())


async def test_red_alert(tmp_path: Path, output: Output, monkeypatch: pytest.MonkeyPatch) -> None:
    app, fake, _ = fake_app(tmp_path, at=1.5)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        panel = app.query_one("#bridge")
        played = flourishes(app, monkeypatch)
        assert not panel.has_class("-red-alert") and not app.deck.viewscreen.alert
        assert str(panel.border_title).startswith("BRIDGE")
        first = fake.make_wait()
        app.poll()
        await settle(app, pilot)
        assert not panel.has_class("-red-alert"), "one waiting is not an alert"
        assert played == [("pulsar", station_of(app, str(first)))], "a pulsar when a hand goes up"
        fake.make_wait()
        app.poll()
        await idle(app, pilot)
        assert panel.has_class("-red-alert") and app.deck.viewscreen.alert
        assert str(panel.border_title).startswith("RED ALERT") and "2 waiting" in str(panel.border_title)
        border, red = panel.styles.border_top[1], Color.parse(app.get_css_variables()["error"])
        assert max(abs(a - b) for a, b in zip(border.rgb, red.rgb, strict=True)) <= 2, "the border turns red"
        assert panel.styles.border_title_color.rgb == border.rgb
        assert any(k[0] == "bridge-viewscreen" and k[2] is True for k in app.glass.sprites)
        assert app.deck.chair.waiting == 2
        fake.answer_all()
        app.poll()
        await idle(app, pilot)
        assert not panel.has_class("-red-alert") and not app.deck.viewscreen.alert
        fake.make_error()
        app.poll()
        await settle(app, pilot)
        assert panel.has_class("-red-alert") and "1 error" in str(panel.border_title)
        assert len(played) == 2, "an error plays no pulsar"
    assert output.model().errors == []


async def test_the_fake_script_runs_clean(tmp_path: Path, output: Output) -> None:
    app, fake, clock = fake_app(tmp_path, at=0.0)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        seen: set[str] = set()
        most = 0
        for _ in range(45):  # one 40-minute loop and a bit, a virtual minute per poll
            clock.t += 3.0
            app.poll()
            await settle(app, pilot, rounds=3)
            seen |= {s.view.pose for s in app.deck.stations}
            most = max(most, len(app.glass.sprites))
        await idle(app, pilot)
        assert seen >= {"seated", "typing", "done", "hand_up", "waving", "standing", "jolt", "tea", "dozing",
                        "empty"}, seen
        assert app.glass.errors == 0 and app.glass.active and app.poll_errors == []
        assert most <= 16, "sprites are freed as poses change"
    model = output.model()
    assert model.errors == [] and not model.images


async def test_keys_drive_the_fake(tmp_path: Path, output: Output) -> None:
    app, fake, _ = fake_app(tmp_path, at=1.5)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        before = {s.id for s in app.bridge.sessions if s.present}
        await pilot.press("n")
        new = {s.id for s in app.bridge.sessions if s.present} - before
        assert len(new) == 1
        assert station_of(app, new.pop()).view.pose == "seated"
        await pilot.press("w")
        assert any(s.view.pose == "hand_up" for s in app.deck.stations)
        await pilot.press("a")
        assert not any(s.view.waiting for s in app.deck.stations)
        await pilot.press("e")
        assert any(s.view.pose == "jolt" for s in app.deck.stations)
        await pilot.press("x")
        assert any(s.view.leaving for s in app.deck.stations), "the newest leaves: shown briefly, then gone"
        await pilot.press("space")
        assert fake.paused and "PAUSED" in str(app.query_one("#keys").render())
        await settle(app, pilot)
    assert output.model().errors == []


async def test_a_failing_poll_is_reported_and_polling_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, fake, _ = fake_app(tmp_path)
    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        polls = app.bridge.polls

        def broken(now: datetime) -> list[Any]:
            raise OSError("the disk went away")

        monkeypatch.setattr(app.bridge.reader, "read", broken)
        app.poll()
        app.poll()
        assert app.poll_errors == ["OSError: the disk went away"] * 2 and app.is_running
        monkeypatch.undo()
        app.poll()
        assert app.bridge.polls == polls + 1 and len(app.poll_errors) == 2


@pytest.mark.parametrize("size", SIZES)
async def test_the_arc_fits_every_size(tmp_path: Path, size: tuple[int, int]) -> None:
    app, _, _ = fake_app(tmp_path)
    async with app.run_test(size=size) as pilot:
        await start(app, pilot)
        deck = app.query_one(Deck)
        assert deck.plan is not None
        boxes = [w.region for w in (*deck.stations, deck.chair)]
        for i, a in enumerate(boxes):
            assert deck.region.contains_region(a), (a, deck.region)
            assert not any(a.overlaps(b) for b in boxes[i + 1:])
            assert a.y >= deck.viewscreen.region.bottom
        expected = {(70, 22): (7, 3), (90, 28): (14, 6), (140, 45): (20, 8)}[size]
        assert deck.plan.tier.art == expected


def test_stations_stay_crisp_on_the_real_cell_size() -> None:
    """Glass draws a sprite at most SPRITE_MAX_PX a side and the terminal stretches the rest: on Retina
    cells a big station would only be a blurrier copy of a smaller one, so the deck picks a smaller one."""
    assert plan_for(90, 28, cell=(9, 19)).tier.art == (14, 6)
    assert plan_for(90, 28, cell=None).tier.art == (14, 6), "glass off: as before"
    retina = plan_for(90, 28, cell=(18, 38))
    assert retina.tier.art == (10, 4), "180 x 152 px: nothing stretched"
    assert plan_for(140, 45, cell=(18, 38)).tier.art == (10, 4)
    assert plan_for(90, 28, cell=(40, 80)).tier == TIERS[-1], "huge cells: the smallest, stretched"


def test_plan_falls_back_to_the_smallest_tier() -> None:
    plan = plan_for(30, 10)
    assert plan.tier == TIERS[-1] and len(plan.stations) == 6
    assert all(isinstance(r, Rect) for r in plan.stations)


def live_session(state_dir: Path, sid: str, state: str, agent: str, project: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    (state_dir / f"{sid}.json").write_text(json.dumps({
        "session_id": sid, "agent": agent, "state": state, "since": now, "updated": now, "started": now,
        "cwd": f"/work/{project}", "model": "gpt-5-codex" if agent == "codex" else "claude-opus-4"}))


@pytest.mark.parametrize("size", SIZES)
async def test_a_codex_session_is_tagged_on_its_station(tmp_path: Path, output: Output, size: tuple[int, int]) -> None:
    live_session(tmp_path, "a", "working", "claude", "api")
    live_session(tmp_path, "b", "working", "codex", "web")
    app = BridgeApp(state_dir=tmp_path, poll=3600)
    async with app.run_test(size=size) as pilot:
        await start(app, pilot)
        claude, codex = station_of(app, "a"), station_of(app, "b")
        assert claude.view.agent == "" and codex.view.agent == "codex"
        rows = codex.art[1]
        text = codex.render_line(rows + 1).text
        tag = {(70, 22): "", (90, 28): " cx", (140, 45): " codex"}[size]  # stations 13, 15 and 22 cells wide
        assert text.strip() == "working <1m" + tag, text
        assert "cx" not in claude.render_line(rows + 1).text and "codex" not in claude.render_line(rows + 1).text
        if codex.label_rows > 2:
            assert "gpt-5-codex" in codex.render_line(rows + 2).text and "opus-4" in claude.render_line(rows + 2).text
    assert output.model().errors == []


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_print_hooks_prints_this_checkouts_registration(agent: str, tmp_path: Path,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    """``--print-hooks`` prints JSON on stdout (where to put it on stderr) and changes nothing: every event the
    hook understands from that agent, tool events with the every-tool matcher, all running this checkout's
    ``hook.py`` with ``--agent``. The printed command works as it is."""
    main(["--print-hooks", agent])
    out, err = capsys.readouterr()
    hooks = json.loads(out)["hooks"]
    assert list(hooks) == list(hook.EVENTS[agent]) and "/hooks" in err
    commands = set()
    for name, groups in hooks.items():
        [group] = groups
        assert group.get("matcher") == ("*" if name in hook.TOOL_EVENTS else None), name
        [entry] = group["hooks"]
        assert entry["type"] == "command"
        commands.add(entry["command"])
    [command] = commands
    argv = shlex.split(command)
    assert argv == [sys.executable, str(Path(hook.__file__).resolve()), "--agent", agent]
    assert Path(argv[1]).is_absolute() and Path(argv[1]).exists()
    event = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "cwd": "/work/api"}
    done = subprocess.run(argv, input=json.dumps(event).encode(), capture_output=True, timeout=10,
                          env={**os.environ, "BRIDGE_STATE": str(tmp_path)})
    assert done.returncode == 0 and done.stdout == done.stderr == b""
    assert json.loads((tmp_path / "s1.json").read_text())["agent"] == agent


async def test_plain_mode_draws_text(tmp_path: Path) -> None:
    app, _, _ = fake_app(tmp_path, glass=False)
    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        assert not app.glass.active and not app.glass.sprites
        api = station_of(app, "fake-api-gateway")
        text = "\n".join(api.render_line(y).text for y in range(api.size.height))
        assert "▲▲" in text and "api-gateway" in text and "allow?" in text and PLACEHOLDER not in text
        assert "RED ALERT" in "\n".join(app.deck.viewscreen.render_line(y).text for y in range(4))


# -- the room ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES)
async def test_the_room_is_light_in_the_backdrop(tmp_path: Path, output: Output, size: tuple[int, int]) -> None:
    """The deck lights the room in glass's backdrop: the well's rings, the rail, a pool behind each console
    in its lamp's colour, the chair's pool and the viewscreen's pane, each with an ident of its own, over the
    panel's glass, inside the deck."""
    app, _, _ = fake_app(tmp_path, at=1.5)
    async with app.run_test(size=size) as pilot:
        await start(app, pilot)
        deck = app.deck
        found = room_items(app)
        key = str(id(deck))
        rail = found[f"bridge-rail:{key}"]
        assert isinstance(rail, Arc) and rail.beads > 0 and not rail.dash
        rings = [i for i in found.values() if i.ident.startswith("bridge-ring")]
        assert rings and all(isinstance(r, Arc) and r.dash > 0 for r in rings), "dashed rings in the well"
        assert isinstance(found[f"bridge-pane:{key}"], Pane)
        r = deck.region
        assert all(item.depth >= 1 and item.clip is None for item in found.values()), "over the panel's glass"
        grid, p = app.glass.grid, app.query_one("#bridge").region
        assert grid is not None
        x0, y0, x1, y1 = grid.box((p.x, p.y, p.width, p.height))
        for item in found.values():
            box = item.bbox(grid)
            assert x0 <= box[0] and y0 <= box[1] and box[2] <= x1 and box[3] <= y1, (item.ident, box)
        palette = deck.theme_palette
        for n, station in enumerate(deck.stations):
            pool = found[f"bridge-pool{n}:{key}"]
            assert isinstance(pool, Pool)
            assert pool.color == lamp_color(station.view.lamp, palette)
            assert pool.strength == POOL_STRENGTH[station.view.lamp]
            assert r.x <= pool.glow[0] - pool.glow[2] and pool.glow[0] + pool.glow[2] <= r.x + r.width + 1
        assert isinstance(found[f"bridge-chair-pool:{key}"], Pool)
        vs = deck.viewscreen.region
        pane = found[f"bridge-pane:{key}"]
        assert isinstance(pane, Pane) and pane.rect == (vs.x, vs.y, vs.width, vs.height), "around the screen's cells"
        assert rail.c1 != palette.error, "calm"
    assert output.model().errors == []


async def test_a_state_change_redraws_only_its_pool(tmp_path: Path, output: Output) -> None:
    """A hand goes up: the station's view changes, glass collects the deck's items again in that frame, and
    only that station's pool and the chair's pool change, and its path of light to the chair appears: the
    backdrop is edited in place over their small boxes, never drawn again whole."""
    app, fake, _ = fake_app(tmp_path, at=1.5)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        assert not app.bridge.alert
        before = room_items(app)
        mark = output.mark()
        sid = fake.make_wait()
        app.poll()
        await idle(app, pilot)
        after = room_items(app)
        station = station_of(app, str(sid))
        key = str(id(app.deck))
        changed = {ident for ident in before.keys() | after.keys() if after.get(ident) != before.get(ident)}
        path = f"bridge-path{station.index}:{key}"
        assert changed == {f"bridge-pool{station.index}:{key}", f"bridge-chair-pool:{key}", path}, changed
        assert path not in before and isinstance(after[path], LightPath)
        pool = after[f"bridge-pool{station.index}:{key}"]
        assert isinstance(pool, Pool) and pool.color == station.theme_palette.warning
        edits = backdrop_edits(app, output.since(mark))
        layer = app.glass.layers[MAIN]
        whole = layer.rect[2] * layer.rect[3]
        assert edits, "edited in place"
        assert sum(w * h for _, _, w, h in edits) < 0.3 * whole, edits  # glass redraws a layer whole past 0.5
        fake.answer_all()
        app.poll()
        await idle(app, pilot)
        assert path not in room_items(app), "the path goes with the wait"
    assert output.model().errors == []


async def test_the_room_turns_red_for_red_alert_and_only_then(tmp_path: Path, output: Output) -> None:
    app, fake, _ = fake_app(tmp_path, at=1.5)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        calm = room_items(app)
        lit = {ident for ident, item in calm.items() if isinstance(item, (Arc, Pane))}
        fake.make_wait()  # one hand up: a pool and the chair's pool change, the rail doesn't
        app.poll()
        await idle(app, pilot)
        one = room_items(app)
        assert not app.bridge.alert and all(one[i] == calm[i] for i in lit)
        fake.make_wait()  # two: red alert
        app.poll()
        mark = output.mark()
        await idle(app, pilot)
        alert = room_items(app)
        error = app.deck.theme_palette.error
        assert app.bridge.alert and all(alert[i] != one[i] and alert[i].c1 == error for i in lit)
        assert "\x1b_G" in output.since(mark), "the backdrop was drawn again"
        fake.answer_all()
        app.poll()
        await idle(app, pilot)
        back = room_items(app)
        assert not app.bridge.alert and all(back[i] == calm[i] for i in lit)
    assert output.model().errors == []


async def test_the_room_keeps_an_idle_bridge_silent(tmp_path: Path, output: Output) -> None:
    """Rule 9 with the room in the backdrop, in red alert and after resizes: nothing is sent while nothing
    changes. (The script starts in red alert; raising a hand here would play a pulsar for a moment.)"""
    app, fake, clock = fake_app(tmp_path)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        fake.toggle_pause()
        app.poll()
        await idle(app, pilot)
        assert next(i for i in room_items(app).values() if isinstance(i, Arc)).c1 == app.deck.theme_palette.error
        for size in [None, (120, 36), (70, 22)]:
            if size is not None:
                await pilot.resize_terminal(*size)
                await idle(app, pilot)
            items = room_items(app)
            mark = output.mark()
            for _ in range(6):
                clock.t += 1.0
                app.poll()
                await settle(app, pilot)
            assert output.since(mark) == "", f"no bytes while idle at {size}"
            assert room_items(app) == items
    assert output.model().errors == []


# -- the viewscreen at its full size --------------------------------------------------------------------


@pytest.mark.parametrize("cell", [9.0, 10.0, 18.0, 8.6])
@pytest.mark.parametrize("cols", [1, 20, 21, 42, 55, 86, 118])
def test_tiles_cover_the_screen_each_within_the_sprite_limit(cols: int, cell: float) -> None:
    runs = tile_columns(cols, cell)
    assert runs[0][0] == 0 and sum(w for _, w in runs) == cols
    assert all(a + w == b for (a, w), (b, _) in zip(runs, runs[1:], strict=False)), "edge to edge"
    assert all(w * cell <= SPRITE_MAX_PX for _, w in runs), "each drawn at its full size"
    assert max(w for _, w in runs) - min(w for _, w in runs) <= 1, "as even as can be"
    assert len(runs) == max(1, -(-cols // int(SPRITE_MAX_PX // cell))), "no more tiles than needed"


def test_steady_keeps_twinkles_and_stills_a_pulse() -> None:
    """Tiles are separate terminal animations that start as each lands: a change across the whole picture
    would show their seams, so it is shown as a still; local twinkles stay animated."""
    rng = np.random.default_rng(1)
    base = rng.integers(0, 255, (40, 200, 4), dtype=np.uint8)
    twinkle = base.copy()
    twinkle[5:7, 10:12, :3] = 255 - twinkle[5:7, 10:12, :3]
    pulse = (base.astype(np.int16) * 3 // 4).astype(np.uint8)
    assert len(steady([base, twinkle, base], 500)[0]) == 3
    frames, gap = steady([base, pulse, twinkle], 500)
    assert len(frames) == 1 and frames[0] is base and gap == 500


@pytest.mark.parametrize("alert", [False, True])
def test_the_tiles_put_back_together_are_the_whole_screen(alert: bool) -> None:
    """Every tile is a crop of one picture of the whole screen: side by side they are that picture, pixel for
    pixel, so no seam shows between them. The picture's own outer ring is transparent (rule 17 for the screen
    as a whole; a tile's inner edges meet its neighbour's), and its frames are steady enough to animate."""
    pack = crew.load()
    cols, rows, cell = 55, 4, (9.0, 19.0)
    width, height = round(cols * cell[0]), round(rows * cell[1])
    frames, _gap = PICTURES.get(pack, alert, width, height)
    assert frames and all(f.shape == (height, width, 4) for f in frames)
    for frame in frames:
        a = frame[..., 3]
        assert a[0].max() == 0 and a[-1].max() == 0 and a[:, 0].max() == 0 and a[:, -1].max() == 0
        changed = (np.abs(frame[..., :3].astype(int) - frames[0][..., :3].astype(int)).max(axis=2) > 8).mean()
        assert changed <= STEADY
    tiles = []
    for x0, w in tile_columns(cols, cell[0]):
        px0, px1 = round(x0 * cell[0]), round((x0 + w) * cell[0])
        tile = tile_frames(frames, px0, px1, px1 - px0, height)
        tiles.append([np.frombuffer(f, np.uint8).reshape(height, px1 - px0, 4) for f in tile])
    for n, frame in enumerate(frames):
        assert np.array_equal(np.concatenate([t[n] for t in tiles], axis=1), frame)
    assert PICTURES.get(pack, alert, width, height)[0] is frames, "made once, shared by every tile"


async def test_the_viewscreen_is_drawn_at_its_full_size(tmp_path: Path, output: Output) -> None:
    """The viewscreen is column tiles, each a sprite at the terminal's own pixel size (none capped and
    stretched), all on every line of the widget; red alert swaps them for red ones and the old ones are freed
    (rule 21)."""
    app, fake, _ = fake_app(tmp_path, at=1.5)
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)
        screen = app.query_one(Viewscreen)
        cw, ch = app.glass.cell_pixels()
        width, height = screen.size

        def tiles(alert: bool) -> list[tuple[Any, ...]]:
            return [k for k in app.glass.sprites if k[0] == "bridge-viewscreen" and k[2] is alert]

        calm = tiles(False)
        runs = tile_columns(width, cw)
        assert len(calm) == len(runs) > 1 and not tiles(True)
        for key in calm:
            cols, rows, w, h = key[-4:]
            assert (rows, w, h) == (height, round(cols * cw), round(rows * ch)), "at its full pixel size"
        ids = {app.glass.sprites[k].image_id for k in calm}
        for y in range(height):
            assert placeholder_ids(screen.render_line(y)) == ids
        fake.make_wait()
        fake.make_wait()
        app.poll()
        await idle(app, pilot)
        assert app.bridge.alert and len(tiles(True)) == len(runs) and not tiles(False), "the calm tiles are freed"
        red = {app.glass.sprites[k].image_id for k in tiles(True)}
        assert placeholder_ids(screen.render_line(1)) == red
    assert output.model().errors == []
