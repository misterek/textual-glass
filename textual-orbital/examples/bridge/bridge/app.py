"""The bridge app: Claude Code and Codex sessions as little crew members at their consoles on a starship bridge.

``app.py`` next to this package runs ``main()``; README.md has the options and the keys. :class:`Bridge` is what
the app shows, apart from the app: another app can show the bridge on a page of its own with it and a ``Deck``.
With a ``textual_orbital.Sounds`` it also plays a short sound when the crew does something (:data:`CUES`).
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.content import Content
from textual.widgets import Static

from bridge import crew, hook
from bridge.fake import DEFAULT_SPEED, FakeBridge
from bridge.state import SeatingChart, Session, SessionReader, pose_for, red_alert, sparking, waiting_count
from bridge.widgets import STATIONS, Deck, Station, StationView, hexed, lamp_color
from textual_glass import Glass
from textual_orbital import ORBITAL, Sounds, fmt_age
from textual_orbital.sounds import parse_quiet_hours

POLL = 1.0  # seconds between reads of the state folder
LAMPS = {"working": "working", "compacting": "working", "waiting": "waiting", "error": "error", "done": "done"}
WORDS = {"seated": "seated", "working": "working", "done": "done", "error": "error", "compacting": "compacting",
         "ended": "left"}
FAKE_KEYS = (("n", "new"), ("w", "wait"), ("a", "answer"), ("e", "error"), ("x", "end"), ("space", "pause"),
             ("q", "quit"))
#: Where each agent's registration goes (printed to stderr by ``--print-hooks``, after the JSON on stdout).
WHERE = {
    "claude": "Save it as a file for one session at a time (claude --settings FILE), or merge its \"hooks\" into\n"
              "~/.claude/settings.json for every session. Check the events with /hooks in Claude Code.",
    "codex": "Save it as ~/.codex/hooks.json for every session, or as .codex/hooks.json in a project for that\n"
             "project once you trust it (merge it with any hooks already there). Then review and trust the\n"
             "hooks with /hooks in Codex: it runs new or changed hooks only after that.",
}


#: The sounds the bridge plays (with a ``Sounds``): cue -> (when, how important, the group whose cooldown it
#: shares). ``hand-up`` shares "needs-you" with a dashboard's own "Claude needs you" sounds, so one wait doesn't
#: chime twice. The pack's ``room``, if it has one, is a room tone the app may loop (:data:`ROOM`).
CUES: dict[str, tuple[str, int, str]] = {
    "red-alert": ("red alert came on: two crew waiting, or an error", 95, "red-alert"),
    "hand-up": ("a crew member raised a hand: a session waits for you", 90, "needs-you"),
    "error": ("a session's turn ended in an error", 80, "error"),
    "escalate": ("still waiting: waving after 2 minutes, standing after 10", 75, "escalate"),
    "done": ("a session finished its turn", 40, "done"),
    "crew-in": ("a session started: a crew member takes a station", 30, "crew"),
    "crew-out": ("a session ended: its crew member leaves", 30, "crew"),
    "ensign-out": ("a subagent started: an ensign runs out", 20, "ensigns"),
    "ensign-in": ("a subagent finished: an ensign comes back", 20, "ensigns"),
}
#: The pack's room tone, looped by the app while the bridge shows (``--room``).
ROOM = "room"


@dataclass(frozen=True)
class Heard:
    """What one read saw of a session, for the sounds: its state, pose, ensigns and whether it is aboard."""

    state: str
    pose: str
    ensigns: int
    present: bool


def heard(sessions: list[Session], now: datetime) -> dict[str, Heard]:
    return {s.id: Heard(s.state, pose_for(s, now), len(s.subagents), s.present) for s in sessions}


def cues_between(before: Mapping[str, Heard], after: Mapping[str, Heard], alert_before: bool,
                 alert_after: bool) -> list[str]:
    """The cues for what changed between two reads (:data:`CUES`), most important first."""
    cues: list[str] = ["red-alert"] if alert_after and not alert_before else []
    for sid, now in after.items():
        old = before.get(sid)
        if old is None:
            if now.present:
                cues.append("crew-in")
                if now.state == "waiting":
                    cues.append("hand-up")
            continue
        if old.present and not now.present:
            cues.append("crew-out")
            continue
        if now.state != old.state:
            cue = {"waiting": "hand-up", "error": "error", "done": "done"}.get(now.state)
            if cue is not None:
                cues.append(cue)
        elif now.state == "waiting" and now.pose != old.pose and now.pose in ("waving", "standing"):
            cues.append("escalate")
        if now.ensigns > old.ensigns:
            cues.append("ensign-out")
        elif now.ensigns < old.ensigns:
            cues.append("ensign-in")
    cues += ["crew-out" for sid, old in before.items() if sid not in after and old.present]
    return sorted(cues, key=lambda cue: -CUES[cue][1])


def status_word(session: Session, pose: str) -> str:
    if session.state == "waiting":
        return "question" if session.waiting_kind == "question" else "allow?"
    if pose == "dozing":
        return "dozing"
    return WORDS.get(session.state, session.state)


def short_model(model: str) -> str:
    return model.removeprefix("claude-")


def registration(agent: str) -> dict[str, Any]:
    """The hook registration for ``agent``: every event in ``hook.EVENTS[agent]``, sent to this checkout's
    ``hook.py`` with ``--agent``, run by this Python. Claude Code's settings and Codex's ``hooks.json`` have
    the same shape."""
    command = " ".join(shlex.quote(part) for part in (sys.executable, str(Path(hook.__file__).resolve()),
                                                       "--agent", agent))
    hooks: dict[str, Any] = {}
    for name in hook.EVENTS[agent]:
        group: dict[str, Any] = {"hooks": [{"type": "command", "command": command}]}
        hooks[name] = [{"matcher": "*", **group} if name in hook.TOOL_EVENTS else group]
    return {"hooks": hooks}


def pulse(station: Station) -> None:
    """A pulsar over a station whose crew member just raised a hand (with glass on)."""
    glass = station.glass
    if glass is not None and glass.active:
        glass.flourish("pulsar", at=station, color=hexed(lamp_color("waiting", station.theme_palette)))


class Bridge:
    """What the bridge shows, apart from any app: the sessions in a state folder, who sits where, red alert.

    :meth:`update` plays the fake into its folder (if there is one), reads the folder (re-reading only the files
    that changed), seats the sessions and shows them on a :class:`~bridge.widgets.Deck`, touching only the
    widgets whose look changed, and plays a pulsar over a station whose crew member has just raised a hand.
    ``BridgeApp`` calls it once a second. It is not a widget, so another app can show the bridge on a page of
    its own (a ``Deck`` in a panel), call it on its own terms (only while that page shows, say) and title the
    panel from ``present``, ``alert``, ``waiting``, ``errors`` and ``unseated``.

    With ``sounds``, each update plays the cues for what changed since the last one (:func:`cues_between`); the
    first update, and one with ``quiet=True`` (the first after the page was hidden a while), only listen.
    """

    def __init__(self, pack: crew.Pack | None = None, *, state_dir: Path | str | None = None,
                 fake: FakeBridge | None = None, sounds: Sounds | None = None) -> None:
        self.pack = pack if pack is not None else crew.load()
        self.fake = fake
        self.state_dir = fake.state_dir if fake is not None else Path(state_dir or hook.state_dir())
        self.reader = SessionReader(self.state_dir)
        self.members = {m.id: m for m in self.pack.members}
        self.chart = SeatingChart([m.id for m in self.pack.members], STATIONS)
        self.sessions: list[Session] = []   # every session on the bridge, ended ones while they leave
        self.present: list[Session] = []    # the ones still at their stations
        self.alert = False
        self.polls = 0                      # updates so far
        self.sounds = sounds
        self._states: dict[str, str] = {}
        self._heard: dict[str, Heard] = {}

    def now(self) -> datetime:
        return self.fake.now() if self.fake is not None else datetime.now(timezone.utc)

    @property
    def waiting(self) -> int:
        return waiting_count(self.present)

    @property
    def errors(self) -> int:
        return sum(1 for s in self.present if s.state == "error")

    @property
    def unseated(self) -> int:
        """Sessions with no station (more sessions than stations): "below deck"."""
        return self.chart.unseated(self.sessions)

    def update(self, deck: Deck, *, quiet: bool = False) -> None:
        """Play the fake, read the folder and show it on ``deck``, and play the sounds for what changed
        (unless ``quiet``). Raises what reading raises (the caller reports it and tries again later)."""
        if self.fake is not None:
            self.fake.step()
        now = self.now()
        sessions = self.reader.read(now)
        seats = self.chart.seat(sessions)
        for station, session in zip(deck.stations, seats, strict=True):
            started_waiting = session is not None and session.state == "waiting" and \
                self._states.get(session.id) != "waiting"
            station.set_view(self.view_for(session, now))
            if started_waiting and self.polls:
                pulse(station)
        self._states = {s.id: s.state for s in sessions}
        self.sessions = sessions
        self.present = [s for s in sessions if s.present]
        alert_before, self.alert = self.alert, red_alert(self.present)
        now_heard = heard(sessions, now)
        if self.sounds is not None and self.polls and not quiet:
            for cue in cues_between(self._heard, now_heard, alert_before, self.alert):
                _, priority, group = CUES[cue]
                self.sounds.play(cue, priority=priority, group=group)
        self._heard = now_heard
        deck.set_alert(self.alert)
        deck.chair.set_waiting(self.waiting)
        self.polls += 1

    def view_for(self, session: Session | None, now: datetime) -> StationView:
        if session is None:
            return StationView()
        member_id = self.chart.member_of.get(session.id)
        member = self.members.get(member_id) if member_id else None
        uniform = member.uniform if member is not None else None
        age = fmt_age(now - session.since)
        agent = "" if session.agent == hook.AGENTS[0] else session.agent
        if not session.present:
            return StationView(project=session.project, uniform=uniform, status="left", age=age, leaving=True)
        pose = pose_for(session, now)
        return StationView(
            member=member_id, name=member.name if member is not None else "", pose=pose,
            lamp="dim" if pose == "dozing" else LAMPS.get(session.state, "dim"),
            ensigns=min(3, len(session.subagents)), project=session.project, uniform=uniform,
            status=status_word(session, pose), age=age, spark=sparking(session, now),
            model=short_model(session.model), agent=agent,
        )


class BridgeApp(Glass, App[None]):
    """Reads the state folder once a second and shows each session at a station."""

    TITLE = "Bridge"
    CSS_PATH = "bridge.tcss"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("n", "new", "New", show=False),
        Binding("w", "wait", "Wait", show=False),
        Binding("a", "answer", "Answer", show=False),
        Binding("e", "error", "Error", show=False),
        Binding("x", "end", "End", show=False),
        Binding("space", "pause", "Pause", show=False),
        Binding("m", "mute", "Mute", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(self, *, state_dir: Path | str | None = None, fake: FakeBridge | None = None,
                 pack: crew.Pack | None = None, poll: float = POLL, sounds: Sounds | None = None,
                 room: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sounds = sounds if sounds is not None else Sounds()
        if self.sounds.schedule is None:  # one sound per moment
            self.sounds.schedule = lambda delay, callback: self.set_timer(delay, callback)
        if self.sounds.on_error is None:
            self.sounds.on_error = lambda where, error: self.call_from_thread(
                self.notify, f"{where}: {error}", title="Bridge: sound", severity="warning")
        self.room = room
        self.bridge = Bridge(pack, state_dir=state_dir, fake=fake, sounds=self.sounds)
        self.poll_interval = poll
        self.poll_errors: list[str] = []
        self._title = ""
        self._hint: tuple[Any, ...] = ()

    def compose(self) -> ComposeResult:
        with Vertical(id="bridge"):
            yield Deck(self.bridge.pack, id="deck")
        yield Static(id="keys")

    def on_mount(self) -> None:
        self.register_theme(ORBITAL)
        self.theme = ORBITAL.name
        self.poll()
        self.set_interval(self.poll_interval, self.poll)
        self.play_room()

    def on_unmount(self) -> None:
        self.sounds.stop()

    # -- sound ---------------------------------------------------------------------------------------

    def play_room(self) -> None:
        """The room tone (``--room``), while the window has focus and sound isn't muted."""
        if self.room and self.app_focus and not self.sounds.muted:
            self.sounds.loop(ROOM)
        else:
            self.sounds.stop_loop()

    def watch_app_focus(self, focus: bool) -> None:
        self.play_room()

    def action_mute(self) -> None:
        self.sounds.muted = not self.sounds.muted
        self.play_room()
        self.notify("muted" if self.sounds.muted else "sound on", timeout=2)

    # -- reading ------------------------------------------------------------------------------------

    @property
    def deck(self) -> Deck:
        return self.query_one(Deck)

    def poll(self) -> None:
        """Read the folder and update only what visibly changed. An error is reported and the next poll
        tries again: an always-on panel never dies of one bad read."""
        try:
            self._poll()
        except Exception as error:  # noqa: BLE001 - reported below, and polling goes on
            self.poll_errors.append(f"{type(error).__name__}: {error}")
            self.log.error("bridge poll failed", error)
            if len(self.poll_errors) == 1 or len(self.poll_errors) % 60 == 0:
                self.notify(self.poll_errors[-1], title="Bridge: reading the sessions failed", severity="error")

    def _poll(self) -> None:
        bridge = self.bridge
        bridge.update(self.deck)
        self.query_one("#bridge").set_class(bridge.alert, "-red-alert")
        self.show_title()
        self.show_hint()

    # -- text ------------------------------------------------------------------------------------------

    def show_title(self) -> None:
        bridge = self.bridge
        waiting, errors, unseated = bridge.waiting, bridge.errors, bridge.unseated
        present = bridge.present
        parts = ["RED ALERT" if bridge.alert else "BRIDGE", f"{len(present)} crew" if present else "no crew aboard"]
        if waiting:
            parts.append(f"{waiting} waiting")
        if errors:
            parts.append(f"{errors} error" + ("s" if errors > 1 else ""))
        if unseated:
            parts.append(f"+{unseated} below deck")
        title = " · ".join(parts)
        if title != self._title:
            self._title = title
            self.query_one("#bridge").border_title = title

    def show_hint(self) -> None:
        fake = self.bridge.fake
        paused = fake is not None and fake.paused
        hint = (fake is not None, paused)
        if hint == self._hint:
            return
        self._hint = hint
        keys: list[tuple[str, str]] = []
        if fake is None:
            keys.append((f"live · {self.bridge.state_dir}  ", "$text-muted"))
            keys += [("q", "bold $text-primary"), (" quit", "$text-muted")]
        else:
            for key, what in FAKE_KEYS:
                keys += [(key, "bold $text-primary"), (f" {what}  ", "$text-muted")]
            if paused:
                keys.append(("PAUSED", "bold $text-warning"))
        self.query_one("#keys", Static).update(Content.assemble(*keys))

    # -- keys (the fake only) -------------------------------------------------------------------------

    def _fake_key(self, act: str) -> None:
        fake = self.bridge.fake
        if fake is None:
            return
        getattr(fake, act)()
        self.poll()

    def action_new(self) -> None:
        self._fake_key("add_session")

    def action_wait(self) -> None:
        self._fake_key("make_wait")

    def action_answer(self) -> None:
        self._fake_key("answer_all")

    def action_error(self) -> None:
        self._fake_key("make_error")

    def action_end(self) -> None:
        self._fake_key("end_one")

    def action_pause(self) -> None:
        self._fake_key("toggle_pause")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="The bridge: Claude Code and Codex sessions as crew on a bridge.")
    parser.add_argument("--live", action="store_true",
                        help="real sessions from $BRIDGE_STATE or ~/.cache/bridge/sessions")
    parser.add_argument("--state", metavar="DIR", help="real sessions from DIR (implies --live)")
    parser.add_argument("--pack", metavar="DIR", help="the crew pack in DIR (default: the built-in crew)")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED, help="the fake's time compression (default 20)")
    parser.add_argument("--plain", action="store_true", help="stock Textual, no glass")
    parser.add_argument("--sounds", metavar="DIR", help="play the sound pack in DIR (textual-orbital's format)")
    parser.add_argument("--room", action="store_true", help="loop the pack's room tone while the window has focus")
    parser.add_argument("--volume", type=float, default=1.0, help="sound volume, 0 to 1 (default 1)")
    parser.add_argument("--quiet-hours", metavar="22-7", help="no sounds between these local hours")
    parser.add_argument("--print-hooks", choices=hook.AGENTS, metavar="AGENT",
                        help="print the hook registration for claude or codex, with this checkout's paths, and exit")
    parser.add_argument("--at", type=float, default=0.0, help=argparse.SUPPRESS)  # start the script N minutes in
    args = parser.parse_args(argv)
    if args.print_hooks:
        print(json.dumps(registration(args.print_hooks), indent=2))
        print(WHERE[args.print_hooks], file=sys.stderr)
        return
    try:
        pack = crew.load(args.pack)
    except (OSError, ValueError) as error:  # a broken pack raises a ValueError naming the file and line
        parser.exit(2, f"bridge: can't load the crew pack: {error}\n")
    try:
        sounds = Sounds(args.sounds, volume=args.volume,
                        quiet_hours=parse_quiet_hours(args.quiet_hours) if args.quiet_hours else None) \
            if args.sounds else None
    except (OSError, ValueError) as error:
        parser.exit(2, f"bridge: can't load the sound pack: {error}\n")
    glass = False if args.plain else None
    if args.live or args.state:
        BridgeApp(state_dir=args.state or hook.state_dir(), pack=pack, sounds=sounds, room=args.room,
                  glass=glass).run()
        return
    folder = tempfile.mkdtemp(prefix="bridge-fake-")
    try:
        BridgeApp(fake=FakeBridge(folder, args.speed, at=args.at), pack=pack, sounds=sounds, room=args.room,
                  glass=glass).run()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
