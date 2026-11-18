"""The store: one JSON file per panel, atomic writes, expiry, replace by key, many writers; and the source."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from textual_orbital.boards.model import ID_RE, PanelSpec, SpecError
from textual_orbital.boards.source import BoardsSource
from textual_orbital.boards.store import Store

HERE = Path(__file__).resolve().parent.parent


def now() -> datetime:
    return datetime.now(timezone.utc)


def make(store: Store, title: str = "Ping", board: str = "main", **changes: object) -> PanelSpec:
    args: dict[str, object] = {"id": store.new_id(), "board": board, "title": title, "kind": "stat",
                               "added_at": now(), "cmd": "echo 1"}
    args.update(changes)
    return PanelSpec(**args)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "boards")


def files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


# ------------------------------------------------------------------------------------------ basics


def test_the_default_root_is_boards_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOARDS_DIR", str(tmp_path / "x"))
    assert Store().root == tmp_path / "x"
    monkeypatch.delenv("BOARDS_DIR")
    assert Store().root == Path("~/.cache/boards").expanduser()


def test_an_added_panel_is_one_private_json_file_in_its_board_folder(store: Store) -> None:
    spec = store.add(make(store, data=None, unit="ms"))
    path = store.root / "main" / f"{spec.id}.json"
    assert files(store.root) == [f"main/{spec.id}.json"]
    assert PanelSpec.from_json(json.loads(path.read_text())) == spec
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.root / "main").stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.panels() == [spec] and store.get(spec.id) == spec and store.problems == []


def test_a_missing_root_has_no_panels_and_an_unreadable_one_raises(store: Store) -> None:
    assert store.panels() == [] and store.boards() == [] and store.signature() == ()
    assert store.get("3f9a0c1e") is None and store.remove("3f9a0c1e") is None
    store.root.parent.mkdir(exist_ok=True)
    store.root.write_text("not a folder")
    with pytest.raises(OSError):
        store.panels()
    with pytest.raises(OSError):
        store.signature()


def test_new_ids_are_eight_hex_digits_and_unused(store: Store) -> None:
    ids = {store.new_id() for _ in range(200)}
    assert len(ids) == 200 and all(ID_RE.fullmatch(i) for i in ids)


def test_panels_are_ordered_by_board_then_added_at_then_id(store: Store) -> None:
    t = now()
    late = store.add(make(store, "late", added_at=t + timedelta(seconds=5)))
    early = store.add(make(store, "early", added_at=t))
    tie_a = store.add(make(store, "tie", id="00000001", added_at=t + timedelta(seconds=1)))
    tie_b = store.add(make(store, "tie", id="00000000", added_at=t + timedelta(seconds=1)))
    other = store.add(make(store, "other", board="incident-db", added_at=t + timedelta(hours=1)))
    assert store.panels() == [other, early, tie_b, tie_a, late]
    assert store.panels("main") == [early, tie_b, tie_a, late]
    assert store.boards() == [("incident-db", 1), ("main", 4)]


def test_bad_board_names_are_refused(store: Store) -> None:
    with pytest.raises(SpecError):
        store.panels("../etc")
    with pytest.raises(SpecError):
        store.remove_board("Main")


# ------------------------------------------------------------------------------------------ atomic writes


def test_writes_leave_no_temporary_files_and_readers_skip_them(store: Store) -> None:
    spec = store.add(make(store))
    (store.root / "main" / f".{store.new_id()}.json.tmp").write_text('{"half": ')
    (store.root / "main" / "notes.txt").write_text("not a panel")
    (store.root / ".lock").write_text("")
    (store.root / "Not-A-Board").mkdir()
    assert store.panels() == [spec] and store.problems == []
    assert [entry[0] for entry in store.signature()] == [f"main/{spec.id}.json"]


def test_a_write_that_fails_leaves_the_old_file_whole(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    old = store.add(make(store, key="k", title="old"))

    def broken(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        store.add(make(store, key="k", title="new"))
    monkeypatch.undo()
    assert store.panels() == [old]
    assert files(store.root) == [".lock", f"main/{old.id}.json"]


# ------------------------------------------------------------------------------------------ expiry


def test_expired_panels_are_deleted_by_whoever_lists_them(store: Store) -> None:
    t = now()
    keep = store.add(make(store, "keep", expires_at=t + timedelta(hours=1)))
    gone = store.add(make(store, "gone", expires_at=t + timedelta(minutes=1)))
    alone = store.add(make(store, "alone", board="incident-db", expires_at=t + timedelta(minutes=1)))
    assert store.panels(at=t) == [alone, keep, gone]
    assert store.get(gone.id) == gone
    later = t + timedelta(minutes=2)
    assert store.panels(at=later) == [keep]
    assert files(store.root) == [f"main/{keep.id}.json"]
    assert not (store.root / "incident-db").exists(), "the board's last panel expired, so the board went"
    assert store.boards(at=later) == [("main", 1)]


def test_get_hides_an_expired_panel(store: Store) -> None:
    spec = store.add(make(store, expires_at=now() - timedelta(seconds=1)))
    assert store.get(spec.id) is None


# ------------------------------------------------------------------------------------------ replace by key


def test_an_add_with_a_key_replaces_that_keys_panel_and_keeps_its_place(store: Store) -> None:
    first = store.add(make(store, "first", key="lat"))
    between = store.add(make(store, "between"))
    second = store.add(make(store, "second", key="lat", unit="ms", expires_at=now() + timedelta(hours=2)))
    assert second.id == first.id and second.added_at == first.added_at
    assert second.updated_at is not None and second.changed_at == second.updated_at
    assert second.title == "second" and second.unit == "ms"
    assert store.panels() == [second, between]
    assert len(files(store.root / "main")) == 2


def test_keys_are_per_board(store: Store) -> None:
    a = store.add(make(store, key="lat"))
    b = store.add(make(store, key="lat", board="incident-db"))
    assert a.id != b.id and b.updated_at is None
    assert len(store.panels()) == 2


def test_an_expired_panel_with_the_key_is_not_replaced(store: Store) -> None:
    old = store.add(make(store, key="lat", expires_at=now() - timedelta(seconds=1)))
    new = store.add(make(store, key="lat"))
    assert new.id != old.id and new.updated_at is None
    assert store.panels() == [new]


def test_duplicate_keys_left_by_anything_else_collapse_into_one(store: Store) -> None:
    t = now()
    a = store.add(make(store, "a", added_at=t))
    b = store.add(make(store, "b", added_at=t + timedelta(seconds=1)))
    for spec in (a, b):  # written behind the store's back, both with the key
        doc = spec.to_json() | {"key": "dup"}
        (store.root / "main" / f"{spec.id}.json").write_text(json.dumps(doc))
    replaced = store.add(make(store, "c", key="dup"))
    assert replaced.id == a.id
    assert [p.title for p in store.panels()] == ["c"]


# ------------------------------------------------------------------------------------------ arranging


def test_arrange_writes_only_the_size_and_position(store: Store) -> None:
    t = now()
    a = store.add(make(store, "a", added_at=t))
    b = store.add(make(store, "b", added_at=t + timedelta(seconds=1)))
    grown = store.arrange(a.id, size="large")
    assert grown == dataclasses.replace(a, size="large") and store.get(a.id) == grown
    assert grown is not None and grown.changed_at == a.changed_at, "arranging is no reason to run it again"
    moved = store.arrange(a.id, position=b.added_at.timestamp() + 1)
    assert moved is not None and (moved.size, moved.position) == ("large", b.added_at.timestamp() + 1)
    assert store.panels() == [b, moved], "panels sort by order_key: a moved after b"
    assert store.arrange(a.id) == moved, "nothing to change"
    assert store.arrange("0badc0de", size="small") is None, "a panel that's gone"
    with pytest.raises(SpecError):
        store.arrange(b.id, size="huge")  # type: ignore[arg-type]
    assert files(store.root) == sorted([".lock", f"main/{a.id}.json", f"main/{b.id}.json"])


def test_arrange_reads_the_file_again_so_a_replace_meanwhile_stays(store: Store) -> None:
    first = store.add(make(store, "first", key="lat"))
    second = store.add(make(store, "second", key="lat", unit="ms"))  # landed after the page last read
    arranged = store.arrange(first.id, size="small")
    assert arranged is not None and arranged.title == "second" and arranged.unit == "ms"
    assert arranged.updated_at == second.updated_at and arranged.size == "small"


def test_arrange_skips_an_expired_panel(store: Store) -> None:
    gone = store.add(make(store, expires_at=now() - timedelta(seconds=1)))
    assert store.arrange(gone.id, size="large") is None


def test_a_key_replace_keeps_the_users_arranging(store: Store) -> None:
    first = store.add(make(store, "first", key="lat"))
    store.arrange(first.id, size="large", position=12.5)
    again = store.add(make(store, "again", key="lat"))
    assert (again.id, again.size, again.position) == (first.id, "large", 12.5), "re-adding doesn't undo it"
    sized = store.add(make(store, "sized", key="lat", size="small"))
    assert (sized.size, sized.position) == ("small", 12.5), "an add that sets a size wins; the place stays"
    other = store.add(make(store, "other", size="medium"))
    assert other.size == "medium" and other.position is None


# ------------------------------------------------------------------------------------------ removing


def test_remove_returns_the_panel_and_drops_an_empty_board_folder(store: Store) -> None:
    one = store.add(make(store, board="incident-db"))
    two = store.add(make(store, board="incident-db"))
    assert store.remove(one.id) == one
    assert (store.root / "incident-db").is_dir()
    assert store.remove(two.id) == two
    assert not (store.root / "incident-db").exists()
    assert store.remove(two.id) is None, "removing a panel that's already gone is fine"
    assert store.remove("not-an-id") is None


def test_remove_board_removes_every_live_panel(store: Store) -> None:
    keep = store.add(make(store, board="main"))
    specs = [store.add(make(store, str(i), board="incident-db")) for i in range(3)]
    assert store.remove_board("incident-db") == specs
    assert store.remove_board("incident-db") == []
    assert store.panels() == [keep]
    assert not (store.root / "incident-db").exists()


# ------------------------------------------------------------------------------------------ bad files


def test_bad_files_are_reported_skipped_and_left_on_disk(store: Store) -> None:
    good = store.add(make(store))
    folder = store.root / "main"
    (folder / "aaaaaaaa.json").write_text("{not json")
    (folder / "bbbbbbbb.json").write_text(json.dumps({"format": 1, "id": "bbbbbbbb", "board": "main"}))
    (folder / "cccccccc.json").write_text(json.dumps(make(store, id="dddddddd").to_json()))
    (folder / "eeeeeeee.json").write_text(json.dumps(make(store, id="eeeeeeee", board="other").to_json()))
    (folder / "ffffffff.json").write_bytes(b"\xff\xfe")
    assert store.panels() == [good]
    problems = {path.name: why for path, why in store.problems}
    assert set(problems) == {"aaaaaaaa.json", "bbbbbbbb.json", "cccccccc.json", "eeeeeeee.json", "ffffffff.json"}
    assert problems["aaaaaaaa.json"].startswith("bad JSON")
    assert problems["bbbbbbbb.json"].startswith("missing title, kind, added_at")
    assert "isn't its file name" in problems["cccccccc.json"]
    assert "isn't its folder" in problems["eeeeeeee.json"]
    assert len(files(folder)) == 6
    assert store.get("aaaaaaaa") is None
    # The next clean listing forgets them.
    for name in problems:
        (folder / name).unlink()
    store.panels()
    assert store.problems == []


# ------------------------------------------------------------------------------------------ signature


def test_the_signature_changes_with_every_add_replace_and_remove(store: Store) -> None:
    sigs = [store.signature()]
    spec = store.add(make(store, key="k"))
    sigs.append(store.signature())
    assert sigs[-1] == store.signature(), "nothing changed, nothing moves"
    time.sleep(0.002)
    store.add(make(store, key="k", title="Renamed ping"))
    sigs.append(store.signature())
    store.remove(spec.id)
    sigs.append(store.signature())
    assert sigs[0] == sigs[-1] == ()
    assert len(set(sigs[:-1])) == 3


# ------------------------------------------------------------------------------------------ many writers


WRITER = textwrap.dedent("""
    import sys
    from datetime import datetime, timezone
    from textual_orbital.boards.model import PanelSpec
    from textual_orbital.boards.store import Store

    root, who, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    store = Store(root)
    for i in range(count):
        for key, board in ((None, "main"), ("shared", "main"), (f"own-{who}", "incident-db")):
            store.add(PanelSpec(id=store.new_id(), board=board, title=f"{who} {i}", kind="stat",
                                added_at=datetime.now(timezone.utc), cmd="echo 1", key=key))
""")


def test_several_processes_adding_at_once_never_lose_or_duplicate_a_panel(store: Store) -> None:
    writers, count = 6, 15
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(store.root), f"w{n}", str(count)], cwd=HERE,
                              stderr=subprocess.PIPE, text=True) for n in range(writers)]
    for proc in procs:
        _, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
    panels = store.panels()
    assert store.problems == []
    plain = [p for p in panels if p.key is None]
    shared = [p for p in panels if p.key == "shared"]
    own = [p for p in panels if p.key and p.key.startswith("own-")]
    assert len(plain) == writers * count
    assert len(shared) == 1, "every writer replaced the one shared panel"
    assert sorted(p.key or "" for p in own) == sorted(f"own-w{n}" for n in range(writers))
    assert not [name for name in files(store.root) if name.endswith(".tmp")]


def test_threads_adding_and_removing_on_one_board_never_trip_over_its_folder(store: Store) -> None:
    errors: list[BaseException] = []
    kept: list[PanelSpec] = []

    def churn(n: int) -> None:
        try:
            for i in range(40):
                # Add and remove the board's only panel, while the others do the same: the board folder
                # comes and goes under everyone's writes.
                spec = store.add(make(store, f"t{n}-{i}", board="busy"))
                store.remove(spec.id)
            kept.append(store.add(make(store, f"t{n}-last", board="busy", key=f"t{n}")))
        except BaseException as error:  # handed to the test thread
            errors.append(error)

    threads = [threading.Thread(target=churn, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert sorted(p.id for p in store.panels("busy")) == sorted(p.id for p in kept)
    assert store.problems == []


# ------------------------------------------------------------------------------------------ the source


def test_the_source_hands_back_the_same_tuple_while_nothing_changes(store: Store) -> None:
    source = BoardsSource(store)
    assert (source.key, source.name, source.interval) == ("BD", "Boards", 2.0)
    empty = source.fetch().payload
    assert empty == () and source.fetch().payload is empty
    spec = store.add(make(store))
    first = source.fetch().payload
    assert first == (spec,) and isinstance(first, tuple)
    assert source.fetch().payload is first
    # Rewriting a file with the same content changes the signature, but not the panels: same object.
    path = store.root / "main" / f"{spec.id}.json"
    time.sleep(0.002)
    path.write_text(path.read_text())
    assert source.fetch().payload is first
    store.remove(spec.id)
    assert source.fetch().payload == ()


def test_the_source_notices_a_panel_expiring_with_no_file_change(store: Store) -> None:
    source = BoardsSource(store)
    soon = store.add(make(store, "soon", expires_at=now() + timedelta(seconds=0.3)))
    keep = store.add(make(store, "keep"))
    assert source.fetch().payload == (soon, keep)
    time.sleep(0.4)
    assert source.fetch().payload == (keep,)


def test_the_source_skips_bad_files_and_raises_only_for_an_unreadable_root(store: Store) -> None:
    source = BoardsSource(store)
    good = store.add(make(store))
    (store.root / "main" / "aaaaaaaa.json").write_text("{")
    assert source.fetch().payload == (good,)
    assert [path.name for path, _ in store.problems] == ["aaaaaaaa.json"]
    broken = BoardsSource(Store(store.root / "main" / f"{good.id}.json"))
    with pytest.raises(OSError):
        broken.fetch()
