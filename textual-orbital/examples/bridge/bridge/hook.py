#!/usr/bin/env python3
"""The bridge's hook for Claude Code and Codex: one hook event in on stdin, one state file per session out.

Register it for the events in ``EVENTS`` (``app.py --print-hooks claude`` or ``codex`` prints the registration
with this file's path). ``--agent codex`` says the events come from Codex; without it they come from Claude
Code. It keeps ``<state dir>/<session_id>.json`` current, where the state dir is ``$BRIDGE_STATE`` or
``~/.cache/bridge/sessions``. The bridge app reads those files.

Rules, because it runs on every tool call of every session that has it:

- stdlib only, Python 3.10+, and quick: read stdin, one small read-modify-write, done;
- it prints nothing (some events add a hook's stdout to the agent's context) and always exits 0;
- it never raises: malformed JSON, unknown events and missing or odd fields are ignored;
- writes are atomic (a temporary file, then ``os.replace``), so a reader never sees half a file, and a
  short lock serialises concurrent hooks of one machine (parallel subagents stop at the same moment).
"""

from __future__ import annotations

import json
import os
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_STATE = "~/.cache/bridge/sessions"
STATES = ("seated", "working", "waiting", "done", "error", "compacting", "ended")
#: The agents the hook takes events from (``--agent``); the first is the default.
AGENTS = ("claude", "codex")
#: The events each agent sends that the hook uses: what to register (README.md says how each becomes a state).
EVENTS = {
    "claude": ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
               "PermissionRequest", "Notification", "Stop", "StopFailure", "PreCompact", "PostCompact",
               "SubagentStart", "SubagentStop", "PostModelSwitch", "SessionEnd"),
    "codex": ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest", "Stop",
              "Interrupt", "PostModelRequest", "PreCompact", "PostCompact", "SubagentStart", "SubagentStop",
              "SessionEnd"),
}
#: Events about one tool call: their registration takes a matcher on the tool name ("*": every tool).
TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest"})
KNOWN_EVENTS = frozenset(name for names in EVENTS.values() for name in names)
WORKING_EVENTS = frozenset({"UserPromptSubmit", "PreToolUse", "PostToolUse", "PostCompact"})
QUESTION_NOTIFICATIONS = frozenset({"elicitation_dialog", "agent_needs_input"})
#: Tools that ask the user a question and wait for the answer (Codex's, in plan mode): a wait, kind question.
QUESTION_TOOLS = frozenset({"request_user_input"})
WAIT_KINDS = ("permission", "question")
MAX_ID = 128            # longer session, agent or tool call ids are ignored
MAX_TEXT = 160          # error texts are cut to this
MAX_SUBAGENTS = 32      # a missed SubagentStop can't grow the file forever
MAX_WAITS = 16          # open waits kept (the newest); a missed answer can't grow the file forever
MAX_CALLS = 32          # tool calls in flight remembered, so a PermissionRequest finds its call's id
PRUNE_AFTER = timedelta(days=2)       # on SessionStart, files untouched this long are deleted
PRUNE_ENDED_AFTER = timedelta(hours=1)  # and ended sessions' files after this
LOCK_WAIT = 0.5         # seconds to wait for the lock before writing without it
_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def state_dir() -> Path:
    """Where the state files live: ``$BRIDGE_STATE`` or ``~/.cache/bridge/sessions``."""
    return Path(os.environ.get("BRIDGE_STATE") or DEFAULT_STATE).expanduser()


def safe_id(value: Any) -> str | None:
    """``value`` if it is usable as a file name (letters, digits, ``-_.``, not hidden), else None."""
    if not isinstance(value, str) or not value or len(value) > MAX_ID or value[0] == ".":
        return None
    return value if all(c in _SAFE for c in value) else None


def _text(value: Any) -> str:
    """A short text from whatever an event carries (a string, a dict with a message, a number)."""
    if isinstance(value, dict):
        for key in ("message", "error", "type", "reason"):
            if isinstance(value.get(key), str):
                return str(value[key])[:MAX_TEXT]
        return ""
    if value is None or isinstance(value, (list, tuple)):
        return ""
    return str(value)[:MAX_TEXT]


def _model(value: Any) -> str:
    """A model name from ``"claude-..."`` or ``{"id": ..., "display_name": ...}``."""
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if isinstance(value, dict):
        for key in ("display_name", "id", "name"):
            if isinstance(value.get(key), str):
                return str(value[key])[:MAX_TEXT]
    return ""


def _waiting_kind(event: dict[str, Any]) -> str | None:
    """``"permission"`` or ``"question"`` for a Notification that means "it needs you", else None."""
    kind = event.get("notification_type")
    if kind == "permission_prompt":
        return "permission"
    if isinstance(kind, str) and kind in QUESTION_NOTIFICATIONS:
        return "question"
    if kind is None:  # older versions send only a message
        message = event.get("message")
        if isinstance(message, str) and "permission" in message.lower():
            return "permission"
    return None


def _call_id(event: dict[str, Any]) -> str | None:
    """The event's ``tool_use_id``, if it has a usable one (PermissionRequest has none, in either agent)."""
    value = event.get("tool_use_id")
    return value if isinstance(value, str) and 0 < len(value) <= MAX_ID else None


def _call_key(actor: str, event: dict[str, Any]) -> str:
    """A short key for one tool call from what every event about it carries: who, the tool and its input.

    It ties a PermissionRequest, which has no ``tool_use_id``, to the call's PreToolUse and PostToolUse. A
    command is keyed on the command alone, other input without ``description``: Codex's PermissionRequest
    adds a ``description`` (why it asks) that its tool events don't have.
    """
    tool = event.get("tool_name") if isinstance(event.get("tool_name"), str) else None
    what = event.get("tool_input")
    if isinstance(what, dict):
        what = what["command"] if "command" in what else {k: v for k, v in what.items() if k != "description"}
    try:
        text = json.dumps([actor, tool, what], sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        text = json.dumps([actor, tool])
    data = text.encode()  # ASCII: json.dumps escapes the rest
    return f"h:{zlib.crc32(data):08x}{zlib.adler32(data):08x}"  # zlib: hashlib alone takes milliseconds to import


def _wait(value: Any) -> dict[str, Any] | None:
    """One open wait read back from the file, or None if it isn't one."""
    if not isinstance(value, dict):
        return None
    actor, kind, key, since = (value.get(name) for name in ("actor", "kind", "key", "since"))
    if not (actor == "" or safe_id(actor)) or kind not in WAIT_KINDS or not isinstance(since, str) \
            or len(since) > 64 or not (key is None or (isinstance(key, str) and 0 < len(key) <= MAX_ID)):
        return None
    return {"actor": actor, "kind": kind, "key": key, "since": since}


def _open_waits(old: dict[str, Any], old_state: str | None, since: str) -> list[dict[str, Any]]:
    """The open waits in ``old``, oldest first; anything odd in the list is no wait."""
    if "waits" not in old:  # a file from before the list: whoever raised the hand, with no call to match
        if old_state != "waiting":
            return []
        kind = old.get("waiting_kind") if old.get("waiting_kind") in WAIT_KINDS else "permission"
        return [{"actor": safe_id(old.get("waiting_agent")) or "", "kind": kind, "key": None, "since": since}]
    raw = old["waits"]
    waits = [_wait(w) for w in raw[-MAX_WAITS:]] if isinstance(raw, list) else []
    return [w for w in waits if w is not None]


def _raise_hand(waits: list[dict[str, Any]], actor: str, kind: str, key: str | None, stamp: str) -> None:
    """Open a wait for ``key`` (None: no call to match it to), unless that call's is open already."""
    if key is None or all(w["key"] != key for w in waits):
        waits.append({"actor": actor, "kind": kind, "key": key, "since": stamp})


def _calls(old: dict[str, Any]) -> list[list[str]]:
    """The tool calls in flight in ``old``: ``[call key, tool_use_id]`` pairs, oldest first."""
    raw = old.get("calls")
    if not isinstance(raw, list):
        return []
    return [list(c) for c in raw[-MAX_CALLS:] if isinstance(c, list) and len(c) == 2
            and all(isinstance(x, str) and 0 < len(x) <= MAX_ID for x in c)]


def next_record(old: dict[str, Any], event: dict[str, Any], now: datetime,
                agent: str = AGENTS[0]) -> dict[str, Any] | None:
    """The session's new record after ``event`` from ``agent``, or None to leave the file alone. Pure.

    The record keeps two layers. ``activity`` is what the session is doing: seated, working, done, error,
    compacting or ended. ``waits`` are the open requests for the user, one per tool call that asks (or per
    notification, when no call can be named). ``state`` is ``waiting`` while any wait is open, else the
    activity; ``waiting_kind`` and ``since`` then come from the oldest wait.
    """
    name = event.get("hook_event_name")
    sid = safe_id(event.get("session_id"))
    if not isinstance(name, str) or name not in KNOWN_EVENTS or sid is None:
        return None
    stamp = now.isoformat()
    old_state = old.get("state") if old.get("state") in STATES else None
    if old_state == "ended" and name != "SessionStart":
        return None  # a late event after the session ended doesn't bring it back
    subagents = [a for a in old.get("subagents", []) if isinstance(a, str)] \
        if isinstance(old.get("subagents"), list) else []
    rec: dict[str, Any] = {
        "session_id": sid,
        "agent": agent,
        "cwd": old.get("cwd") if isinstance(old.get("cwd"), str) else "",
        "project": "",
        "state": old_state or "seated",
        "waiting_kind": None,
        "since": old.get("since") if isinstance(old.get("since"), str) else stamp,
        "started": old.get("started") if isinstance(old.get("started"), str) else stamp,
        "updated": stamp,
        "model": old.get("model") if isinstance(old.get("model"), str) else "",
        "subagents": subagents,
        "spark_at": old.get("spark_at") if isinstance(old.get("spark_at"), str) else None,
        "error": old.get("error") if isinstance(old.get("error"), str) else "",
        "last_event": name,
        # what the session was doing before compacting, and since when: PostCompact goes back to it
        "resume_state": old.get("resume_state") if old.get("resume_state") in STATES else None,
        "resume_since": old.get("resume_since") if isinstance(old.get("resume_since"), str) else None,
    }
    waits = _open_waits(old, old_state, rec["since"])
    calls = _calls(old)
    old_activity = old.get("activity")
    if old_activity not in STATES or old_activity == "waiting":
        old_activity = "working" if old_state == "waiting" else (old_state or "seated")
    activity = old_activity
    # Hooks that fire inside a subagent carry its agent_id; SubagentStart and SubagentStop are about one.
    actor = "" if name in ("SubagentStart", "SubagentStop") else (safe_id(event.get("agent_id")) or "")
    cwd = event.get("cwd")
    if isinstance(cwd, str) and cwd:
        rec["cwd"] = cwd[:1024]
    rec["project"] = os.path.basename(rec["cwd"].rstrip("/\\")) if rec["cwd"] else ""
    if "agent_id" not in event:  # the session's own model (Codex names it on every event, Claude on SessionStart)
        rec["model"] = _model(event.get("model")) or rec["model"]

    call_id = _call_id(event)
    if name in TOOL_EVENTS:
        call_key = _call_key(actor, event)
        # the actor is busy again, so a wait no call can be named for is over (parallel siblings included)
        waits = [w for w in waits if not (w["key"] is None and w["actor"] == actor)]
        tool = event.get("tool_name")
        if name == "PreToolUse":
            if call_id:
                calls = [c for c in calls if c[1] != call_id] + [[call_key, call_id]]
            if isinstance(tool, str) and tool in QUESTION_TOOLS:  # it asks the user and waits for the answer
                _raise_hand(waits, actor, "question", call_id or call_key, stamp)
        elif name in ("PostToolUse", "PostToolUseFailure"):
            answered = {k for k in (call_id, call_key) if k}
            waits = [w for w in waits if w["key"] not in answered]  # that call ran: whatever it asked is settled
            calls = [c for c in calls if c[1] != call_id]
        elif name == "PermissionRequest":  # no tool_use_id: its call's, from the PreToolUse with the same key
            key = call_id or next((c[1] for c in reversed(calls) if c[0] == call_key), None) or call_key
            _raise_hand(waits, actor, "permission", key, stamp)

    since: str | None = None  # set to keep an older "since" (PostCompact going back)
    if name == "SessionStart" and event.get("source") == "compact":
        pass  # the same session after a compaction: PostCompact says where it goes back to
    elif name == "SessionStart":
        activity, waits, calls = "seated", [], []
        rec["subagents"], rec["error"], rec["spark_at"] = [], "", None
        if old_state == "ended":
            rec["started"] = stamp
    elif name in ("PreCompact", "PostCompact") and actor:
        pass  # a subagent compacting its own conversation: the session carries on as it was
    elif name == "PostCompact":
        resume = rec["resume_state"]
        activity = resume if resume not in (None, "compacting", "waiting") else "working"
        if activity != "working" and rec["resume_since"]:
            since = rec["resume_since"]
        rec["resume_state"] = rec["resume_since"] = None
    elif name in WORKING_EVENTS:
        if actor and old_activity in ("done", "seated"):
            pass  # a background subagent working after the turn ended: its ensign shows that
        else:
            activity = "working"
        if name == "UserPromptSubmit":
            rec["error"] = ""
            activity, waits = "working", []  # the user answered in the chat: whatever was asked is settled
    elif name == "Notification":
        found = _waiting_kind(event)
        # The ~6 s reminder of a request already up is the same wait. A notification may not say which
        # agent asked: without an agent_id, any open wait of its kind is the one it reminds of.
        if found is not None and not any(w["kind"] == found and (not actor or w["actor"] == actor) for w in waits):
            _raise_hand(waits, actor, found, None, stamp)
    elif name in ("Stop", "StopFailure"):
        waits = [w for w in waits if w["actor"] != actor]  # its turn is over; a subagent's request outlives it
        if name == "Stop":
            activity = "done"
        else:
            activity = "error"
            rec["error"] = _text(event.get("error")) or "the turn failed"
    elif name == "Interrupt":
        activity, waits = "done", []  # the user stopped the turn: their move
    elif name == "PostModelRequest" and not actor:
        status = event.get("status")
        if status == "failed":  # Codex retries a few times; the last failure ends the turn
            activity = "error"
            rec["error"] = _text(event.get("error")) or "the model request failed"
        elif status == "completed" and old_activity == "error":
            activity = "working"  # a retry got through
    elif name == "PostToolUseFailure":
        if event.get("is_interrupt") is not True:
            rec["spark_at"] = stamp
            rec["error"] = _text(event.get("error")) or rec["error"]
    elif name == "PreCompact":
        if old_activity != "compacting":
            rec["resume_state"] = old_activity
            rec["resume_since"] = rec["since"] if old_state == old_activity else None
        activity = "compacting"
    elif name in ("SubagentStart", "SubagentStop"):
        sub = safe_id(event.get("agent_id"))
        if sub is not None:
            agents = [a for a in rec["subagents"] if a != sub]
            if name == "SubagentStart":
                agents.append(sub)
            else:
                waits = [w for w in waits if w["actor"] != sub]  # the subagent is gone, and its requests with it
            rec["subagents"] = agents[-MAX_SUBAGENTS:]
    elif name == "PostModelSwitch":
        rec["model"] = _model(event.get("to_model")) or rec["model"]
    elif name == "SessionEnd":
        activity, waits, calls = "ended", [], []
        rec["subagents"] = []

    waits = waits[-MAX_WAITS:]
    rec["activity"], rec["waits"], rec["calls"] = activity, waits, calls[-MAX_CALLS:]
    if waits:
        rec["state"], rec["waiting_kind"], rec["since"] = "waiting", waits[0]["kind"], waits[0]["since"]
        return rec
    rec["state"] = activity
    if since is not None:
        rec["since"] = since
    elif activity != old_state:
        rec["since"] = stamp
    return rec


def _read(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, record: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.stem}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _prune(directory: Path, now: datetime) -> None:
    """Delete state files of long-gone sessions (on SessionStart only, so tool calls stay quick)."""
    cutoff = (now - PRUNE_AFTER).timestamp()
    ended_cutoff = (now - PRUNE_ENDED_AFTER).timestamp()
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.endswith(".json") or entry.name.startswith("."):
                continue
            try:
                mtime = entry.stat().st_mtime
                if mtime < cutoff or (mtime < ended_cutoff and _read(Path(entry.path)).get("state") == "ended"):
                    os.unlink(entry.path)
            except OSError:
                continue


class _Lock:
    """A short exclusive lock on ``<dir>/.lock`` (none on systems without ``fcntl``)."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / ".lock"
        self.fd: int | None = None

    def __enter__(self) -> _Lock:
        try:
            import fcntl
            import time

            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            end = time.monotonic() + LOCK_WAIT
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    if time.monotonic() > end:
                        return self  # someone holds it too long: write anyway
                    time.sleep(0.005)
        except (ImportError, OSError):
            return self

    def __exit__(self, *exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)  # closing releases the lock


def apply(event: dict[str, Any], state_dir: Path, now: datetime, agent: str = AGENTS[0]) -> None:
    """Apply one hook event from ``agent`` to its session's state file in ``state_dir``.

    Malformed events, unknown event names and missing fields are ignored. File system errors are
    raised to the caller (``main`` swallows them; the fake bridge and the tests want to see them).
    """
    if not isinstance(event, dict):
        return
    sid = safe_id(event.get("session_id"))
    name = event.get("hook_event_name")
    if sid is None or not isinstance(name, str) or name not in KNOWN_EVENTS:
        return
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"{sid}.json"
    with _Lock(state_dir):
        record = next_record(_read(path), event, now, agent)
        if record is not None:
            _write(path, record)
    if name == "SessionStart":
        _prune(state_dir, now)


def agent_of(argv: list[str]) -> str:
    """The agent ``--agent NAME`` names, if it is one of ``AGENTS``; Claude Code otherwise."""
    for i, flag in enumerate(argv[:-1]):
        if flag == "--agent" and argv[i + 1] in AGENTS:
            return argv[i + 1]
    return AGENTS[0]


def main(argv: list[str] | None = None) -> int:
    """The hook's entry point: never raises, never prints, always 0."""
    try:
        agent = agent_of(sys.argv[1:] if argv is None else argv)
        event = json.loads(sys.stdin.buffer.read() or b"null")
        if isinstance(event, dict):
            apply(event, state_dir(), datetime.now(timezone.utc), agent)
    except BaseException:  # noqa: BLE001 - a broken hook must never disturb the agent
        pass
    return 0


if __name__ == "__main__":
    try:
        main()
    finally:
        os._exit(0)
