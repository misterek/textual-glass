"""The hook, the session model, poses, red alert, the seating chart and the fake bridge. No Textual."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bridge import hook
from bridge.crew import POSES
from bridge.fake import CYCLE, FakeBridge
from bridge.state import (
    ENDED_LINGER,
    STALE_AFTER,
    SeatingChart,
    Session,
    SessionReader,
    pose_for,
    read_sessions,
    red_alert,
    sparking,
)

T0 = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)
SID = "5f1c2b9e-1111-4a4a-9c9c-0123456789ab"


def ev(name: str, sid: str = SID, **fields: Any) -> dict[str, Any]:
    return {"session_id": sid, "hook_event_name": name, "cwd": "/work/api-gateway",
            "transcript_path": "/tmp/t.jsonl", **fields}


def record(state_dir: Path, sid: str = SID) -> dict[str, Any]:
    data = json.loads((state_dir / f"{sid}.json").read_text())
    assert isinstance(data, dict)
    return data


def play(state_dir: Path, *events: dict[str, Any], start: datetime = T0, step: float = 1.0,
         agent: str = "claude") -> dict[str, Any]:
    """Apply events from ``agent`` a ``step`` of seconds apart; the session's record after the last."""
    for i, event in enumerate(events):
        hook.apply(event, state_dir, start + timedelta(seconds=i * step), agent)
    return record(state_dir, events[-1]["session_id"])


def cx(name: str, sid: str = SID, **fields: Any) -> dict[str, Any]:
    """A Codex event: it names the model and the turn on every one."""
    return ev(name, sid, **{"model": "gpt-5-codex", "turn_id": "turn-1", "permission_mode": "default", **fields})


def actors(rec: dict[str, Any]) -> list[str]:
    """Who has a hand up, oldest wait first ("" is the session itself)."""
    return [w["actor"] for w in rec["waits"]]


# -- the hook: README.md's event table, row by row -------------------------------------------------------


def test_session_start_seats_the_session(tmp_path: Path) -> None:
    rec = play(tmp_path, ev("SessionStart", source="startup", model="claude-opus-4"))
    assert rec["state"] == "seated" and rec["model"] == "claude-opus-4"
    assert rec["project"] == "api-gateway" and rec["cwd"] == "/work/api-gateway"
    assert rec["since"] == rec["updated"] == rec["started"] == T0.isoformat()
    assert rec["subagents"] == [] and rec["waiting_kind"] is None and rec["spark_at"] is None
    assert rec["last_event"] == "SessionStart" and rec["session_id"] == SID
    for key in ("session_id", "cwd", "project", "state", "waiting_kind", "since", "updated", "model", "subagents",
                "spark_at", "error", "last_event"):
        assert key in rec, key


@pytest.mark.parametrize("name", ["UserPromptSubmit", "PreToolUse", "PostToolUse", "PostCompact"])
def test_working_events(tmp_path: Path, name: str) -> None:
    assert play(tmp_path, ev("SessionStart"), ev(name, tool_name="Bash"))["state"] == "working"


@pytest.mark.parametrize(("event", "kind"), [
    (ev("PermissionRequest", tool_name="Bash"), "permission"),
    (ev("Notification", notification_type="permission_prompt", message="Claude needs your permission"), "permission"),
    (ev("Notification", notification_type="elicitation_dialog"), "question"),
    (ev("Notification", notification_type="agent_needs_input"), "question"),
    (ev("Notification", message="Claude needs your permission to use Bash"), "permission"),  # older versions
])
def test_waiting_events(tmp_path: Path, event: dict[str, Any], kind: str) -> None:
    rec = play(tmp_path, ev("UserPromptSubmit"), event)
    assert (rec["state"], rec["waiting_kind"]) == ("waiting", kind)


def test_idle_prompt_only_confirms_done(tmp_path: Path) -> None:
    done = play(tmp_path, ev("UserPromptSubmit"), ev("Stop"))
    idle = ev("Notification", notification_type="idle_prompt", message="Claude is waiting for your input")
    rec = play(tmp_path, idle, start=T0 + timedelta(minutes=5))
    assert rec["state"] == "done" and rec["since"] == done["since"] and rec["last_event"] == "Notification"
    working = play(tmp_path, ev("UserPromptSubmit"), ev("Notification", notification_type="idle_prompt"),
                   start=T0 + timedelta(minutes=9))
    assert working["state"] == "working", "an idle prompt never changes the state"


def test_stop_stop_failure_and_compacting(tmp_path: Path) -> None:
    assert play(tmp_path, ev("UserPromptSubmit"), ev("Stop"))["state"] == "done"
    rec = play(tmp_path, ev("UserPromptSubmit"), ev("StopFailure", error="API Error: 529 overloaded"))
    assert rec["state"] == "error" and rec["error"] == "API Error: 529 overloaded"
    assert play(tmp_path, ev("StopFailure", error={"message": "rate limited"}))["error"] == "rate limited"
    assert play(tmp_path, ev("StopFailure"))["error"], "an error without text still says something"
    assert play(tmp_path, ev("UserPromptSubmit"))["error"] == "", "a new prompt clears the error"
    assert play(tmp_path, ev("PreCompact", trigger="auto"))["state"] == "compacting"
    assert play(tmp_path, ev("PostCompact", trigger="auto"))["state"] == "working"


def test_a_manual_compact_goes_back_to_what_the_session_was_doing(tmp_path: Path) -> None:
    done = play(tmp_path, ev("UserPromptSubmit"), ev("Stop"))
    later = T0 + timedelta(minutes=20)
    assert play(tmp_path, ev("PreCompact", trigger="manual"), start=later)["state"] == "compacting"
    rec = play(tmp_path, ev("PostCompact", trigger="manual"), start=later + timedelta(seconds=30))
    assert rec["state"] == "done" and rec["since"] == done["since"], "still your move, for as long as before"
    assert rec["resume_state"] is None and rec["resume_since"] is None
    play(tmp_path, ev("UserPromptSubmit"), ev("PreCompact", trigger="auto"), ev("PreCompact", trigger="auto"))
    rec = play(tmp_path, ev("PostCompact", trigger="auto"))
    assert rec["state"] == "working", "a second PreCompact keeps the first"


def test_only_whoever_raised_the_hand_lowers_it(tmp_path: Path) -> None:
    # a subagent asks: the session's own tool calls (or another subagent's) don't lower the hand
    rec = play(tmp_path, ev("UserPromptSubmit"), ev("SubagentStart", agent_id="a1"),
               ev("PermissionRequest", tool_name="Bash", agent_id="a1"),
               ev("Notification", notification_type="permission_prompt"),
               ev("PreToolUse", tool_name="Read"), ev("PostToolUse", tool_name="Read", agent_id="a2"))
    assert rec["state"] == "waiting" and actors(rec) == ["a1"], "the ~6 s reminder is the same wait"
    assert play(tmp_path, ev("Stop"))["state"] == "waiting", "the subagent's prompt outlives the turn"
    rec = play(tmp_path, ev("PostToolUse", tool_name="Bash", agent_id="a1"))
    assert rec["state"] == "done" and rec["waits"] == [], "answered after the turn ended: your move again"
    # the session asks: a subagent's tool calls don't lower it, the session's own do
    rec = play(tmp_path, ev("PermissionRequest", tool_name="Edit"), ev("PreToolUse", tool_name="Grep", agent_id="a1"))
    assert rec["state"] == "waiting" and actors(rec) == [""]
    assert play(tmp_path, ev("PostToolUse", tool_name="Edit"))["state"] == "working"


def test_a_subagent_that_stops_takes_its_question_with_it(tmp_path: Path) -> None:
    rec = play(tmp_path, ev("UserPromptSubmit"), ev("SubagentStart", agent_id="a1"),
               ev("PermissionRequest", agent_id="a1"), ev("SubagentStop", agent_id="a1"))
    assert rec["state"] == "working" and rec["subagents"] == []


def test_a_background_subagent_after_the_turn_leaves_it_your_move(tmp_path: Path) -> None:
    rec = play(tmp_path, ev("UserPromptSubmit"), ev("SubagentStart", agent_id="bg"), ev("Stop"),
               ev("PreToolUse", tool_name="Bash", agent_id="bg"), ev("PostToolUse", tool_name="Bash", agent_id="bg"))
    assert rec["state"] == "done" and rec["subagents"] == ["bg"], "the ensign shows the background work"


# -- open waits: one per request, each lowered by its own answer -------------------------------------------


def bash(command: str, **fields: Any) -> dict[str, Any]:
    """The fields of a Bash call (Claude Code's tool input has a description of its own)."""
    return {"tool_name": "Bash", "tool_input": {"command": command, "description": "Run it"}, **fields}


def test_a_parallel_sibling_finishing_leaves_the_permission_up(tmp_path: Path) -> None:
    read: dict[str, Any] = {"tool_name": "Read", "tool_input": {"file_path": "/work/api-gateway/README.md"}}
    rec = play(tmp_path, ev("UserPromptSubmit"), ev("PreToolUse", **bash("rm -rf build", tool_use_id="toolu_1")),
               ev("PreToolUse", tool_use_id="toolu_2", **read), ev("PermissionRequest", **bash("rm -rf build")),
               ev("PostToolUse", tool_use_id="toolu_2", **read))
    assert (rec["state"], rec["waiting_kind"]) == ("waiting", "permission"), "the Read finishing isn't an answer"
    assert rec["waits"][0]["key"] == "toolu_1", "the request (no tool_use_id) found its call's PreToolUse"
    assert rec["since"] == (T0 + timedelta(seconds=3)).isoformat()
    rec = play(tmp_path, ev("PostToolUse", **bash("rm -rf build", tool_use_id="toolu_1")))
    assert rec["state"] == "working" and rec["waits"] == [] and rec["calls"] == []


def test_a_parallel_sibling_finishing_leaves_a_codex_question_up(tmp_path: Path) -> None:
    ask: dict[str, Any] = {"tool_name": "request_user_input", "tool_use_id": "call_2",
                           "tool_input": {"questions": ["Which?"]}}
    build: dict[str, Any] = {"tool_name": "Bash", "tool_use_id": "call_1", "tool_input": {"command": "cargo build"}}
    rec = play(tmp_path, cx("UserPromptSubmit"), cx("PreToolUse", **build), cx("PreToolUse", **ask),
               cx("PostToolUse", tool_response="ok", **build), agent="codex")
    assert (rec["state"], rec["waiting_kind"]) == ("waiting", "question")
    assert play(tmp_path, cx("PostToolUse", **ask), agent="codex")["state"] == "working"


@pytest.mark.parametrize("first", ["a1", "a2"])
def test_two_subagents_waiting_are_answered_in_either_order(tmp_path: Path, first: str) -> None:
    def asks(agent: str) -> dict[str, Any]:
        return ev("PermissionRequest", agent_id=agent, **bash(f"make {agent}"))

    def runs(agent: str) -> dict[str, Any]:
        return ev("PostToolUse", agent_id=agent, **bash(f"make {agent}", tool_use_id=f"toolu_{agent}"))

    a1 = play(tmp_path, ev("UserPromptSubmit"), ev("SubagentStart", agent_id="a1"), ev("SubagentStart", agent_id="a2"),
              asks("a1"))
    a2_at = T0 + timedelta(minutes=1)
    rec = play(tmp_path, asks("a2"), start=a2_at)
    assert actors(rec) == ["a1", "a2"] and rec["since"] == a1["since"]
    second = "a2" if first == "a1" else "a1"
    rec = play(tmp_path, runs(first), start=T0 + timedelta(minutes=2))
    assert rec["state"] == "waiting" and actors(rec) == [second], f"{second} still waits"
    assert rec["since"] == (a1["since"] if second == "a1" else a2_at.isoformat()), "the oldest open wait's age"
    rec = play(tmp_path, runs(second), start=T0 + timedelta(minutes=3))
    assert rec["state"] == "working" and rec["since"] == (T0 + timedelta(minutes=3)).isoformat()


def test_a_notification_with_no_request_is_a_wait_of_its_own(tmp_path: Path) -> None:
    # a sandboxed command's network request sends no PermissionRequest, only the notification
    prompt = ev("Notification", notification_type="permission_prompt", message="Claude needs your permission")
    rec = play(tmp_path, ev("UserPromptSubmit"), ev("PreToolUse", **bash("curl example.com", tool_use_id="toolu_1")),
               prompt)
    at = (T0 + timedelta(seconds=2)).isoformat()
    assert rec["state"] == "waiting" and rec["waits"] == [{"actor": "", "kind": "permission", "key": None, "since": at}]
    again = play(tmp_path, prompt, ev("PreToolUse", tool_name="Read", agent_id="a1"), start=T0 + timedelta(minutes=1))
    assert again["waits"] == rec["waits"] and again["since"] == at, "its reminder and a subagent's call change nothing"
    rec = play(tmp_path, ev("PostToolUse", **bash("curl example.com", tool_use_id="toolu_1")))
    assert rec["state"] == "working", "the asker is busy again: with no call to match, that is the answer"


def test_a_subagents_request_outlives_the_turn(tmp_path: Path) -> None:
    rec = play(tmp_path, ev("UserPromptSubmit"), ev("SubagentStart", agent_id="bg"),
               ev("PermissionRequest", agent_id="bg", **bash("npm test")), ev("PermissionRequest", **bash("ls")),
               ev("Stop"))
    assert rec["state"] == "waiting" and actors(rec) == ["bg"] and rec["activity"] == "done", "the turn's own went"
    rec = play(tmp_path, ev("PostToolUse", agent_id="bg", **bash("npm test", tool_use_id="toolu_9")))
    assert rec["state"] == "done", "answered after the turn: your move"
    rec = play(tmp_path, ev("UserPromptSubmit"), ev("PermissionRequest", agent_id="bg", **bash("npm test")),
               ev("SubagentStop", agent_id="bg"))
    assert rec["state"] == "working" and rec["waits"] == [], "a subagent that stops takes its requests with it"


def test_a_denied_permission_stays_up_until_the_next_prompt_or_stop(tmp_path: Path) -> None:
    # neither agent sends an event when the user says no
    asked = play(tmp_path, ev("UserPromptSubmit"), ev("PermissionRequest", **bash("rm -rf /")))
    assert asked["state"] == "waiting"
    assert play(tmp_path, ev("UserPromptSubmit", prompt="no, don't"))["state"] == "working"
    play(tmp_path, ev("PermissionRequest", **bash("rm -rf /")))
    assert play(tmp_path, ev("Stop"))["state"] == "done"
    play(tmp_path, cx("UserPromptSubmit"), cx("PermissionRequest", **bash("rm -rf /")), agent="codex")
    assert play(tmp_path, cx("Interrupt"), agent="codex")["state"] == "done"


def test_a_request_finds_its_call_when_the_answer_changes_the_input(tmp_path: Path) -> None:
    # Claude Code adds the user's answers to AskUserQuestion's input; Codex's request adds why it asks
    asked = {"questions": [{"question": "Which framework?", "options": [{"label": "React"}, {"label": "Vue"}]}]}
    rec = play(tmp_path, ev("UserPromptSubmit"), ev("PreToolUse", tool_name="AskUserQuestion", tool_use_id="toolu_q",
                                                    tool_input=asked),
               ev("PermissionRequest", tool_name="AskUserQuestion", tool_input=asked),
               ev("PostToolUse", tool_name="AskUserQuestion", tool_use_id="toolu_q",
                  tool_input={**asked, "answers": {"Which framework?": "React"}}))
    assert rec["state"] == "working"
    test: dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": "cargo test"}}
    reason: dict[str, Any] = {"tool_name": "Bash",
                              "tool_input": {"command": "cargo test", "description": "needs the network"}}
    for before in ([cx("PreToolUse", tool_use_id="call_7", **test)], []):  # with its PreToolUse, and without
        rec = play(tmp_path, cx("UserPromptSubmit"), *before, cx("PermissionRequest", **reason), agent="codex")
        assert rec["state"] == "waiting"
        assert play(tmp_path, cx("PostToolUse", tool_use_id="call_7", **test), agent="codex")["state"] == "working"


def test_a_subagents_compaction_leaves_the_session_alone(tmp_path: Path) -> None:
    asked = play(tmp_path, ev("UserPromptSubmit"), ev("SubagentStart", agent_id="a1"), ev("PermissionRequest"))
    rec = play(tmp_path, ev("PreCompact", agent_id="a1", trigger="auto"))
    assert (rec["state"], rec["activity"], rec["resume_state"]) == ("waiting", "working", None)
    rec = play(tmp_path, ev("PostCompact", agent_id="a1", trigger="auto"))
    assert (rec["state"], rec["waiting_kind"], rec["since"]) == ("waiting", "permission", asked["since"])
    done = play(tmp_path, ev("PostToolUse"), ev("Stop"))
    rec = play(tmp_path, ev("PreCompact", agent_id="a1"), ev("PostCompact", agent_id="a1"),
               start=T0 + timedelta(minutes=5))
    assert rec["state"] == "done" and rec["since"] == done["since"], "nor after the turn"


def test_the_session_compacting_while_a_hand_is_up(tmp_path: Path) -> None:
    asked = play(tmp_path, ev("UserPromptSubmit"), ev("SubagentStart", agent_id="a1"),
                 ev("PermissionRequest", agent_id="a1", **bash("make")))
    rec = play(tmp_path, ev("PreCompact", trigger="auto"), start=T0 + timedelta(minutes=1))
    assert rec["state"] == "waiting" and rec["activity"] == "compacting" and rec["since"] == asked["since"]
    rec = play(tmp_path, ev("PostCompact", trigger="auto"))
    assert rec["state"] == "waiting" and rec["activity"] == "working", "compaction never touches a wait"
    rec = play(tmp_path, ev("PreCompact"), ev("SubagentStop", agent_id="a1"), start=T0 + timedelta(minutes=2))
    assert rec["state"] == "compacting", "the last wait gone mid-compaction: the compaction shows"
    assert play(tmp_path, ev("PostCompact"))["state"] == "working"


def test_post_compact_goes_back_to_done_under_a_hand(tmp_path: Path) -> None:
    rec = play(tmp_path, ev("UserPromptSubmit"), ev("SubagentStart", agent_id="bg"),
               ev("PermissionRequest", agent_id="bg", **bash("make")), ev("Stop"),
               ev("PreCompact", trigger="manual"), ev("PostCompact", trigger="manual"))
    assert (rec["state"], rec["activity"]) == ("waiting", "done")
    rec = play(tmp_path, ev("PostToolUse", agent_id="bg", **bash("make", tool_use_id="toolu_1")),
               start=T0 + timedelta(minutes=1))
    assert rec["state"] == "done" and rec["since"] == (T0 + timedelta(minutes=1)).isoformat()


def test_waits_and_calls_are_capped(tmp_path: Path) -> None:
    rec = play(tmp_path, ev("UserPromptSubmit"), *[ev("PermissionRequest", **bash(f"step {i}")) for i in range(20)])
    assert len(rec["waits"]) == hook.MAX_WAITS == 16, "the newest are kept"
    assert rec["since"] == rec["waits"][0]["since"] == (T0 + timedelta(seconds=5)).isoformat()
    rec = play(tmp_path, *[ev("PreToolUse", **bash(f"step {i}", tool_use_id=f"toolu_{i}")) for i in range(50)])
    assert len(rec["calls"]) == hook.MAX_CALLS and rec["calls"][-1][1] == "toolu_49"


@pytest.mark.parametrize("waits", [
    "nonsense", 3, {"actor": ""},
    [1, None, "x", [], {"actor": 5, "kind": "permission", "key": None, "since": "t"},
     {"actor": "", "kind": "nap", "key": None, "since": "t"}, {"actor": "", "kind": "question", "key": 7, "since": "t"},
     {"actor": "../x", "kind": "question", "key": None, "since": "t"}, {"actor": "", "kind": "question", "key": None}],
])
def test_garbage_in_the_waits_list_is_no_wait(tmp_path: Path, waits: Any) -> None:
    write(tmp_path, SID, state="waiting", waiting_kind="permission", activity="done", since=T0.isoformat(),
          waits=waits, calls=[["a"], [1, 2], "x"])
    rec = play(tmp_path, ev("Notification", notification_type="idle_prompt"), start=T0 + timedelta(minutes=1))
    assert (rec["state"], rec["waits"], rec["calls"], rec["waiting_kind"]) == ("done", [], [], None)


def test_a_file_from_before_the_waits_list(tmp_path: Path) -> None:
    write(tmp_path, SID, state="waiting", waiting_kind="question", waiting_agent="a1", since=T0.isoformat(),
          subagents=["a1"])
    rec = play(tmp_path, ev("PostToolUse", tool_name="Read"), start=T0 + timedelta(minutes=1))
    assert rec["waits"] == [{"actor": "a1", "kind": "question", "key": None, "since": T0.isoformat()}]
    assert (rec["state"], rec["since"]) == ("waiting", T0.isoformat()) and "waiting_agent" not in rec
    rec = play(tmp_path, ev("PreToolUse", tool_name="Bash", agent_id="a1"), start=T0 + timedelta(minutes=2))
    assert rec["state"] == "working" and rec["waits"] == [], "the asker's next call lowers a hand with no call to match"
    write(tmp_path, SID, state="waiting", activity="waiting")
    rec = play(tmp_path, ev("Notification", notification_type="idle_prompt"))
    assert rec["waits"][0]["kind"] == "permission" and rec["activity"] == "working"


def test_tool_failure_sparks_unless_interrupted(tmp_path: Path) -> None:
    before = play(tmp_path, ev("UserPromptSubmit"))
    at = T0 + timedelta(minutes=3)
    rec = play(tmp_path, ev("PostToolUseFailure", tool_name="Bash", error="exit 1", is_interrupt=False), start=at)
    assert rec["state"] == "working" and rec["since"] == before["since"], "the state is unchanged"
    assert rec["spark_at"] == at.isoformat() and rec["error"] == "exit 1"
    later = T0 + timedelta(minutes=4)
    rec = play(tmp_path, ev("PostToolUseFailure", error="interrupted", is_interrupt=True), start=later)
    assert rec["spark_at"] == at.isoformat(), "an interrupt is the user's doing: no spark"


def test_subagents_come_and_go(tmp_path: Path) -> None:
    rec = play(tmp_path, ev("UserPromptSubmit"), ev("SubagentStart", agent_id="a1", agent_type="Explore"),
               ev("SubagentStart", agent_id="a2"), ev("SubagentStart", agent_id="a1"))
    assert rec["subagents"] == ["a2", "a1"] and rec["state"] == "working"
    assert play(tmp_path, ev("SubagentStop", agent_id="a1"))["subagents"] == ["a2"]
    assert play(tmp_path, ev("SubagentStop", agent_id="nobody"))["subagents"] == ["a2"]
    assert play(tmp_path, ev("SubagentStop"))["subagents"] == ["a2"], "no agent_id: nothing to remove"
    assert play(tmp_path, ev("SubagentStart", agent_id=None))["subagents"] == ["a2"]
    many = [ev("SubagentStart", agent_id=f"x{i}") for i in range(50)]
    assert len(play(tmp_path, *many)["subagents"]) == hook.MAX_SUBAGENTS


def test_model_switch_and_session_end(tmp_path: Path) -> None:
    rec = play(tmp_path, ev("SessionStart", model={"id": "claude-opus-4", "display_name": "Opus"}),
               ev("PostModelSwitch", to_model="claude-sonnet-4"))
    assert rec["model"] == "claude-sonnet-4" and rec["state"] == "seated"
    assert play(tmp_path, ev("PostModelSwitch"))["model"] == "claude-sonnet-4", "a missing model keeps the old one"
    ended = play(tmp_path, ev("SubagentStart", agent_id="a1"), ev("SessionEnd", reason="clear"))
    assert ended["state"] == "ended" and ended["subagents"] == []
    late = play(tmp_path, ev("PostToolUse"), ev("Stop"), start=T0 + timedelta(minutes=1))
    assert late == ended, "late events never bring an ended session back"
    back = play(tmp_path, ev("SessionStart", source="resume"), start=T0 + timedelta(minutes=2))
    assert back["state"] == "seated" and back["started"] == (T0 + timedelta(minutes=2)).isoformat()


def test_since_changes_only_with_the_state(tmp_path: Path) -> None:
    first = play(tmp_path, ev("UserPromptSubmit"))
    rec = play(tmp_path, ev("PreToolUse"), ev("PostToolUse"), ev("SubagentStart", agent_id="a"),
               start=T0 + timedelta(minutes=1))
    assert rec["since"] == first["since"] and rec["updated"] == (T0 + timedelta(minutes=1, seconds=2)).isoformat()
    wait = play(tmp_path, ev("PermissionRequest"), start=T0 + timedelta(minutes=2))
    assert wait["since"] == (T0 + timedelta(minutes=2)).isoformat()
    again = play(tmp_path, ev("Notification", notification_type="permission_prompt"), start=T0 + timedelta(minutes=3))
    assert again["since"] == wait["since"], "the prompt's notification is the same wait"
    question = play(tmp_path, ev("Notification", notification_type="elicitation_dialog"),
                    start=T0 + timedelta(minutes=4))
    assert question["since"] == wait["since"] and question["waiting_kind"] == "permission", "the oldest wait's"
    assert [w["kind"] for w in question["waits"]] == ["permission", "question"]
    answered = play(tmp_path, ev("PostToolUse"), start=T0 + timedelta(minutes=5))
    assert answered["since"] == (T0 + timedelta(minutes=5)).isoformat() and answered["waiting_kind"] is None


@pytest.mark.parametrize("event", [
    {"hook_event_name": "Stop"},                                   # no session id
    {"session_id": SID},                                           # no event name
    {"session_id": SID, "hook_event_name": "SomethingNew"},        # an event from a newer Claude Code
    {"session_id": "../../etc/passwd", "hook_event_name": "Stop"},  # never a path
    {"session_id": ".hidden", "hook_event_name": "Stop"},
    {"session_id": "x" * 500, "hook_event_name": "Stop"},
    {"session_id": 12, "hook_event_name": "Stop"},
    {"session_id": SID, "hook_event_name": ["Stop"]},
])
def test_malformed_events_write_nothing(tmp_path: Path, event: dict[str, Any]) -> None:
    hook.apply(event, tmp_path, T0)
    assert not list(tmp_path.glob("*.json"))


def test_odd_fields_are_tolerated(tmp_path: Path) -> None:
    hook.apply("not a dict", tmp_path, T0)  # type: ignore[arg-type]
    rec = play(tmp_path, {"session_id": SID, "hook_event_name": "SessionStart", "cwd": 7, "model": 5})
    assert rec["state"] == "seated" and rec["cwd"] == "" and rec["project"] == "" and rec["model"] == ""
    rec = play(tmp_path, ev("StopFailure", error=["a", "list"]), ev("Notification", notification_type=None))
    assert rec["state"] == "error"
    naive = play(tmp_path, ev("UserPromptSubmit"), start=datetime(2026, 11, 9, 10, 0))
    assert naive["since"].endswith("+00:00"), "a naive now is taken as UTC"


def test_a_broken_state_file_is_replaced(tmp_path: Path) -> None:
    (tmp_path / f"{SID}.json").write_text("{half a file")
    rec = play(tmp_path, ev("PreToolUse"))
    assert rec["state"] == "working" and rec["since"] == T0.isoformat()
    (tmp_path / f"{SID}.json").write_text('["a list"]')
    assert play(tmp_path, ev("Stop"))["state"] == "done"
    (tmp_path / f"{SID}.json").write_text('{"state": "nonsense", "subagents": "x", "since": 3}')
    assert play(tmp_path, ev("Stop"))["state"] == "done"


def test_writes_are_atomic_and_leave_no_temporary_files(tmp_path: Path) -> None:
    play(tmp_path, *[ev("PreToolUse") for _ in range(5)])
    assert sorted(p.name for p in tmp_path.iterdir()) == [".lock", f"{SID}.json"]


def test_concurrent_subagent_stops_lose_nothing(tmp_path: Path) -> None:
    play(tmp_path, *[ev("SubagentStart", agent_id=f"a{i}") for i in range(12)])
    threads = [threading.Thread(target=hook.apply, args=(ev("SubagentStop", agent_id=f"a{i}"), tmp_path, T0))
               for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert record(tmp_path)["subagents"] == []


def test_session_start_prunes_long_gone_sessions(tmp_path: Path) -> None:
    old, ended, fresh = tmp_path / "old.json", tmp_path / "ended.json", tmp_path / "fresh.json"
    old.write_text('{"state": "done"}')
    ended.write_text('{"state": "ended"}')
    fresh.write_text('{"state": "ended"}')
    now = datetime.now(timezone.utc)
    os.utime(old, ((now - timedelta(days=3)).timestamp(),) * 2)
    os.utime(ended, ((now - timedelta(hours=2)).timestamp(),) * 2)
    hook.apply(ev("SessionStart"), tmp_path, now)
    assert not old.exists() and not ended.exists() and fresh.exists() and (tmp_path / f"{SID}.json").exists()


def test_main_never_raises_and_always_returns_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE", str(tmp_path))
    for raw in (b"", b"garbage", b"[1, 2]", b"null", b'{"session_id": 3}', b"\xff\xfe", b'{"a":' * 5000):
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
        assert hook.main() == 0
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(ev("Stop")).encode())))
    assert hook.main() == 0 and record(tmp_path)["state"] == "done"

    class Broken:
        @property
        def buffer(self) -> Any:
            raise OSError("stdin is gone")

    monkeypatch.setattr(sys, "stdin", Broken())
    assert hook.main() == 0
    unwritable = tmp_path / "file-not-dir"
    unwritable.write_text("")
    monkeypatch.setenv("BRIDGE_STATE", str(unwritable / "sessions"))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(ev("Stop")).encode())))
    assert hook.main() == 0, "a state dir that can't be made is swallowed too"


def test_the_hook_script_exits_0_and_prints_nothing(tmp_path: Path) -> None:
    script = Path(hook.__file__)
    env = {**os.environ, "BRIDGE_STATE": str(tmp_path)}
    for raw in (b"not json", json.dumps(ev("PermissionRequest")).encode()):
        done = subprocess.run([sys.executable, str(script)], input=raw, capture_output=True, env=env, timeout=10)
        assert done.returncode == 0 and done.stdout == b"" and done.stderr == b""
    assert record(tmp_path)["state"] == "waiting" and record(tmp_path)["agent"] == "claude"
    for args in (["--agent", "codex"], ["--agent"], ["--agent", "nobody", "--more"]):
        raw = json.dumps(cx("Interrupt")).encode()
        done = subprocess.run([sys.executable, str(script), *args], input=raw, capture_output=True, env=env, timeout=10)
        assert done.returncode == 0 and done.stdout == b"" and done.stderr == b""
        assert record(tmp_path)["agent"] == ("codex" if args[1:] == ["codex"] else "claude")


# -- Codex ------------------------------------------------------------------------------------------------


def test_the_agent_comes_from_the_command_line() -> None:
    assert hook.agent_of(["--agent", "codex"]) == "codex"
    assert hook.agent_of(["--agent", "claude"]) == hook.agent_of([]) == "claude"
    assert hook.agent_of(["--agent"]) == hook.agent_of(["--agent", "nobody"]) == "claude", "unknown: Claude Code"
    assert set(hook.EVENTS) == set(hook.AGENTS) and hook.AGENTS[0] == "claude"
    assert set(hook.EVENTS["codex"]) <= hook.KNOWN_EVENTS and set(hook.EVENTS["claude"]) <= hook.KNOWN_EVENTS


def test_codex_events_become_the_same_states(tmp_path: Path) -> None:
    rec = play(tmp_path, cx("SessionStart", source="startup"), agent="codex")
    assert (rec["state"], rec["agent"], rec["model"]) == ("seated", "codex", "gpt-5-codex")
    assert play(tmp_path, cx("UserPromptSubmit", prompt="go"), agent="codex")["state"] == "working"
    rec = play(tmp_path, cx("PermissionRequest", tool_name="Bash"), agent="codex")
    assert (rec["state"], rec["waiting_kind"]) == ("waiting", "permission")
    assert play(tmp_path, cx("PostToolUse", tool_name="Bash"), agent="codex")["state"] == "working"
    assert play(tmp_path, cx("PreCompact", trigger="auto"), agent="codex")["state"] == "compacting"
    assert play(tmp_path, cx("PostCompact", trigger="auto"), agent="codex")["state"] == "working"
    assert play(tmp_path, cx("Stop", stop_hook_active=False), agent="codex")["state"] == "done"
    assert play(tmp_path, cx("SessionEnd", reason="other"), agent="codex")["state"] == "ended"


def test_codex_interrupt_is_your_move(tmp_path: Path) -> None:
    rec = play(tmp_path, cx("UserPromptSubmit"), cx("PermissionRequest", tool_name="Bash"), cx("Interrupt"),
               agent="codex")
    assert (rec["state"], rec["waiting_kind"], rec["waits"]) == ("done", None, [])


def test_codex_failed_model_request_is_an_error_until_a_retry_gets_through(tmp_path: Path) -> None:
    rec = play(tmp_path, cx("UserPromptSubmit"), cx("PostModelRequest", status="failed", error="stream disconnected"),
               agent="codex")
    assert rec["state"] == "error" and rec["error"] == "stream disconnected"
    assert play(tmp_path, cx("PostModelRequest", status="failed", error=None), agent="codex")["error"]
    rec = play(tmp_path, cx("PostModelRequest", status="completed"), agent="codex")
    assert rec["state"] == "working", "the retry got through"
    assert play(tmp_path, cx("PostModelRequest", status="completed"), agent="codex")["state"] == "working"
    rec = play(tmp_path, cx("PostModelRequest", status="failed", agent_id="sub1"),
               cx("PostModelRequest", status="cancelled"), agent="codex")
    assert rec["state"] == "working", "a subagent's failure, and a cancelled request, change nothing"
    done = play(tmp_path, cx("Stop"), cx("PostModelRequest", status="completed"), agent="codex")
    assert done["state"] == "done", "a completed request never wakes a finished turn"


def test_codex_asks_a_question_through_a_tool(tmp_path: Path) -> None:
    rec = play(tmp_path, cx("UserPromptSubmit"), cx("PreToolUse", tool_name="request_user_input"), agent="codex")
    assert (rec["state"], rec["waiting_kind"]) == ("waiting", "question")
    assert play(tmp_path, cx("PostToolUse", tool_name="request_user_input"), agent="codex")["state"] == "working"


def test_the_model_follows_the_session_not_its_subagents(tmp_path: Path) -> None:
    play(tmp_path, cx("SessionStart"), agent="codex")
    rec = play(tmp_path, cx("PreToolUse", model="gpt-5.1-codex"), agent="codex")
    assert rec["model"] == "gpt-5.1-codex", "Codex names the model on every event"
    rec = play(tmp_path, cx("SubagentStart", agent_id="a1", model="gpt-5-mini"),
               cx("PreToolUse", agent_id="a1", model="gpt-5-mini"), agent="codex")
    assert rec["model"] == "gpt-5.1-codex" and rec["subagents"] == ["a1"]


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_a_session_start_after_a_compaction_is_the_same_session(tmp_path: Path, agent: str) -> None:
    done = play(tmp_path, ev("UserPromptSubmit"), ev("SubagentStart", agent_id="bg"), ev("Stop"), agent=agent)
    later = T0 + timedelta(minutes=20)
    rec = play(tmp_path, ev("PreCompact", trigger="manual"), ev("PostCompact", trigger="manual"),
               ev("SessionStart", source="compact"), start=later, agent=agent)
    assert rec["state"] == "done" and rec["since"] == done["since"] and rec["subagents"] == ["bg"]
    rec = play(tmp_path, ev("SessionStart", source="clear"), start=later + timedelta(minutes=1), agent=agent)
    assert rec["state"] == "seated" and rec["subagents"] == [], "a cleared conversation starts again"


@pytest.mark.parametrize("name", hook.EVENTS["codex"])
def test_codex_events_with_missing_or_odd_fields_never_raise(tmp_path: Path, name: str) -> None:
    hook.apply({"session_id": SID, "hook_event_name": name}, tmp_path, T0, "codex")
    odd = {"session_id": SID, "hook_event_name": name, "model": ["x"], "status": 3, "error": {"no": "text"},
           "tool_name": None, "agent_id": 5, "source": {}, "cwd": None, "turn_id": 1.5}
    hook.apply(odd, tmp_path, T0 + timedelta(seconds=1), "codex")
    rec = record(tmp_path)
    assert rec["agent"] == "codex" and rec["last_event"] == name and rec["state"] in hook.STATES


# -- the session model -----------------------------------------------------------------------------------


def session(state: str, since: datetime = T0, sid: str = "s1", **kw: Any) -> Session:
    return Session(id=sid, state=state, since=since, updated=kw.pop("updated", since), started=kw.pop("started", since),
                   **kw)


@pytest.mark.parametrize(("state", "minutes", "pose"), [
    ("seated", 0, "seated"), ("working", 50, "typing"), ("error", 5, "jolt"), ("compacting", 1, "tea"),
    ("ended", 0, "empty"), ("something-new", 0, "seated"),
    ("done", 0, "done"), ("done", 29.9, "done"), ("done", 30, "dozing"), ("done", 300, "dozing"),
    ("waiting", 0, "hand_up"), ("waiting", 1.9, "hand_up"), ("waiting", 2, "waving"), ("waiting", 9.9, "waving"),
    ("waiting", 10, "standing"), ("waiting", 120, "standing"),
])
def test_pose_escalates_with_age(state: str, minutes: float, pose: str) -> None:
    assert pose_for(session(state), T0 + timedelta(minutes=minutes)) == pose
    assert pose in POSES


def test_no_session_is_an_empty_station() -> None:
    assert pose_for(None, T0) == "empty"


def test_spark_shows_for_a_minute() -> None:
    s = session("working", spark_at=T0)
    assert sparking(s, T0 + timedelta(seconds=59)) and not sparking(s, T0 + timedelta(seconds=61))
    assert not sparking(session("working"), T0) and not sparking(None, T0)


def test_red_alert() -> None:
    assert not red_alert([])
    assert not red_alert([session("waiting", sid="a"), session("working", sid="b")])
    assert red_alert([session("waiting", sid="a"), session("waiting", sid="b")])
    assert red_alert([session("error", sid="a")])
    assert not red_alert([session("done", sid="a"), session("compacting", sid="b")])


def test_session_from_odd_records() -> None:
    mtime = T0
    assert Session.from_record([1], "x", mtime) is None
    assert Session.from_record({"state": 3}, "x", mtime) is None
    s = Session.from_record({"state": "working", "since": "garbage", "subagents": [1, "a"], "cwd": "/w/infra/"},
                            "file-id", mtime)
    assert s is not None and s.id == "file-id" and s.since == mtime and s.subagents == ("a",) and s.project == "infra"
    z = Session.from_record({"state": "done", "since": "2026-11-09T09:00:00Z", "updated": "2026-11-09T09:00:00"},
                            "z", mtime)
    assert z is not None and z.since == T0 and z.updated == T0, "Z and naive times are UTC"
    assert z.agent == "claude", "a file from before the agent was recorded is Claude Code's"
    c = Session.from_record({"state": "working", "agent": "codex"}, "c", mtime)
    assert c is not None and c.agent == "codex"


def write(state_dir: Path, sid: str, **fields: Any) -> Path:
    path = state_dir / f"{sid}.json"
    path.write_text(json.dumps({"session_id": sid, **fields}))
    return path


def test_reader_rereads_only_changed_files_and_tolerates_garbage(tmp_path: Path) -> None:
    stamp = T0.isoformat()
    write(tmp_path, "a", state="working", since=stamp, updated=stamp)
    write(tmp_path, "b", state="done", since=stamp, updated=stamp)
    (tmp_path / "junk.json").write_text("{nope")
    (tmp_path / "list.json").write_text("[]")
    (tmp_path / ".a.123.tmp").write_text("{}")
    (tmp_path / ".lock").write_text("")
    (tmp_path / "notes.txt").write_text("hi")
    reader = SessionReader(tmp_path)
    assert [s.id for s in reader.read(T0)] == ["a", "b"]
    loads = reader.loads
    assert loads == 4
    assert [s.id for s in reader.read(T0)] == ["a", "b"] and reader.loads == loads, "nothing changed: no re-reads"
    os.replace(write(tmp_path, "tmp", state="waiting", since=stamp, updated=stamp), tmp_path / "b.json")
    sessions = reader.read(T0)
    assert reader.loads == loads + 1 and [s.state for s in sessions] == ["working", "waiting"]
    (tmp_path / "a.json").unlink()
    assert [s.id for s in reader.read(T0)] == ["b"]
    assert SessionReader(tmp_path / "missing").read(T0) == []


def test_reader_forgets_ended_and_stale_sessions(tmp_path: Path) -> None:
    write(tmp_path, "gone", state="ended", since=T0.isoformat(), updated=T0.isoformat())
    write(tmp_path, "old", state="working", since=T0.isoformat(), updated=T0.isoformat())
    assert {s.id for s in read_sessions(tmp_path, T0 + ENDED_LINGER)} == {"gone", "old"}, "an ended one shows briefly"
    later = T0 + ENDED_LINGER + timedelta(seconds=1)
    assert [s.id for s in read_sessions(tmp_path, later)] == ["old"]
    assert read_sessions(tmp_path, T0 + STALE_AFTER + timedelta(seconds=1)) == []


# -- the seating chart --------------------------------------------------------------------------------------


def test_members_and_stations_are_stable_and_unique() -> None:
    chart = SeatingChart(["ash", "bo", "cy", "dee"], stations=6)
    a, b, c = (session("working", T0 + timedelta(minutes=i), sid=s) for i, s in enumerate("abc"))
    seats = chart.seat([c, a, b])
    assert [s.id if s else None for s in seats] == [None, "c", "a", "b", None, None], "the middle fills first"
    assert chart.member_of == {"a": "ash", "b": "bo", "c": "cy"}, "roster order, oldest session first"
    seats = chart.seat([b, c])                                   # a leaves
    assert chart.station_of == {"b": 3, "c": 1} and chart.member_of == {"b": "bo", "c": "cy"}
    d = session("working", T0 + timedelta(minutes=5), sid="d")
    seats = chart.seat([b, c, d])
    assert chart.station_of["d"] == 2 and chart.member_of["d"] == "ash", "the first free station and member"
    e, f, g = (session("working", T0 + timedelta(minutes=6 + i), sid=s) for i, s in enumerate("efg"))
    chart.seat([b, c, d, e, f, g])
    members = list(chart.member_of.values())
    assert chart.member_of["e"] == "dee" and len(set(members[:4])) == 4
    assert sorted(chart.station_of.values()) == list(range(6))
    h = session("working", T0 + timedelta(minutes=9), sid="h")
    seats = chart.seat([b, c, d, e, f, g, h])
    assert "h" not in chart.station_of and chart.unseated([b, c, d, e, f, g, h]) == 1, "no room: below deck"
    chart.seat([c, d, e, f, g, h])                               # b leaves: h takes its station
    assert chart.station_of["h"] == 3 and chart.station_of["c"] == 1


def test_an_empty_roster_still_seats() -> None:
    chart = SeatingChart([], stations=2)
    assert chart.seat([session("working")]) == [None, session("working")] or chart.station_of == {"s1": 0}


# -- the fake bridge -----------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_the_fake_script_goes_through_every_state(tmp_path: Path) -> None:
    clock = Clock()
    fake = FakeBridge(tmp_path, speed=20, clock=clock, start=T0)
    reader = SessionReader(tmp_path)
    poses: set[str] = set()
    states: set[str] = set()
    ensigns = alerts = 0
    sparks = False
    while clock.t <= CYCLE * 60 / 20 + 5:  # one loop and a bit, polled every real second
        fake.step()
        sessions = reader.read(fake.now())
        poses |= {pose_for(s, fake.now()) for s in sessions}
        states |= {s.state for s in sessions}
        ensigns = max([ensigns, *(len(s.subagents) for s in sessions)])
        alerts += red_alert([s for s in sessions if s.present])
        sparks = sparks or any(sparking(s, fake.now()) for s in sessions)
        assert len(sessions) <= 6
        clock.t += 1.0
    assert poses == set(POSES), set(POSES) - poses
    assert states == {"seated", "working", "waiting", "done", "error", "compacting", "ended"}
    assert ensigns == 3 and alerts > 0 and sparks
    assert clock.t * 20 / 60 >= CYCLE, "every state within one 40-minute loop: two real minutes at speed 20"


def test_the_fake_loops_with_new_visitors(tmp_path: Path) -> None:
    clock = Clock()
    fake = FakeBridge(tmp_path, speed=20, clock=clock, start=T0, at=CYCLE + 2)
    fake.step()
    ids = {p.stem for p in tmp_path.glob("*.json")}
    assert "fake-mobile-app-1" in ids and "fake-mobile-app-0" in ids and "fake-web-frontend-0" in ids
    present = {s.id for s in fake.sessions()}
    assert present == {"fake-api-gateway", "fake-docs-site", "fake-infra", "fake-billing-service", "fake-mobile-app-1"}


def test_the_fakes_codex_visitor(tmp_path: Path) -> None:
    """web-frontend is a Codex session: a permission, a failed request and its retry, a question, an interrupt."""
    clock = Clock()
    fake = FakeBridge(tmp_path, speed=20, clock=clock, start=T0, at=20.0)
    seen: list[tuple[str, str | None]] = []
    while clock.t * 20 / 60 < 19:
        fake.step()
        web = {s.id: s for s in fake.sessions()}.get("fake-web-frontend-0")
        if web is not None:
            assert web.agent == "codex" and web.model == "gpt-5-codex"
            if not seen or seen[-1] != (web.state, web.waiting_kind):
                seen.append((web.state, web.waiting_kind))
        clock.t += 1.0
    assert seen == [("seated", None), ("working", None), ("waiting", "permission"), ("working", None),
                    ("error", None), ("working", None), ("waiting", "question"), ("working", None), ("done", None),
                    ("working", None), ("done", None)], seen
    others = {s.agent for s in fake.reader.read(fake.now()) if s.id != "fake-web-frontend-0"}
    assert others == {"claude"}
    fake = FakeBridge(tmp_path / "keys", speed=20, clock=Clock(), start=T0, at=27.6)
    fake.step()
    broken = [fake.make_error() for _ in range(6)]
    assert "fake-web-frontend-0" in broken, broken
    assert record(tmp_path / "keys", "fake-web-frontend-0")["last_event"] == "PostModelRequest", "e, the Codex way"


def test_the_fake_keys(tmp_path: Path) -> None:
    clock = Clock()
    fake = FakeBridge(tmp_path, speed=20, clock=clock, start=T0, at=1.5)
    fake.step()
    by_id = {s.id: s for s in fake.sessions()}
    new = fake.add_session()
    assert {s.id: s for s in fake.sessions()}[new].state == "seated" and new not in by_id
    waiter = fake.make_wait()
    assert waiter is not None and {s.id: s for s in fake.sessions()}[waiter].state == "waiting"
    assert fake.answer_all() == 1 and all(s.state != "waiting" for s in fake.sessions())
    broken = fake.make_error()
    assert broken is not None and {s.id: s for s in fake.sessions()}[broken].state == "error"
    gone = fake.end_one()
    assert gone == new and gone not in {s.id for s in fake.sessions()}
    clock.t += 30                                     # ten virtual minutes on: the script leaves it alone
    fake.step()
    assert gone not in {s.id for s in fake.sessions()}
    paused_at = fake.now()
    assert fake.toggle_pause()
    clock.t += 100
    assert fake.now() == paused_at and fake.step() == 0
    assert not fake.toggle_pause()
    clock.t += 3
    assert fake.now() == paused_at + timedelta(minutes=1)
