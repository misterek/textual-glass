"""A scripted fake bridge: made-up Claude Code and Codex hook events fed through ``hook.apply`` into a folder.

The demo reads that folder exactly as it reads the real one, so it exercises the hook and the reader too.
Time is compressed: the fake keeps a virtual clock that runs ``speed`` times faster than the wall clock
(20 by default), and the app uses it as "now", so a ten-minute wait takes thirty seconds.

The script is a 40-minute (virtual) loop of five sessions that between them go through every state: a
permission wait that escalates to waving and standing, a question, subagents coming and going, a tool
failure's spark, compacting, an error, a long done that dozes off, and visitors that arrive and leave (the
second is a Codex session: a permission, a failed model request and its retry, a question, an interrupt).
The keys of the demo add to it: a new session, a wait, an answer, an error, an end.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bridge import hook
from bridge.state import Session, SessionReader

CYCLE = 40.0  # virtual minutes per loop of the script
DEFAULT_SPEED = 20.0

#: Sessions: script key -> (project, model, agent). Keys ending in "#" are visitors: a new session each loop.
PROJECTS = {
    "api": ("api-gateway", "claude-opus-4", "claude"),
    "docs": ("docs-site", "claude-sonnet-4", "claude"),
    "infra": ("infra", "claude-opus-4", "claude"),
    "billing": ("billing-service", "claude-sonnet-4", "claude"),
    "mobile#": ("mobile-app", "claude-haiku-4", "claude"),
    "web#": ("web-frontend", "gpt-5-codex", "codex"),
}
#: Projects for sessions the user adds with ``n``.
SPARE_PROJECTS = ("search-indexer", "auth-service", "data-pipeline", "design-system", "cli-tools", "ml-training",
                  "status-page", "edge-cache")

FIRST = "first"  # an event only the first loop plays (the persistent sessions' SessionStart)


@dataclass(frozen=True)
class Beat:
    """One event of the script, ``at`` virtual minutes into the loop."""

    at: float
    who: str
    event: str
    fields: dict[str, Any] = field(default_factory=dict)
    first_only: bool = False


def _b(at: float, who: str, event: str, first: str = "", **fields: Any) -> Beat:
    return Beat(at, who, event, dict(fields), first == FIRST)


SCRIPT: tuple[Beat, ...] = tuple(sorted([
    # api-gateway: three subagents, then a permission wait: hand up at 6, waving at 8, standing at 16
    _b(0.0, "api", "SessionStart", FIRST, source="startup"),
    _b(0.2, "api", "UserPromptSubmit"),
    _b(0.5, "api", "PreToolUse", tool_name="Read"),
    _b(1.0, "api", "SubagentStart", agent_id="ax1", agent_type="Explore"),
    _b(1.3, "api", "SubagentStart", agent_id="ax2", agent_type="Explore"),
    _b(1.6, "api", "SubagentStart", agent_id="ax3", agent_type="general-purpose"),
    _b(4.0, "api", "SubagentStop", agent_id="ax1"),
    _b(4.5, "api", "SubagentStop", agent_id="ax2"),
    _b(5.0, "api", "SubagentStop", agent_id="ax3"),
    _b(6.0, "api", "PermissionRequest", tool_name="Bash"),
    _b(6.1, "api", "Notification", notification_type="permission_prompt",
       message="Claude needs your permission to use Bash"),
    _b(18.0, "api", "PostToolUse", tool_name="Bash"),
    _b(24.0, "api", "Stop"),
    _b(25.0, "api", "Notification", notification_type="idle_prompt", message="Claude is waiting for your input"),
    _b(34.0, "api", "UserPromptSubmit"),
    # docs-site: done early, dozes off after 30 minutes, back to work just before the loop ends
    _b(0.0, "docs", "SessionStart", FIRST, source="startup"),
    _b(0.4, "docs", "UserPromptSubmit"),
    _b(3.0, "docs", "Stop"),
    _b(4.0, "docs", "Notification", notification_type="idle_prompt", message="Claude is waiting for your input"),
    _b(39.0, "docs", "UserPromptSubmit"),
    # infra: a tool failure's spark, an interrupt (no spark), compacting, an error, a model switch
    _b(0.0, "infra", "SessionStart", FIRST, source="resume"),
    _b(0.6, "infra", "UserPromptSubmit"),
    _b(2.0, "infra", "PostToolUseFailure", tool_name="Bash", error="exit status 1", is_interrupt=False),
    _b(3.0, "infra", "PostToolUseFailure", tool_name="Bash", error="interrupted", is_interrupt=True),
    _b(5.0, "infra", "PreCompact", trigger="auto"),
    _b(8.0, "infra", "PostCompact", trigger="auto"),
    _b(12.0, "infra", "StopFailure", error="API Error: 529 overloaded"),
    _b(16.0, "infra", "UserPromptSubmit"),
    _b(19.0, "infra", "PostModelSwitch", to_model="claude-sonnet-4"),
    _b(30.0, "infra", "Stop"),
    _b(36.0, "infra", "UserPromptSubmit"),
    # billing-service: a question while api-gateway waits (red alert), a subagent, done
    _b(0.0, "billing", "SessionStart", FIRST, source="startup"),
    _b(0.8, "billing", "UserPromptSubmit"),
    _b(10.0, "billing", "Notification", notification_type="elicitation_dialog", message="Claude has a question"),
    _b(14.0, "billing", "UserPromptSubmit"),
    _b(21.0, "billing", "SubagentStart", agent_id="bx1", agent_type="Plan"),
    _b(27.0, "billing", "SubagentStop", agent_id="bx1"),
    _b(28.0, "billing", "Stop"),
    _b(37.0, "billing", "UserPromptSubmit"),
    # visitors: mobile-app arrives, works, leaves; then web-frontend, a Codex session, arrives, asks for
    # permission, loses a model request and retries it, asks a question, finishes, is interrupted, leaves
    _b(1.0, "mobile#", "SessionStart", source="startup"),
    _b(2.0, "mobile#", "UserPromptSubmit"),
    _b(9.0, "mobile#", "Stop"),
    _b(14.0, "mobile#", "SessionEnd", reason="prompt_input_exit"),
    _b(21.0, "web#", "SessionStart", source="startup"),
    _b(22.0, "web#", "UserPromptSubmit"),
    _b(23.0, "web#", "PreToolUse", tool_name="Bash"),
    _b(26.0, "web#", "PermissionRequest", tool_name="apply_patch"),
    _b(27.5, "web#", "PostToolUse", tool_name="apply_patch"),
    _b(29.0, "web#", "PostModelRequest", status="failed", error="stream disconnected before completion"),
    _b(30.0, "web#", "PostModelRequest", status="completed"),
    _b(31.0, "web#", "PreToolUse", tool_name="request_user_input"),
    _b(32.0, "web#", "PostToolUse", tool_name="request_user_input"),
    _b(33.0, "web#", "Stop"),
    _b(36.0, "web#", "UserPromptSubmit"),
    _b(37.0, "web#", "Interrupt"),
    _b(38.0, "web#", "SessionEnd", reason="other"),
], key=lambda b: b.at))


class FakeBridge:
    """Plays :data:`SCRIPT` (and the demo's key presses) into ``state_dir`` through ``hook.apply``.

    Call :meth:`step` often (the app does, every poll); :meth:`now` is the virtual clock to read the
    folder with. ``clock`` is the wall clock in seconds (``time.monotonic``; tests pass a fake one), and
    ``at`` starts the script that many virtual minutes in (the first step plays everything before it).
    """

    def __init__(self, state_dir: Path | str, speed: float = DEFAULT_SPEED, *,
                 clock: Callable[[], float] = time.monotonic, start: datetime | None = None,
                 at: float = 0.0) -> None:
        self.state_dir = Path(state_dir)
        self.speed = max(0.01, float(speed))
        self._clock = clock
        self._t0 = start or datetime.now(timezone.utc)
        self._banked = max(0.0, at) * 60  # virtual seconds before the last resume (``at``: minutes in)
        self._resumed = clock()        # wall time of the last resume
        self.paused = False
        self._played = -1.0            # virtual seconds of script played so far
        self._extra: list[tuple[float, dict[str, Any]]] = []  # events the keys scheduled, virtual seconds
        self._ended: set[str] = set()  # sessions ended by a key: the script leaves them alone
        self._added = 0
        self.reader = SessionReader(self.state_dir)

    # -- time ------------------------------------------------------------------------------------

    def elapsed(self) -> float:
        """Virtual seconds since the start."""
        if self.paused:
            return self._banked
        return self._banked + (self._clock() - self._resumed) * self.speed

    def now(self) -> datetime:
        return self._t0 + timedelta(seconds=self.elapsed())

    def toggle_pause(self) -> bool:
        """Pause or resume the script and the clock; returns whether it is paused now."""
        if self.paused:
            self._resumed = self._clock()
        else:
            self._banked = self.elapsed()
        self.paused = not self.paused
        return self.paused

    # -- the script ------------------------------------------------------------------------------

    def session_id(self, who: str, loop: int) -> str:
        project = PROJECTS[who][0]
        return f"fake-{project}-{loop}" if who.endswith("#") else f"fake-{project}"

    def _event(self, sid: str, project: str, name: str, model: str = "", agent: str = "claude",
               **fields: Any) -> dict[str, Any]:
        event: dict[str, Any] = {
            "session_id": sid, "hook_event_name": name, "cwd": f"/work/{project}",
            "transcript_path": f"/tmp/fake/{sid}.jsonl", "permission_mode": "default",
        }
        if model and (name == "SessionStart" or agent == "codex"):  # Codex names the model on every event
            event["model"] = model
        event.update(fields)
        return event

    def _apply(self, event: dict[str, Any], at: float, agent: str = "claude") -> None:
        hook.apply(event, self.state_dir, self._t0 + timedelta(seconds=at), agent)

    def step(self) -> int:
        """Play every event due by now; returns how many were applied."""
        upto = self.elapsed()
        played = 0
        due: list[tuple[float, int, dict[str, Any], str]] = []
        cycle = CYCLE * 60
        first_loop = int(max(0.0, self._played) // cycle)
        for loop in range(first_loop, int(upto // cycle) + 1):
            for order, beat in enumerate(SCRIPT):
                at = loop * cycle + beat.at * 60
                if not self._played < at <= upto or (beat.first_only and loop > 0):
                    continue
                sid = self.session_id(beat.who, loop)
                if sid in self._ended:
                    continue
                project, model, agent = PROJECTS[beat.who]
                due.append((at, order, self._event(sid, project, beat.event, model, agent, **beat.fields), agent))
        keep = []
        for at, event in self._extra:
            if at <= upto:
                due.append((at, len(SCRIPT), event, "claude"))
            else:
                keep.append((at, event))
        self._extra = keep
        for at, _, event, agent in sorted(due, key=lambda d: (d[0], d[1])):
            self._apply(event, at, agent)
            played += 1
        self._played = upto
        return played

    # -- the demo's keys -------------------------------------------------------------------------

    def sessions(self) -> list[Session]:
        return [s for s in self.reader.read(self.now()) if s.present]

    def _now_event(self, session: Session, name: str, **fields: Any) -> None:
        event = self._event(session.id, session.project, name, session.model, session.agent, **fields)
        self._apply(event, self.elapsed(), session.agent)

    def add_session(self) -> str:
        """A new session arrives (``n``): it starts, works for a while, and finishes."""
        used = {s.project for s in self.sessions()}
        project = next((p for p in SPARE_PROJECTS if p not in used), SPARE_PROJECTS[self._added % len(SPARE_PROJECTS)])
        self._added += 1
        sid = f"fake-user-{self._added}-{project}"
        t = self.elapsed()
        self._apply(self._event(sid, project, "SessionStart", "claude-sonnet-4", source="startup"), t)
        self._extra.append((t + 20, self._event(sid, project, "UserPromptSubmit")))
        self._extra.append((t + 8 * 60, self._event(sid, project, "Stop")))
        return sid

    def make_wait(self) -> str | None:
        """Someone asks for permission (``w``): the first working or idle session that isn't waiting."""
        order = {"working": 0, "compacting": 1, "done": 2, "seated": 3, "error": 4}
        candidates = sorted((s for s in self.sessions() if s.state in order), key=lambda s: order[s.state])
        if not candidates:
            return None
        self._now_event(candidates[0], "PermissionRequest", tool_name="Bash")
        return candidates[0].id

    def answer_all(self) -> int:
        """Every waiting session gets its answer (``a``) and goes back to work: the user replies in its chat,
        which settles whatever it asked, whoever asked it."""
        waiting = [s for s in self.sessions() if s.state == "waiting"]
        for s in waiting:
            self._now_event(s, "UserPromptSubmit")
        return len(waiting)

    def make_error(self) -> str | None:
        """A turn dies on an error (``e``): the first working session, else any that isn't in error."""
        sessions = [s for s in self.sessions() if s.state != "error"]
        sessions.sort(key=lambda s: s.state != "working")
        if not sessions:
            return None
        if sessions[0].agent == "codex":  # Codex has no failure event: its last model request failed
            self._now_event(sessions[0], "PostModelRequest", status="failed", error="unexpected status 500")
        else:
            self._now_event(sessions[0], "StopFailure", error="API Error: 500 internal server error")
        return sessions[0].id

    def end_one(self) -> str | None:
        """The newest session ends (``x``); the script leaves it alone from then on."""
        sessions = self.sessions()
        if not sessions:
            return None
        newest = max(sessions, key=lambda s: (s.started, s.id))
        self._now_event(newest, "SessionEnd", reason="prompt_input_exit")
        self._ended.add(newest.id)
        return newest.id
