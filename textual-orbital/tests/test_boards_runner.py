"""The runner: a panel's command with the time window in its environment, in its folder, with a deadline."""

from __future__ import annotations

import os
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from textual_orbital.boards.model import PanelSpec
from textual_orbital.boards.runner import PanelError, one_line, panel_env, run

#: The boards example's fake data commands.
FEED = Path(__file__).resolve().parents[1] / "examples" / "boards" / "boards" / "feed.py"
NOW = 1_800_000_000  # 2027-01-15T08:00:00Z


def panel(cmd: str | None = "echo 1", **changes: object) -> PanelSpec:
    args: dict[str, object] = {"id": "3f9a0c1e", "board": "main", "title": "t", "kind": "stat",
                               "added_at": datetime.now(timezone.utc), "cmd": cmd}
    args.update(changes)
    return PanelSpec(**args)  # type: ignore[arg-type]


def feed(*args: str) -> str:
    return shlex.join([sys.executable, str(FEED), *args])


# ------------------------------------------------------------------------------------------ environment


def test_the_command_gets_its_window_in_the_environment() -> None:
    frame = run(panel("env | grep '^PANEL_' | sort", kind="text"), now=NOW)
    env = dict(line.split("=", 1) for line in (frame.text or "").splitlines())
    assert env == {
        "PANEL_START": str(NOW - 900), "PANEL_END": str(NOW),
        "PANEL_START_ISO": "2027-01-15T07:45:00Z", "PANEL_END_ISO": "2027-01-15T08:00:00Z",
        "PANEL_STEP": "15", "PANEL_WINDOW": "15m",
    }
    assert frame.at == NOW


def test_the_step_grows_with_the_window() -> None:
    assert panel_env(900, NOW)["PANEL_STEP"] == "15"
    assert panel_env(3600, NOW)["PANEL_STEP"] == "15"
    assert panel_env(6 * 3600, NOW)["PANEL_STEP"] == "90"
    day = panel_env(86400, datetime.fromtimestamp(NOW, timezone.utc))
    assert day["PANEL_STEP"] == "360" and day["PANEL_WINDOW"] == "1d" and day["PANEL_START"] == str(NOW - 86400)


def test_the_runners_own_environment_is_passed_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOARDS_TEST_VALUE", "from-the-runner")
    assert run(panel("echo $BOARDS_TEST_VALUE", kind="text")).text == "from-the-runner"


def test_a_feed_series_covers_the_window_it_is_given() -> None:
    frame = run(panel(feed("series", "api", "web"), kind="timeseries", window=3600.0), now=NOW)
    assert [s.name for s in frame.series] == ["api", "web"]
    points = frame.series[0].points
    assert points[0][0] == NOW - 3600 and points[-1][0] == NOW and len(points) == 241


# ------------------------------------------------------------------------------------------ folder and stdin


def test_the_command_runs_in_the_panels_folder(tmp_path: Path) -> None:
    assert run(panel("pwd", kind="text", cwd=str(tmp_path))).text == os.path.realpath(tmp_path)


def test_a_folder_that_is_gone_means_the_current_one(tmp_path: Path) -> None:
    gone = tmp_path / "gone"
    assert run(panel("pwd", kind="text", cwd=str(gone))).text == os.path.realpath(os.getcwd())


def test_stdin_is_empty() -> None:
    assert run(panel("read line; echo \"got:$line\"", kind="text")).text == "got:"


# ------------------------------------------------------------------------------------------ deadline


def test_a_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    started = time.monotonic()
    with pytest.raises(PanelError, match=r"^timed out after 1s$"):
        run(panel(f"sleep 30 & echo $! > {pidfile}; sleep 30", timeout=1.0))
    assert time.monotonic() - started < 5
    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the background child {child} outlived the timeout")


# ------------------------------------------------------------------------------------------ failures


def test_a_non_zero_exit_says_the_last_line_of_stderr() -> None:
    cmd = "echo some output; echo 'warming up' >&2; echo 'connection refused' >&2; echo >&2; exit 3"
    with pytest.raises(PanelError, match=r"^connection refused$"):
        run(panel(cmd))


def test_a_silent_failure_says_its_status_or_signal() -> None:
    with pytest.raises(PanelError, match=r"^exited with status 4$"):
        run(panel("exit 4"))
    with pytest.raises(PanelError, match=r"^killed by signal 9$"):
        run(panel("kill -9 $$"))


def test_output_that_cant_be_read_is_what_the_parser_says() -> None:
    with pytest.raises(PanelError, match=r"^bad JSON"):
        run(panel("echo '{\"a\": '"))
    with pytest.raises(PanelError, match=r"^no column 'AGE'; the columns are NAME, STATUS$"):
        run(panel("printf 'NAME  STATUS\\nweb   ok\\n'", kind="table", columns=("AGE",)))


def test_messages_are_one_line_and_short() -> None:
    with pytest.raises(PanelError) as caught:
        run(panel("printf 'x%.0s' $(seq 1 500) >&2; exit 1"))
    assert "\n" not in str(caught.value) and len(str(caught.value)) == 300
    assert one_line("\x1b[31mred\x1b[0m\n\n") == "red"


# ------------------------------------------------------------------------------------------ empty and data


def test_empty_output_is_an_empty_frame_with_what_stderr_said() -> None:
    frame = run(panel("echo 'No results found.' >&2", kind="table"), now=NOW)
    assert frame.shape == "empty" and frame.note == "No results found." and frame.at == NOW
    assert run(panel("true")).note == ""


def test_a_data_panel_runs_no_process() -> None:
    frame = run(panel(None, data={"api": 4, "web": 5}, kind="bars"), now=NOW)
    assert [(s.name, s.points) for s in frame.series] == [("api", ((NOW, 4.0),)), ("web", ((NOW, 5.0),))]
    with pytest.raises(PanelError, match="for a table"):
        run(panel(None, data=42, kind="table"))


def test_kinds_steer_the_reading() -> None:
    assert run(panel(feed("table"), kind="table", columns=("name", "status"))).table is not None
    assert run(panel("echo 42", kind="text")).text == "42"
    assert run(panel(feed("value"), kind="stat")).series[0].last is not None
