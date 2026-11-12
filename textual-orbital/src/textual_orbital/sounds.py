"""Sound cues: a short sound when something happens, from a pack of your own files.

A **pack** is a folder with a ``pack.json`` and the sound files::

    {
      "name": "Starship computer",
      "volume": 1.0,
      "sounds": {
        "attention": "attention.wav",
        "comet": {"file": "comet.wav", "volume": 0.6},
        "room": {"file": "room.wav", "volume": 0.35}
      }
    }

A sound is a file name (relative to the folder) or an object with ``file`` and an
optional ``volume`` (0 to 1, times the pack's own ``volume``); any other keys are
yours (notes, where a sound came from) and are ignored. A cue the pack doesn't
name stays silent, so a pack can be as small as one sound. WAV plays everywhere;
macOS also plays MP3, AAC and AIFF.

:class:`Sounds` plays a pack. Nothing is decoded in Python: each sound goes to
the system's player (``afplay`` on macOS; ``pw-play``, ``paplay`` or ``aplay``
on Linux), started from a daemon thread that waits for it, so the UI thread
never blocks and no process is left behind. Without a player it stays silent.

What keeps sounds bearable in a window that is open all day:

* **One sound per moment.** With a ``schedule`` (``OrbitalApp`` gives it one),
  the cues asked for within ``window`` seconds, one fetch's worth, collapse into
  the most important one (:attr:`Sounds.PRIORITY`).
* **A cooldown** per cue, or per group of cues that mean the same thing
  (:attr:`Sounds.GROUPS`: a popup right after the "needs you" chime doesn't chime
  again), unless the new cue is more important than the last one.
* At most :attr:`Sounds.MAX_AT_ONCE` sounds at once, ``muted`` and ``quiet_hours``.
* ``TEXTUAL_ORBITAL_SOUND=off`` in the environment silences every app started
  there: test suites set it (rule 20 in ``AGENTS.md``).

The cues :class:`~textual_orbital.app.OrbitalApp` plays are in :data:`CUES`;
``OrbitalApp.flourish`` plays a sound named after the flourish (``comet``,
``supernova``...) while the window has focus. Apps play cues of their own with
``app.sounds.play(name)``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from datetime import time as clock_time
from pathlib import Path
from typing import Any, ClassVar, Protocol

#: The cues ``OrbitalApp`` plays, and when.
CUES: dict[str, str] = {
    "attention": "a new needs-attention item arrived (one sound per fetch, however many; none at startup)",
    "transmission": "an alert opened as a popup",
    "toast": "an alert showed as a toast",
    "resolved": "an alert closed itself: its condition went away",
    "clear": "a fetch emptied the needs-attention queue",
    "shipped": "an event of a winning kind arrived: merged, deployed or released",
    "trouble": "a source started failing",
}

#: ``TEXTUAL_ORBITAL_SOUND`` values that silence every app in the environment.
OFF = frozenset({"off", "0", "no", "false"})

#: Players tried in order, per platform.
PLAYERS: dict[str, tuple[str, ...]] = {"darwin": ("afplay",), "linux": ("pw-play", "paplay", "aplay")}

#: The longest a one-shot sound may play before its player is stopped (seconds).
LONGEST = 60.0


class Process(Protocol):
    """What :class:`Sounds` needs of a started player (``subprocess.Popen`` has it)."""

    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


def silenced(env: Mapping[str, str] | None = None) -> bool:
    """True when ``TEXTUAL_ORBITAL_SOUND`` turns sound off (``off``, ``0``, ``no``, ``false``)."""
    return (os.environ if env is None else env).get("TEXTUAL_ORBITAL_SOUND", "").strip().lower() in OFF


def find_player(platform: str | None = None) -> list[str] | None:
    """The system's command-line player for this platform, or None."""
    for name in PLAYERS.get(platform or sys.platform, ()):
        found = shutil.which(name)
        if found:
            return [found]
    return None


def player_argv(player: Sequence[str], path: Path, volume: float) -> list[str]:
    """The command that plays ``path`` at ``volume`` (0 to 1) with ``player``."""
    name = Path(player[0]).name
    if name == "afplay":
        return [*player, "-v", f"{volume:.3f}", str(path)]
    if name == "pw-play":
        return [*player, "--volume", f"{volume:.3f}", str(path)]
    if name == "paplay":
        return [*player, f"--volume={round(volume * 65536)}", str(path)]
    return [*player, str(path)]  # aplay and the rest: no volume of their own


def _popen(argv: list[str]) -> Process:
    """Start a player. Same process group as the app, so closing the terminal
    window (SIGHUP to the group) stops it too."""
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True)


@dataclass(frozen=True)
class Sound:
    """One sound of a pack: its file and its volume (0 to 1)."""

    file: Path
    volume: float = 1.0


def _volume(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError(f"{where}: volume must be a number from 0 to 1, not {value!r}")
    return float(value)


@dataclass(frozen=True)
class SoundPack:
    """A folder of sounds and its ``pack.json`` (the format is in this module's docstring)."""

    path: Path
    sounds: dict[str, Sound]
    name: str = ""
    volume: float = 1.0

    @classmethod
    def load(cls, path: str | Path) -> SoundPack:
        """Read the pack in folder ``path`` (or its ``pack.json``). Raises
        ValueError (or OSError) naming what is wrong: no ``pack.json``, bad JSON,
        a volume out of range, a file that isn't there."""
        folder = Path(path).expanduser()
        if folder.name == "pack.json":
            folder = folder.parent
        manifest = folder / "pack.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"{manifest}: not JSON ({error})") from None
        if not isinstance(data, dict) or not isinstance(data.get("sounds"), dict):
            raise ValueError(f"{manifest}: needs a \"sounds\" object (cue -> file)")
        volume = _volume(data.get("volume", 1.0), str(manifest))
        sounds: dict[str, Sound] = {}
        problems: list[str] = []
        for cue, entry in data["sounds"].items():
            spec = {"file": entry} if isinstance(entry, str) else entry
            if not isinstance(spec, dict) or not isinstance(spec.get("file"), str):
                problems.append(f"{cue}: a file name, or an object with \"file\"")
                continue
            try:
                sound_volume = _volume(spec.get("volume", 1.0), cue)
            except ValueError as error:
                problems.append(str(error))
                continue
            file = (folder / spec["file"]).resolve()
            if not file.is_file():
                problems.append(f"{cue}: no file {spec['file']!r}")
                continue
            sounds[str(cue)] = Sound(file, sound_volume)
        if problems:
            raise ValueError(f"{manifest}: " + "; ".join(problems))
        name = data.get("name")
        return cls(folder, sounds, name if isinstance(name, str) else folder.name, volume)

    def __contains__(self, cue: object) -> bool:
        return cue in self.sounds


def _in_hours(at: clock_time, hours: tuple[clock_time, clock_time]) -> bool:
    """Is ``at`` in ``[start, end)``? ``(22:00, 07:00)`` runs over midnight."""
    start, end = hours
    return start <= at < end if start <= end else at >= start or at < end


class Sounds:
    """Plays a :class:`SoundPack`'s cues in the background.

    Args:
        pack: The pack (or its folder); None is silent until ``pack`` is set.
        volume: The app's volume, 0 to 1, times the pack's and the sound's.
        cooldown: Seconds before the same cue, or a cue of the same group, sounds again
            (``COOLDOWNS`` overrides it per group), unless the new one is more important.
        window: With ``schedule``, cues asked for within this many seconds collapse into the
            most important one.
        quiet_hours: ``(start, end)`` local times with no sound, e.g. ``(time(22), time(7))``.
        player: The player command (``["afplay"]``); None finds the system's (:func:`find_player`).
        schedule: ``schedule(delay, callback)`` runs ``callback`` on your thread after ``delay``
            seconds (``OrbitalApp`` passes its ``set_timer``). None plays each cue at once.
        on_error: ``on_error(where, error)``, called from the player's thread when a player
            can't start or fails; the first failure to start turns sound off.
        spawn: Starts a player (``subprocess.Popen`` by default); tests pass a recorder.

    Call :meth:`play` and :meth:`loop` from one thread (the app's). ``played`` keeps
    the last cues that started, newest last.
    """

    #: Sounds playing at once at most (loops not counted); more are dropped.
    MAX_AT_ONCE: ClassVar[int] = 2
    #: How important each cue is; a cue that isn't listed is 10 (flourishes, your own).
    PRIORITY: ClassVar[dict[str, int]] = {"transmission": 90, "trouble": 80, "attention": 70, "toast": 60,
                                         "shipped": 50, "clear": 40, "resolved": 30}
    #: Cues that share a cooldown because they mean the same thing; a cue in no group is its own.
    GROUPS: ClassVar[dict[str, str]] = {"transmission": "needs-you", "toast": "needs-you",
                                        "attention": "needs-you"}
    #: Cooldowns per group that differ from ``cooldown`` (seconds).
    COOLDOWNS: ClassVar[dict[str, float]] = {"needs-you": 10.0}

    def __init__(self, pack: SoundPack | str | Path | None = None, *, volume: float = 1.0,
                 cooldown: float = 3.0, window: float = 0.15,
                 quiet_hours: tuple[clock_time, clock_time] | None = None,
                 player: Sequence[str] | None = None,
                 schedule: Callable[[float, Callable[[], None]], object] | None = None,
                 on_error: Callable[[str, BaseException], None] | None = None,
                 spawn: Callable[[list[str]], Process] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], clock_time] = lambda: datetime.now().time()) -> None:
        self.pack: SoundPack | None = SoundPack.load(pack) if isinstance(pack, (str, Path)) else pack
        self.volume = _volume(volume, "Sounds")
        self.cooldown = cooldown
        self.window = window
        self.quiet_hours = quiet_hours
        self.player = list(player) if player is not None else find_player()
        self.schedule = schedule
        self.on_error = on_error
        self._spawn = spawn
        self._clock = clock
        self._wall_clock = wall_clock
        #: Off for this environment (``TEXTUAL_ORBITAL_SOUND=off``); a test's own ``spawn`` still records.
        self.silenced = spawn is None and silenced()
        self.broken: BaseException | None = None  # why the player couldn't start: sound stays off
        self.played: deque[str] = deque(maxlen=32)
        self._muted = False
        self._last: dict[str, tuple[float, int]] = {}  # group -> (when, priority) it last sounded
        self._pending: tuple[str, int, str] | None = None
        self._lock = threading.Lock()
        self._playing: set[Process] = set()
        self._threads: set[threading.Thread] = set()
        self._loop: tuple[str, threading.Event] | None = None
        self._failed: set[str] = set()  # cues whose player exited with an error (reported once)

    # -- state ----------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """A pack, a player, and nothing turning sound off (``muted`` aside)."""
        return self.pack is not None and self.player is not None and not self.silenced and self.broken is None

    @property
    def muted(self) -> bool:
        """No sound while True; muting also stops the loop."""
        return self._muted

    @muted.setter
    def muted(self, value: bool) -> None:
        self._muted = bool(value)
        if self._muted:
            self._pending = None
            self.stop_loop()

    def quiet(self) -> bool:
        """Inside ``quiet_hours`` now."""
        return self.quiet_hours is not None and _in_hours(self._wall_clock(), self.quiet_hours)

    def can_play(self, cue: str) -> bool:
        """Would ``cue`` sound now, cooldowns aside: enabled, in the pack, not muted or quiet."""
        return self.enabled and not self._muted and self.pack is not None and cue in self.pack and not self.quiet()

    def priority(self, cue: str) -> int:
        return self.PRIORITY.get(cue, 10)

    def group(self, cue: str) -> str:
        return self.GROUPS.get(cue, cue)

    def _cooling(self, group: str, priority: int) -> bool:
        last = self._last.get(group)
        if last is None:
            return False
        when, last_priority = last
        return self._clock() - when < self.COOLDOWNS.get(group, self.cooldown) and priority <= last_priority

    # -- playing --------------------------------------------------------------------------

    def play(self, cue: str, *, priority: int | None = None, group: str | None = None) -> bool:
        """Ask for ``cue``. False when it won't sound: not in the pack, off, muted, quiet
        hours, or its group is cooling down. True when it is playing, or waiting out the
        ``window`` (where a more important cue may still take its place)."""
        if not self.can_play(cue):
            return False
        rank = self.priority(cue) if priority is None else priority
        team = group or self.group(cue)
        if self._cooling(team, rank):
            return False
        if self.schedule is None or self.window <= 0:
            return self._start(cue, rank, team)
        if self._pending is None:
            self._pending = (cue, rank, team)
            self.schedule(self.window, self.flush)
        elif rank > self._pending[1]:
            self._pending = (cue, rank, team)
        return True

    def flush(self) -> None:
        """Play the most important cue of the window (``schedule`` calls it)."""
        pending, self._pending = self._pending, None
        if pending is not None and self.can_play(pending[0]) and not self._cooling(pending[2], pending[1]):
            self._start(*pending)

    def _start(self, cue: str, rank: int, team: str) -> bool:
        assert self.pack is not None and self.player is not None
        with self._lock:
            if len(self._playing) >= self.MAX_AT_ONCE:
                return False
        self._last[team] = (self._clock(), rank)
        self.played.append(cue)
        self._thread(self._run_once, cue, self._argv(cue))
        return True

    def _argv(self, cue: str) -> list[str]:
        assert self.pack is not None and self.player is not None
        sound = self.pack.sounds[cue]
        volume = min(1.0, max(0.0, self.volume * self.pack.volume * sound.volume))
        return player_argv(self.player, sound.file, volume)

    def _thread(self, target: Callable[..., None], *args: Any) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True, name="orbital-sound")
        with self._lock:
            self._threads.add(thread)
        thread.start()

    def _launch(self, cue: str, argv: list[str], *, counted: bool = True) -> Process | None:
        """Start a player; ``counted`` ones (not loops) count towards ``MAX_AT_ONCE``."""
        try:
            proc = (self._spawn or _popen)(argv)
        except Exception as error:  # noqa: BLE001 - no player: report once, stay silent
            if self.broken is None:
                self.broken = error
                self._report("sound player", error)
            return None
        if counted:
            with self._lock:
                self._playing.add(proc)
        return proc

    def _finished(self, cue: str, proc: Process, code: int | None) -> None:
        with self._lock:
            self._playing.discard(proc)
        if code is not None and code > 0 and cue not in self._failed:
            self._failed.add(cue)
            self._report(f"sound {cue!r}", RuntimeError(f"the player exited with {code}"))

    def _run_once(self, cue: str, argv: list[str]) -> None:
        try:
            proc = self._launch(cue, argv)
            if proc is None:
                return
            try:
                code: int | None = proc.wait(timeout=LONGEST)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = None
            self._finished(cue, proc, code)
        finally:
            self._done_thread()

    def _done_thread(self) -> None:
        with self._lock:
            self._threads.discard(threading.current_thread())

    def _report(self, where: str, error: BaseException) -> None:
        if self.on_error is not None:
            try:
                self.on_error(where, error)
            except Exception:  # noqa: BLE001, S110 - the reporter itself failed: nothing more to do
                pass

    # -- loops --------------------------------------------------------------------------

    @property
    def looping(self) -> str | None:
        """The cue playing over and over, or None."""
        return self._loop[0] if self._loop is not None else None

    def loop(self, cue: str) -> bool:
        """Play ``cue`` over and over (a room tone) until :meth:`stop_loop`, :meth:`stop` or
        muting. One loop at a time: a new one replaces the old. Unmuting doesn't restart it.
        Returns False when it won't sound (as :meth:`play`, without the cooldown)."""
        if self.looping == cue:
            return True
        self.stop_loop()
        if not self.can_play(cue):
            return False
        stop = threading.Event()
        self._loop = (cue, stop)
        self.played.append(cue)
        self._thread(self._run_loop, cue, self._argv(cue), stop)
        return True

    def stop_loop(self) -> None:
        if self._loop is not None:
            self._loop[1].set()
            self._loop = None

    def _run_loop(self, cue: str, argv: list[str], stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                proc = self._launch(cue, argv, counted=False)  # its own stop ends it, within 0.2 s
                if proc is None:
                    return
                code: int | None = None
                while code is None:
                    try:
                        code = proc.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        if stop.is_set():
                            proc.terminate()
                            code = -1
                if code > 0:
                    self._finished(cue, proc, code)
                    return  # a file the player can't play: don't spin
        finally:
            if self._loop is not None and self._loop[1] is stop:
                self._loop = None  # ended by itself (a failure): no longer looping
            self._done_thread()

    def stop(self) -> None:
        """Stop the loop and every sound playing, and forget the pending one (on exit)."""
        self._pending = None
        self.stop_loop()
        with self._lock:
            playing = list(self._playing)
        for proc in playing:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001, S110 - it ended by itself meanwhile
                pass

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Wait until every player thread has ended (loops must be stopped first).
        For tests and scripts; True if they all ended in time."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                threads = list(self._threads)
            if not threads:
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            threads[0].join(min(left, 0.1))


def parse_quiet_hours(text: str) -> tuple[clock_time, clock_time]:
    """``"22-7"`` or ``"22:30-07:15"`` -> ``(start, end)`` local times."""
    try:
        start, end = (part.strip() for part in text.split("-"))

        def one(part: str) -> clock_time:
            hours, _, minutes = part.partition(":")
            return clock_time(int(hours) % 24, int(minutes or 0))  # 24 is midnight

        return one(start), one(end)
    except ValueError:
        raise ValueError(f"quiet hours look like 22-7 or 22:30-07:00, not {text!r}") from None


__all__ = ["CUES", "OFF", "PLAYERS", "Process", "Sound", "SoundPack", "Sounds", "find_player",
           "parse_quiet_hours", "player_argv", "silenced"]
