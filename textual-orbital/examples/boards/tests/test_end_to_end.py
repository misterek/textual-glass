"""The whole loop, as an agent and the user will use it: the app runs in a pty over a folder, and panel files are
written and removed through ``Store`` (what an app's front door for agents calls) while it runs; ``+`` grows a
panel and its file says so. Then ``x`` and ``y`` remove one from the app. About 30 seconds."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from textual_glass.testing.harness import Step, replay_steps, run_app
from textual_orbital.boards import Kind, PanelSpec, Store

ROOT = Path(__file__).resolve().parent.parent
SIZE = (90, 28)


def add(folder: Path, title: str, kind: Kind, cmd: str, **fields: Any) -> PanelSpec:
    """Write one panel, the way an app's CLI would after checking the command."""
    store = Store(folder)
    return store.add(PanelSpec(id=store.new_id(), board=fields.pop("board", "main"), title=title, kind=kind,
                               cmd=cmd, added_at=datetime.now(timezone.utc), cwd=str(folder.parent), **fields))


def test_panels_written_to_the_folder_show_in_the_running_app(tmp_path: Path) -> None:
    folder = tmp_path / "boards"
    done: dict[str, Any] = {}

    def probe(label: str, pid: int) -> None:
        if label == "add":
            done[label] = add(folder, "Ping", "stat", "echo 42", unit="ms", every=1.0)
        elif label == "add-ops":
            done[label] = add(folder, "Queue", "bars", "printf 'email 3\\nsearch 5\\n'", board="ops", key="queue",
                              expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        elif label == "replace-ops":
            done[label] = add(folder, "Queue v2", "bars", "printf 'email 4\\nsearch 9\\n'", board="ops", key="queue")
        elif label == "remove-ops":
            store = Store(folder)
            done[label] = [store.remove(p.id) for p in store.panels("ops") if p.key == "queue"]
        elif label == "grown":
            done[label] = " ".join(spec.size for spec in Store(folder).panels())

    steps = [
        Step(4.0, "empty"),
        Step(0.0, "add"),
        Step(4.0, "added"),
        Step(0.0, "grow", "+"),
        Step(1.5, "grown"),
        Step(0.0, "add-ops"),
        Step(4.0, "two-boards"),
        Step(0.0, "replace-ops"),
        Step(4.0, "replaced"),
        Step(0.0, "remove-ops"),
        Step(4.0, "ops-gone"),
        Step(0.0, "x", "x"),
        Step(1.0, "confirm"),
        Step(0.0, "yes", "y"),
        Step(4.0, "gone"),
        Step(0.0, "quit", "q"),
    ]
    capture = run_app([sys.executable, "app.py", "--dir", str(folder)], steps, str(ROOT), size=SIZE, timeout=90.0,
                      probe=probe)
    assert capture.code == 0 and b"Traceback" not in capture.raw, capture.raw[-3000:]
    snaps = {label: rp.snapshot() for label, rp in replay_steps(capture, SIZE)}

    assert "No panels yet" in snaps["empty"].all_text()
    added = snaps["added"].all_text()
    assert "Ping" in added and "42" in added, "a panel written to the folder shows up while the app runs"
    assert done["grown"] == "medium", "+ grew the stat (small) to medium, in its file"

    two = snaps["two-boards"].all_text()
    assert "ops 1" in two and "main 1" in two, "a new board appears in the board row"
    first, second = done["add-ops"], done["replace-ops"]
    assert second.id == first.id and second.updated_at is not None, "the same key on the same board replaced it"
    assert "ops 1" in snaps["replaced"].all_text(), "replacing by key adds no second panel"

    assert [spec.title for spec in done["remove-ops"]] == ["Queue v2"]
    assert "ops" not in snaps["ops-gone"].all_text().split("\n")[1], "the board went with its last panel"

    assert 'Remove "Ping"? y / n' in snaps["confirm"].all_text()
    assert "No panels yet" in snaps["gone"].all_text(), "x then y removed it from the screen"
    assert Store(folder).panels() == [], "and from disk"
