"""``textual_orbital.sounds`` without an app: packs, players, cooldowns, groups,
the window that keeps one sound per moment, mute, quiet hours, loops and
failures. No test starts a real player: each passes a ``spawn`` recorder
(the ``no_sound`` fixture fails any test that doesn't)."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import time as clock_time
from pathlib import Path

import pytest

from textual_orbital import CUES, SoundPack, Sounds
from textual_orbital.sounds import parse_quiet_hours, player_argv, silenced


def make_pack(folder: Path, cues: list[str] | None = None, volume: float = 1.0,
              volumes: dict[str, float] | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    sounds: dict[str, object] = {}
    for cue in cues or [*CUES, "comet", "room"]:
        (folder / f"{cue}.wav").write_bytes(b"RIFF....WAVE")
        sounds[cue] = {"file": f"{cue}.wav", "volume": volumes[cue]} if volumes and cue in volumes else f"{cue}.wav"
    (folder / "pack.json").write_text(json.dumps({"name": "test", "volume": volume, "sounds": sounds}))
    return folder


class Proc:
    """A started player: ends at once, or, with ``hold``, when it is released or stopped."""

    def __init__(self, argv: list[str], code: int = 0, hold: threading.Event | None = None) -> None:
        self.argv, self.code, self.hold = argv, code, hold
        self.terminated = False

    def wait(self, timeout: float | None = None) -> int:
        if self.hold is not None and not self.terminated and not self.hold.wait(timeout):
            raise subprocess.TimeoutExpired(self.argv, timeout or 0)
        return -15 if self.terminated else self.code

    def terminate(self) -> None:
        self.terminated = True
        if self.hold is not None:
            self.hold.set()

    def kill(self) -> None:
        self.terminate()


class Spawn:
    """Records every player started. With ``holding``, each plays until :meth:`release` or until it is stopped."""

    def __init__(self, code: int = 0, holding: bool = False, error: BaseException | None = None) -> None:
        self.code, self.holding, self.error = code, holding, error
        self.procs: list[Proc] = []

    def __call__(self, argv: list[str]) -> Proc:
        if self.error is not None:
            raise self.error
        proc = Proc(argv, self.code, threading.Event() if self.holding else None)
        self.procs.append(proc)
        return proc

    def release(self) -> None:
        self.holding = False
        for proc in self.procs:
            if proc.hold is not None:
                proc.hold.set()

    @property
    def files(self) -> list[str]:
        return [Path(p.argv[-1]).stem for p in self.procs]


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def sounds(tmp_path: Path, spawn: Spawn | None = None, **kwargs: object) -> Sounds:
    kwargs.setdefault("player", ["afplay"])
    return Sounds(make_pack(tmp_path / "pack"), spawn=spawn or Spawn(), **kwargs)  # type: ignore[arg-type]


def until(condition: Callable[[], object], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


# -- packs -----------------------------------------------------------------------------------


def test_a_pack_reads_names_objects_volumes_and_ignores_notes(tmp_path: Path) -> None:
    folder = make_pack(tmp_path / "p", ["attention", "comet"], volume=0.5, volumes={"comet": 0.6})
    data = json.loads((folder / "pack.json").read_text())
    data["sounds"]["comet"]["made"] = {"prompt": "a whoosh"}  # yours, ignored
    (folder / "pack.json").write_text(json.dumps(data))
    pack = SoundPack.load(folder / "pack.json")  # the manifest works as well as the folder
    assert pack.name == "test" and pack.volume == 0.5 and "attention" in pack and "toast" not in pack
    assert pack.sounds["comet"].volume == 0.6 and pack.sounds["comet"].file == (folder / "comet.wav").resolve()


@pytest.mark.parametrize(("change", "message"), [
    (lambda d: d.update(sounds=[]), "needs a \"sounds\" object"),
    (lambda d: d["sounds"].update(attention="missing.wav"), "attention: no file 'missing.wav'"),
    (lambda d: d["sounds"].update(attention={"file": "attention.wav", "volume": 2}), "volume must be"),
    (lambda d: d["sounds"].update(attention=3), "a file name, or an object"),
    (lambda d: d.update(volume="loud"), "volume must be"),
])
def test_a_broken_pack_says_what_is_wrong(tmp_path: Path, change: Callable[[dict[str, object]], None],
                                          message: str) -> None:
    folder = make_pack(tmp_path / "p", ["attention"])
    data = json.loads((folder / "pack.json").read_text())
    change(data)
    (folder / "pack.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match=message.replace("(", r"\(")):
        SoundPack.load(folder)


def test_a_pack_that_isnt_json_or_isnt_there(tmp_path: Path) -> None:
    (tmp_path / "pack.json").write_text("{nope")
    with pytest.raises(ValueError, match="not JSON"):
        SoundPack.load(tmp_path)
    with pytest.raises(OSError):
        SoundPack.load(tmp_path / "nowhere")


# -- players ---------------------------------------------------------------------------------


def test_each_player_gets_the_volume_its_own_way(tmp_path: Path) -> None:
    f = tmp_path / "a.wav"
    assert player_argv(["/usr/bin/afplay"], f, 0.5) == ["/usr/bin/afplay", "-v", "0.500", str(f)]
    assert player_argv(["pw-play"], f, 0.25) == ["pw-play", "--volume", "0.250", str(f)]
    assert player_argv(["paplay"], f, 0.5) == ["paplay", "--volume=32768", str(f)]
    assert player_argv(["aplay", "-q"], f, 0.5) == ["aplay", "-q", str(f)]


def test_the_environment_can_silence_every_app() -> None:
    assert silenced({"TEXTUAL_ORBITAL_SOUND": "off"}) and silenced({"TEXTUAL_ORBITAL_SOUND": " No "})
    assert not silenced({}) and not silenced({"TEXTUAL_ORBITAL_SOUND": "on"})
    assert Sounds(None, player=["afplay"]).silenced, "the no_sound fixture sets it: no real player in tests"


def test_a_cue_plays_at_the_volumes_multiplied(tmp_path: Path) -> None:
    spawn = Spawn()
    folder = make_pack(tmp_path / "p", ["attention", "comet"], volume=0.5, volumes={"comet": 0.6})
    s = Sounds(folder, player=["afplay"], spawn=spawn, volume=0.5)
    assert s.enabled and s.play("comet") and not s.play("toast"), "toast isn't in this pack: silent"
    until(lambda: spawn.procs)
    assert spawn.procs[0].argv[:3] == ["afplay", "-v", "0.150"] and list(s.played) == ["comet"]
    assert s.wait_idle()


def test_without_a_pack_or_a_player_nothing_plays(tmp_path: Path) -> None:
    spawn = Spawn()
    assert not Sounds(None, player=["afplay"], spawn=spawn).play("attention")
    s = Sounds(make_pack(tmp_path / "p"), spawn=spawn)
    s.player = None
    assert not s.enabled and not s.play("attention") and not spawn.procs


# -- one sound per moment ------------------------------------------------------------------


def test_a_cooldown_per_group_unless_the_new_cue_matters_more(tmp_path: Path) -> None:
    clock = Clock()
    s = sounds(tmp_path, clock=clock)
    assert s.play("attention")
    assert not s.play("attention"), "the same cue again at once"
    assert not s.play("toast"), "a toast means the same as the chime: its group is cooling down"
    assert s.play("transmission"), "a popup matters more: it sounds anyway"
    assert s.play("comet") and s.play("resolved"), "other groups have cooldowns of their own"
    clock.t += 3.5
    assert not s.play("attention"), "needs-you cools down for 10 s"
    assert s.play("comet"), "the rest for 3 s"
    clock.t += 7
    assert s.play("attention")
    assert list(s.played) == ["attention", "transmission", "comet", "resolved", "comet", "attention"]
    assert s.wait_idle()


def test_cues_in_one_window_collapse_into_the_most_important(tmp_path: Path) -> None:
    waiting: list[tuple[float, Callable[[], None]]] = []
    s = sounds(tmp_path, schedule=lambda delay, callback: waiting.append((delay, callback)))
    assert s.play("comet") and s.play("attention") and s.play("toast") and s.play("resolved")
    assert len(waiting) == 1 and waiting[0][0] == s.window and not s.played, "one flush, nothing yet"
    waiting[0][1]()
    assert list(s.played) == ["attention"], "the most important of the moment"
    s.flush()
    assert list(s.played) == ["attention"], "flushing again plays nothing"
    assert s.wait_idle()


def test_mute_and_quiet_hours(tmp_path: Path) -> None:
    at = [clock_time(23, 30)]
    s = sounds(tmp_path, quiet_hours=(clock_time(22), clock_time(7)), wall_clock=lambda: at[0])
    assert s.quiet() and not s.play("attention"), "quiet hours run over midnight"
    at[0] = clock_time(6, 59)
    assert s.quiet()
    at[0] = clock_time(7, 0)
    assert not s.quiet() and s.play("attention")
    s.muted = True
    assert not s.play("transmission") and not s.loop("room")
    s.muted = False
    assert s.play("transmission")
    assert parse_quiet_hours("22-7") == (clock_time(22), clock_time(7))
    assert parse_quiet_hours("22:30 - 07:15") == (clock_time(22, 30), clock_time(7, 15))
    assert parse_quiet_hours("18-24") == (clock_time(18), clock_time(0))
    with pytest.raises(ValueError, match="22-7"):
        parse_quiet_hours("evenings")
    assert s.wait_idle()


def test_at_most_two_at_once(tmp_path: Path) -> None:
    spawn = Spawn(holding=True)
    s = sounds(tmp_path, spawn=spawn)
    assert s.play("comet") and s.play("attention")
    until(lambda: len(spawn.procs) == 2)
    assert not s.play("trouble"), "two are playing: dropped"
    spawn.release()
    assert s.wait_idle()
    assert s.play("trouble")
    assert s.wait_idle()


# -- loops -----------------------------------------------------------------------------------


def test_a_loop_plays_until_stopped_and_muting_stops_it(tmp_path: Path) -> None:
    spawn = Spawn()  # each play ends at once: the loop starts it again
    s = sounds(tmp_path, spawn=spawn)
    assert s.loop("room") and s.looping == "room"
    until(lambda: len(spawn.procs) >= 3)
    s.stop_loop()
    assert s.looping is None and s.wait_idle()
    spawn.holding = True
    assert s.loop("room")
    until(lambda: spawn.procs and spawn.procs[-1].hold is not None)
    s.muted = True
    assert s.looping is None and s.wait_idle() and spawn.procs[-1].terminated, "muting stops it at once"


def test_a_loop_doesnt_take_a_place(tmp_path: Path) -> None:
    spawn = Spawn(holding=True)
    s = sounds(tmp_path, spawn=spawn)
    assert s.loop("room") and s.play("comet") and s.play("attention"), "two sounds over the room tone"
    until(lambda: len(spawn.procs) == 3)
    s.stop()
    assert s.wait_idle()


def test_stop_ends_everything(tmp_path: Path) -> None:
    spawn = Spawn(holding=True)
    s = sounds(tmp_path, spawn=spawn)
    s.play("attention")
    s.loop("room")
    until(lambda: len(spawn.procs) == 2)
    s.stop()
    assert s.wait_idle() and all(p.terminated for p in spawn.procs) and s.looping is None


# -- failures --------------------------------------------------------------------------------


def test_a_player_that_cant_start_turns_sound_off_and_is_reported_once(tmp_path: Path) -> None:
    errors: list[tuple[str, BaseException]] = []
    s = sounds(tmp_path, spawn=Spawn(error=FileNotFoundError("afplay")),
               on_error=lambda where, error: errors.append((where, error)))
    s.play("attention")
    assert s.wait_idle() and s.broken is not None and not s.enabled
    assert not s.play("transmission") and [w for w, _ in errors] == ["sound player"]


def test_a_sound_the_player_cant_play_is_reported_once_per_cue(tmp_path: Path) -> None:
    errors: list[str] = []
    clock = Clock()
    s = sounds(tmp_path, spawn=Spawn(code=1), clock=clock, on_error=lambda where, error: errors.append(where))
    s.play("comet")
    assert s.wait_idle()
    clock.t += 5
    s.play("comet")
    assert s.wait_idle()
    assert errors == ["sound 'comet'"] and s.enabled, "one bad file doesn't turn sound off"
    s.loop("room")
    assert s.wait_idle() and s.looping is None, "a loop whose file fails stops trying"
