"""The bridge's sounds: which cues a change between two reads plays (``cues_between``), and the app playing them,
its room tone and its mute key. No real player: a recorder stands in (the ``no_sound`` fixture)."""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bridge.app import CUES, ROOM, BridgeApp, Heard, cues_between, main
from bridge.fake import FakeBridge
from textual_orbital import Sounds

T0 = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)


def h(state: str = "working", pose: str = "typing", ensigns: int = 0, present: bool = True) -> Heard:
    return Heard(state, pose, ensigns, present)


@pytest.mark.parametrize(("before", "after", "cues"), [
    ({}, {"a": h()}, ["crew-in"]),
    ({}, {"a": h("waiting", "hand_up")}, ["hand-up", "crew-in"]),
    ({"a": h()}, {"a": h("waiting", "hand_up")}, ["hand-up"]),
    ({"a": h("waiting", "hand_up")}, {"a": h("waiting", "waving")}, ["escalate"]),
    ({"a": h("waiting", "waving")}, {"a": h("waiting", "standing")}, ["escalate"]),
    ({"a": h("waiting", "standing")}, {"a": h("waiting", "standing")}, []),
    ({"a": h()}, {"a": h("done", "done")}, ["done"]),
    ({"a": h("done", "done")}, {"a": h("done", "dozing")}, []),
    ({"a": h()}, {"a": h("error", "jolt")}, ["error"]),
    ({"a": h()}, {"a": h("compacting", "tea")}, []),
    ({"a": h()}, {"a": h(ensigns=2)}, ["ensign-out"]),
    ({"a": h(ensigns=2)}, {"a": h(ensigns=1)}, ["ensign-in"]),
    ({"a": h()}, {"a": h("ended", "empty", present=False)}, ["crew-out"]),
    ({"a": h("ended", "empty", present=False)}, {}, []),
    ({"a": h()}, {}, ["crew-out"]),
])
def test_what_each_change_sounds_like(before: dict[str, Heard], after: dict[str, Heard], cues: list[str]) -> None:
    assert cues_between(before, after, False, False) == cues


def test_red_alert_comes_first() -> None:
    before = {"a": h("waiting", "hand_up"), "b": h()}
    after = {"a": h("waiting", "hand_up"), "b": h("waiting", "hand_up")}
    assert cues_between(before, after, False, True) == ["red-alert", "hand-up"]
    assert cues_between(after, after, True, True) == [], "staying red is quiet"
    assert all(cue in CUES for cue in ("red-alert", "hand-up", "error", "escalate", "done", "crew-in", "crew-out",
                                       "ensign-out", "ensign-in"))


class Proc:
    def __init__(self, hold: threading.Event | None = None) -> None:
        self.hold = hold

    def wait(self, timeout: float | None = None) -> int:
        if self.hold is not None and not self.hold.wait(timeout):
            raise subprocess.TimeoutExpired("room", timeout or 0)
        return 0

    def terminate(self) -> None:
        if self.hold is not None:
            self.hold.set()

    def kill(self) -> None:
        self.terminate()


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def recorder(folder: Path, clock: Clock | None = None) -> Sounds:
    """Sounds over a pack of every bridge cue and the room tone; one sound at a time, recorded, never played.
    The room tone plays until it is stopped; the rest end at once."""
    folder.mkdir()
    for cue in (*CUES, ROOM):
        (folder / f"{cue}.wav").write_bytes(b"RIFF")
    (folder / "pack.json").write_text(json.dumps({"sounds": {cue: f"{cue}.wav" for cue in (*CUES, ROOM)}}))
    return Sounds(folder, player=["afplay"], window=0, clock=clock or Clock(),
                  spawn=lambda argv: Proc(threading.Event() if argv[-1].endswith(f"{ROOM}.wav") else None))


def fake_app(tmp_path: Path, **kwargs: Any) -> tuple[BridgeApp, FakeBridge, Sounds, Clock]:
    """The bridge's demo, read only when a test says, with a recorder for its sounds and a clock for their
    cooldowns."""
    state = tmp_path / "state"
    state.mkdir()
    ticks = Clock()
    sounds = recorder(tmp_path / "pack", ticks)
    fake = FakeBridge(state, 20, clock=Clock(), start=T0, at=12.5)
    return BridgeApp(fake=fake, poll=3600, sounds=sounds, glass=False, **kwargs), fake, sounds, ticks


async def test_the_crew_is_heard_and_the_first_read_only_listens(tmp_path: Path) -> None:
    app, fake, sounds, ticks = fake_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.bridge.present and not sounds.played, "whoever is aboard at startup is no news"
        app.action_new()
        assert list(sounds.played) == ["crew-in"]
        app.action_end()
        assert list(sounds.played) == ["crew-in"], "crew-out shares crew-in's cooldown"
        ticks.t += 15  # past every cooldown
        app.action_new()
        assert list(sounds.played) == ["crew-in", "crew-in"]
        ticks.t += 15
        app.action_error()
        assert {"red-alert", "error"} & set(sounds.played)
        await pilot.press("m")
        assert sounds.muted
        ticks.t += 15
        before = len(sounds.played)
        app.action_new()
        assert len(sounds.played) == before, "muted"
    assert sounds.wait_idle()


async def test_the_room_tone_plays_while_the_window_has_focus(tmp_path: Path) -> None:
    app, fake, sounds, _ = fake_app(tmp_path, room=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert sounds.looping == ROOM
        app.app_focus = False
        await pilot.pause()
        assert sounds.looping is None, "in the background: quiet"
        app.app_focus = True
        await pilot.pause()
        assert sounds.looping == ROOM
        await pilot.press("m")
        assert sounds.looping is None and sounds.muted
        await pilot.press("m")
        assert sounds.looping == ROOM, "unmuting brings the room back"
    assert sounds.looping is None and sounds.wait_idle(), "quitting stops it"


def test_a_sound_pack_that_cant_be_read_stops_the_app_at_once(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--sounds", str(tmp_path / "nowhere")])
    assert stopped.value.code == 2
