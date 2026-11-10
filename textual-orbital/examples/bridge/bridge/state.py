"""The session model the bridge reads: one :class:`Session` per state file the hook writes.

Also the pure rules on top of it: a session's pose (:func:`pose_for`), red alert (:func:`red_alert`), and
the seating chart (:class:`SeatingChart`), which gives each session a station and a crew member for life.
No Textual imports.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ENDED_LINGER = timedelta(minutes=1)    # an ended session stays on screen this long, then it is forgotten
STALE_AFTER = timedelta(hours=12)      # a session nobody heard from for this long is forgotten
DOZE_AFTER = timedelta(minutes=30)     # done -> dozing
WAVE_AFTER = timedelta(minutes=2)      # hand_up -> waving
STAND_AFTER = timedelta(minutes=10)    # waving -> standing
SPARK_SHOWN = timedelta(minutes=1)     # a tool failure's spark stays this long

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_time(value: Any) -> datetime | None:
    """An aware datetime from an ISO string (``Z`` and naive values are taken as UTC), else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Session:
    """One Claude Code or Codex session, as its state file says."""

    id: str
    state: str
    since: datetime
    updated: datetime
    started: datetime
    cwd: str = ""
    project: str = ""
    waiting_kind: str | None = None
    model: str = ""
    subagents: tuple[str, ...] = ()
    spark_at: datetime | None = None
    error: str = ""
    last_event: str = ""
    agent: str = "claude"   # who sends its events: "claude" (Claude Code) or "codex"

    @property
    def present(self) -> bool:
        """Still at its station (an ended one is only shown leaving)."""
        return self.state != "ended"

    @classmethod
    def from_record(cls, record: Any, sid: str, mtime: datetime) -> Session | None:
        """A session from a state file's JSON, or None if it isn't one. Odd fields get defaults. ``sid`` is
        the file's name: unique in the folder, so it is the id (the hook names files by session id)."""
        if not isinstance(record, dict):
            return None
        state = record.get("state")
        if not isinstance(state, str) or not state:
            return None
        updated = parse_time(record.get("updated")) or mtime
        since = parse_time(record.get("since")) or updated
        started = parse_time(record.get("started")) or since
        agents = record.get("subagents")
        kind = record.get("waiting_kind")

        def text(key: str) -> str:
            value = record.get(key)
            return value if isinstance(value, str) else ""

        cwd = text("cwd")
        return cls(
            id=sid, state=state, since=since, updated=updated, started=started, cwd=cwd,
            project=text("project") or os.path.basename(cwd.rstrip("/")) or sid[:8],
            waiting_kind=kind if isinstance(kind, str) else None, model=text("model"),
            subagents=tuple(a for a in agents if isinstance(a, str)) if isinstance(agents, list) else (),
            spark_at=parse_time(record.get("spark_at")), error=text("error"), last_event=text("last_event"),
            agent=text("agent") or "claude",
        )


def forgotten(session: Session, now: datetime) -> bool:
    """Gone from the bridge: ended a while ago, or not heard from in 12 hours."""
    if session.state == "ended" and now - session.since > ENDED_LINGER:
        return True
    return now - session.updated > STALE_AFTER


class SessionReader:
    """Reads a folder of state files, re-reading only the files that changed since the last call."""

    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir)
        self._cache: dict[str, tuple[tuple[int, int, int], Session | None]] = {}
        self.loads = 0  # files parsed so far (the tests check the cache with it)

    def read(self, now: datetime) -> list[Session]:
        """Every session on the bridge at ``now``, oldest first. Garbage files are skipped."""
        sessions: list[Session] = []
        seen: set[str] = set()
        try:
            entries = list(os.scandir(self.state_dir))
        except OSError:
            entries = []
        for entry in entries:
            name = entry.name
            if name.startswith(".") or not name.endswith(".json"):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            seen.add(name)
            sig = (st.st_mtime_ns, st.st_ino, st.st_size)
            cached = self._cache.get(name)
            if cached is not None and cached[0] == sig:
                session = cached[1]
            else:
                session = self._load(Path(entry.path), datetime.fromtimestamp(st.st_mtime, timezone.utc))
                self._cache[name] = (sig, session)
            if session is not None and not forgotten(session, now):
                sessions.append(session)
        for name in set(self._cache) - seen:
            del self._cache[name]
        sessions.sort(key=lambda s: (s.started, s.id))
        return sessions

    def _load(self, path: Path, mtime: datetime) -> Session | None:
        self.loads += 1
        try:
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError):
            return None
        return Session.from_record(record, path.stem, mtime)


_READERS: dict[Path, SessionReader] = {}


def read_sessions(state_dir: Path | str, now: datetime) -> list[Session]:
    """Every session on the bridge at ``now`` (a cached :class:`SessionReader` per folder)."""
    key = Path(state_dir).expanduser().resolve()
    reader = _READERS.get(key)
    if reader is None:
        reader = _READERS[key] = SessionReader(key)
    return reader.read(now)


def pose_for(session: Session | None, now: datetime) -> str:
    """The pose a station shows for ``session`` at ``now`` (README.md's table)."""
    if session is None:
        return "empty"
    age = now - session.since
    state = session.state
    if state == "seated":
        return "seated"
    if state == "working":
        return "typing"
    if state == "done":
        return "dozing" if age >= DOZE_AFTER else "done"
    if state == "waiting":
        return "standing" if age >= STAND_AFTER else "waving" if age >= WAVE_AFTER else "hand_up"
    if state == "error":
        return "jolt"
    if state == "compacting":
        return "tea"
    if state == "ended":
        return "empty"
    return "seated"


def sparking(session: Session | None, now: datetime) -> bool:
    """A tool failed a moment ago (not the whole turn: that is ``error``)."""
    return session is not None and session.spark_at is not None and \
        timedelta(0) <= now - session.spark_at < SPARK_SHOWN


def waiting_count(sessions: Sequence[Session]) -> int:
    return sum(1 for s in sessions if s.state == "waiting")


def red_alert(sessions: Sequence[Session]) -> bool:
    """Two or more crew waiting on you, or any error."""
    return waiting_count(sessions) >= 2 or any(s.state == "error" for s in sessions)


#: The order stations fill in: the two in the middle of the arc first, the front corners last.
#: Station numbers run left to right along the arc.
FILL_ORDER = (2, 3, 1, 4, 0, 5)


class SeatingChart:
    """Who sits where. A session keeps its station and its crew member for life; a new session takes the
    first free station (in ``fill_order``) and the first crew member, in roster order, nobody present has.
    Sessions beyond the stations wait, unseated, for a free one."""

    def __init__(self, roster: Sequence[str], stations: int = 6, fill_order: Sequence[int] = FILL_ORDER) -> None:
        self.roster = list(roster)
        self.stations = stations
        self.fill_order = [i for i in fill_order if i < stations] + \
            [i for i in range(stations) if i not in fill_order]
        self.station_of: dict[str, int] = {}
        self.member_of: dict[str, str] = {}

    def seat(self, sessions: Sequence[Session]) -> list[Session | None]:
        """Each station's session (None: empty), left to right; the chart is updated in place."""
        ids = {s.id for s in sessions}
        for sid in [sid for sid in self.station_of if sid not in ids]:
            del self.station_of[sid]
            self.member_of.pop(sid, None)
        seats: list[Session | None] = [None] * self.stations
        newcomers = []
        for s in sessions:
            if s.id in self.station_of:
                seats[self.station_of[s.id]] = s
            else:
                newcomers.append(s)
        for s in sorted(newcomers, key=lambda s: (s.started, s.id)):
            free = next((i for i in self.fill_order if seats[i] is None), None)
            if free is None:
                break
            seats[free] = s
            self.station_of[s.id] = free
            self.member_of[s.id] = self._free_member()
        return seats

    def unseated(self, sessions: Sequence[Session]) -> int:
        """How many sessions have no station (more sessions than stations)."""
        return sum(1 for s in sessions if s.id not in self.station_of)

    def _free_member(self) -> str:
        used = list(self.member_of.values())
        if not self.roster:
            return ""
        for member in self.roster:
            if member not in used:
                return member
        # more sessions than crew: the least used member, earliest in the roster
        return min(self.roster, key=lambda m: (used.count(m), self.roster.index(m)))
