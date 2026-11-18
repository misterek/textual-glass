"""The folder of boards: one folder per board, one JSON file per panel.

Many processes may use one folder at once (several agents adding panels, a dashboard reading and removing
them). Adding a new panel only creates a file and removing one only deletes a file, so they never
conflict; writes go to a hidden temporary file that is then renamed, so a reader never sees half a panel.
Only an add with a ``key`` (which replaces that key's panel) and ``arrange`` (a panel's size or place, set on
the page) take a lock, around their find-and-write.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import secrets
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from .model import BOARD_RE, ID_RE, PanelSpec, Size, SpecError

__all__ = ["Store", "default_root"]

#: Times a write retries when another process removed the (empty) board folder under it.
_RETRIES = 5


def default_root() -> Path:
    """``$BOARDS_DIR``, or ``~/.cache/boards``."""
    return Path(os.environ.get("BOARDS_DIR") or "~/.cache/boards").expanduser()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _check_board(board: str) -> None:
    if not BOARD_RE.fullmatch(board):
        raise SpecError(f"bad board name {board!r}: lowercase letters, digits and dashes, up to 32")


class Store:
    """The boards under ``root`` (None: ``$BOARDS_DIR`` or ``~/.cache/boards``).

    ``problems`` lists the panel files the last ``panels()`` call couldn't read, with why. They are left on
    disk and never stop the others from loading.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else default_root()
        self.problems: list[tuple[Path, str]] = []

    # ------------------------------------------------------------------------------------------- reading

    def _board_dirs(self, board: str | None = None) -> list[Path]:
        if board is not None:
            _check_board(board)
            folder = self.root / board
            return [folder] if folder.is_dir() else []
        try:
            entries = sorted(os.scandir(self.root), key=lambda e: e.name)
        except FileNotFoundError:
            return []
        return [Path(e.path) for e in entries
                if BOARD_RE.fullmatch(e.name) and e.is_dir(follow_symlinks=False)]

    @staticmethod
    def _panel_files(folder: Path) -> list[Path]:
        try:
            names = sorted(os.listdir(folder))
        except (FileNotFoundError, NotADirectoryError):
            return []
        return [folder / name for name in names if not name.startswith(".") and name.endswith(".json")]

    @staticmethod
    def _read(path: Path) -> PanelSpec:
        """One panel file. Raises ``FileNotFoundError`` if it's gone, ``SpecError`` if it's no good."""
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except FileNotFoundError:
            raise
        except (ValueError, UnicodeDecodeError) as error:
            raise SpecError(f"bad JSON: {error}") from None
        except OSError as error:
            raise SpecError(f"can't read it: {error.strerror or error}") from None
        spec = PanelSpec.from_json(doc)
        if spec.id != path.stem:
            raise SpecError(f"its id {spec.id!r} isn't its file name")
        if spec.board != path.parent.name:
            raise SpecError(f"its board {spec.board!r} isn't its folder")
        return spec

    def _scan(self, board: str | None, problems: list[tuple[Path, str]]) -> Iterator[tuple[Path, PanelSpec]]:
        for folder in self._board_dirs(board):
            files = self._panel_files(folder)
            if not files:
                self._prune(folder)
            for path in files:
                try:
                    yield path, self._read(path)
                except FileNotFoundError:
                    continue  # removed while we looked
                except SpecError as error:
                    problems.append((path, str(error)))

    def panels(self, board: str | None = None, at: datetime | None = None) -> list[PanelSpec]:
        """The live panels (of one board, or all), sorted by board, then ``order_key`` (``position`` if the
        user moved the panel, else ``added_at``; then the id). Expired panels found on the way are deleted."""
        when = at or _utcnow()
        problems: list[tuple[Path, str]] = []
        live = []
        for path, spec in self._scan(board, problems):
            if spec.expired(when):
                self._delete(path)
            else:
                live.append(spec)
        self.problems = problems
        return sorted(live, key=lambda s: (s.board, s.order_key))

    def boards(self, at: datetime | None = None) -> list[tuple[str, int]]:
        """``(board, live panel count)`` for every board that has panels, by name."""
        counts: dict[str, int] = {}
        for spec in self.panels(at=at):
            counts[spec.board] = counts.get(spec.board, 0) + 1
        return sorted(counts.items())

    def _find(self, panel_id: str) -> tuple[Path, PanelSpec] | None:
        if not ID_RE.fullmatch(panel_id):
            return None
        for folder in self._board_dirs():
            path = folder / f"{panel_id}.json"
            try:
                return path, self._read(path)
            except (FileNotFoundError, SpecError):
                continue
        return None

    def get(self, panel_id: str) -> PanelSpec | None:
        """The live panel with this id, or None."""
        found = self._find(panel_id)
        if found is None or found[1].expired(_utcnow()):
            return None
        return found[1]

    def signature(self) -> tuple[tuple[str, int, int], ...]:
        """``(board/file, mtime_ns, size)`` of every panel file, sorted: it changes when any panel is
        added, replaced or removed. Cheap: no file is opened. Raises ``OSError`` only if the root can't be
        read (a missing root is an empty signature)."""
        out = []
        for folder in self._board_dirs():
            for path in self._panel_files(folder):
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                out.append((f"{folder.name}/{path.name}", st.st_mtime_ns, st.st_size))
        return tuple(sorted(out))

    # ------------------------------------------------------------------------------------------- writing

    def new_id(self) -> str:
        """A fresh panel id (8 random hex digits) that no board has."""
        while True:
            panel_id = secrets.token_hex(4)
            if not any((folder / f"{panel_id}.json").exists() for folder in self._board_dirs()):
                return panel_id

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing it lets the lock go

    def _write(self, spec: PanelSpec) -> None:
        """Write one panel file atomically: a hidden temporary file in the same folder, then a rename."""
        body = json.dumps(spec.to_json(), indent=2, ensure_ascii=False) + "\n"
        for attempt in range(_RETRIES):
            folder = self.root / spec.board
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = folder / f".{spec.id}.json.tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            except FileNotFoundError:
                if attempt == _RETRIES - 1:
                    raise
                continue  # the board folder went away (its last panel was removed): make it again
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(body)
                os.replace(tmp, folder / f"{spec.id}.json")
                return
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise

    def add(self, spec: PanelSpec) -> PanelSpec:
        """Write a panel and return what was written. With ``spec.key`` set, the panel with that key on that
        board is replaced: it keeps its id, ``added_at`` and ``position`` (its place on the board), and its
        ``size`` unless ``spec`` sets one (not ``auto``), so the user's arranging stays; ``updated_at`` is now."""
        if spec.key is None:
            self._write(spec)
            return spec
        with self._locked():
            now = _utcnow()
            same = [(path, old) for path, old in self._scan(spec.board, [])
                    if old.key == spec.key and not old.expired(now)]
            same.sort(key=lambda found: (found[1].added_at, found[1].id))
            if same:
                old = same[0][1]
                spec = dataclasses.replace(spec, id=old.id, added_at=old.added_at, updated_at=now,
                                           position=old.position, size=old.size if spec.size == "auto" else spec.size)
            self._write(spec)
            for path, _ in same[1:]:
                self._delete(path)  # other panels with the key, if anything ever made them
        return spec

    def arrange(self, panel_id: str, *, size: Size | None = None, position: float | None = None) -> PanelSpec | None:
        """Set a panel's ``size`` and/or ``position`` (None leaves that field as it is) and return it, or None if
        the panel is gone. It reads the file again under the same lock as a key replace and writes only these
        fields, so it never undoes a replace that landed meanwhile; ``changed_at`` stays (arranging is no reason
        to run the panel again). Raises ``SpecError`` for a bad size or position."""
        with self._locked():
            found = self._find(panel_id)
            if found is None or found[1].expired(_utcnow()):
                return None
            old = found[1]
            spec = dataclasses.replace(old, size=old.size if size is None else size,
                                       position=old.position if position is None else float(position))
            if spec != old:
                self._write(spec)
            return spec

    def _delete(self, path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self._prune(path.parent)

    def _prune(self, folder: Path) -> None:
        """Remove a board folder that has nothing left in it. The OS checks that it's empty, so this fails
        harmlessly while a write is under way; a write that finds its folder gone makes it again."""
        with contextlib.suppress(OSError):
            folder.rmdir()

    def remove(self, panel_id: str) -> PanelSpec | None:
        """Delete a panel. Returns it, or None if it wasn't there."""
        found = self._find(panel_id)
        if found is None:
            return None
        self._delete(found[0])
        return found[1]

    def remove_board(self, board: str) -> list[PanelSpec]:
        """Delete every panel on a board (and so the board). Returns the live ones it deleted."""
        removed = []
        for spec in self.panels(board):
            if self.remove(spec.id) is not None:
                removed.append(spec)
        return removed
