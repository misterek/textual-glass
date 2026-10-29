"""Run a Textual app in a pty the way Ghostty would host it, then replay its output.

``run_app`` starts the app with ``TERM=xterm-ghostty`` (by default) on a pty
whose size includes pixel dimensions (TIOCSWINSZ), sends a script of keys and
resizes, and records every byte the app writes, with timestamps.

``Replay`` feeds the bytes to pyte (text and cell colours) and to
``KittyModel`` (images), in order, so a snapshot at any point in the script
shows what the terminal would hold. ``Replay.composite`` draws an approximate
screenshot of it, glyphs included.

By default nothing answers the app's terminal queries, as in any plain pty.
With ``answer=True`` the harness replies to the kitty graphics query, DA1 and
the synchronized-output DECRQM like a kitty-graphics terminal would.
"""

from __future__ import annotations

import fcntl
import os
import re
import select
import struct
import subprocess
import termios
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pyte

from textual_glass.kitty import DIACRITICS, PLACEHOLDER
from textual_glass.testing.kitty_emu import KittyModel, composite

CW, CH = 9, 19  # pixels per cell reported via TIOCSWINSZ
KEYS = {
    "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
    "enter": b"\r", "escape": b"\x1b", "tab": b"\t", "f9": b"\x1b[20~", "shift+f9": b"\x1b[20;2~",
    "ctrl+p": b"\x10", "pagedown": b"\x1b[6~", "end": b"\x1b[F",
}
BOX_GLYPHS = set("╭╮╰╯─│┌┐└┘━┃┏┓┗┛═║╔╗╚╝")
UNFINISHED = re.compile(rb"\x1b(?:\[[0-9;?$]*|\][^\x07\x1b]*|_)?\Z")  # an escape sequence cut off at the end


@dataclass
class Step:
    delay: float
    label: str
    keys: bytes | str | tuple[int, int] | None = None  # bytes, a KEYS name, text, or (cols, rows) to resize


@dataclass
class Capture:
    code: object
    chunks: list[tuple[float, bytes]]
    marks: list[tuple[float, str, object]]
    wall: float
    sizes: list[tuple[float, tuple[int, int]]] = field(default_factory=list)
    started: float = 0.0  # time.time() when the app was started (for wall-clock checks)

    @property
    def raw(self) -> bytes:
        return b"".join(c for _, c in self.chunks)

    def between(self, t0: float, t1: float) -> bytes:
        return b"".join(c for t, c in self.chunks if t0 <= t < t1)

    def mark_time(self, label: str) -> float:
        return next(t for t, name, _ in self.marks if name == label)


def winsize(cols: int, rows: int) -> bytes:
    return struct.pack("4H", rows, cols, cols * CW, rows * CH)


def run_app(argv: Sequence[str], script: Sequence[Step], root: str, size: tuple[int, int] = (160, 45),
            env: dict[str, str] | None = None, answer: bool = False, timeout: float = 30.0,
            term: str = "xterm-ghostty", probe: Callable[[str, int], None] | None = None) -> Capture:
    """Run ``argv`` in a pty (working directory ``root``) and play ``script``. ``probe(label, pid)`` is called
    at every step, before its keys are sent (to sample CPU time, for example)."""
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize(*size))
    environment = {k: v for k, v in os.environ.items()
                   if k not in ("KITTY_WINDOW_ID", "TMUX", "STY", "TEXTUAL_GLASS", "TEXTUAL_GLASS_MOTION",
                                "TERM_PROGRAM", "GHOSTTY_RESOURCES_DIR", "NO_COLOR")}
    # BROWSER=true makes Python's webbrowser run the no-op `true` command: an app under test
    # must never open the user's real browser.
    environment.update(TERM=term, COLORTERM="truecolor", BROWSER="true")
    if term == "xterm-ghostty":
        environment["TERM_PROGRAM"] = "ghostty"
    environment.update(env or {})
    proc = subprocess.Popen(list(argv), stdin=slave, stdout=slave, stderr=slave, env=environment,
                            start_new_session=True, cwd=root,
                            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    os.close(slave)
    chunks: list[tuple[float, bytes]] = []
    marks: list[tuple[float, str, object]] = []
    sizes = [(0.0, size)]
    started = time.time()
    t0 = time.monotonic()
    done = threading.Event()

    def reply(data: bytes) -> None:
        if not answer:
            return
        out = b""
        for match in re.finditer(rb"\x1b_G([^\x1b]*?a=q[^\x1b]*)\x1b\\", data):
            image_id = re.search(rb"i=(\d+)", match.group(1))
            out += b"\x1b_Gi=" + (image_id.group(1) if image_id else b"0") + b";OK\x1b\\"
        if b"\x1b[c" in data:
            out += b"\x1b[?62;22;52c"
        if b"\x1b[?2026$p" in data:
            out += b"\x1b[?2026;2$y"
        if out:
            os.write(master, out)

    def reader() -> None:
        while not done.is_set():
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    data = os.read(master, 1 << 16)
                except OSError:
                    break
                if not data:
                    break
                chunks.append((time.monotonic() - t0, data))
                reply(data)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    for step in script:
        time.sleep(step.delay)
        if probe is not None:
            probe(step.label, proc.pid)
        marks.append((time.monotonic() - t0, step.label, step.keys))
        keys = step.keys
        if isinstance(keys, tuple):
            fcntl.ioctl(master, termios.TIOCSWINSZ, winsize(*keys))  # the kernel sends SIGWINCH
            sizes.append((time.monotonic() - t0, keys))
        elif isinstance(keys, str):
            os.write(master, KEYS.get(keys, keys.encode()))
        elif keys:
            os.write(master, keys)
    try:
        code: object = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = "TIMEOUT"
    time.sleep(0.3)
    done.set()
    thread.join(1)
    os.close(master)
    return Capture(code, chunks, marks, time.monotonic() - t0, sizes, started)


class Replay:
    """Feeds output to pyte and the kitty model, in order."""

    def __init__(self, cols: int, rows: int) -> None:
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.model = KittyModel(cell=(CW, CH))
        self.buf = b""
        self.default_bg = "1d1f21"

    @property
    def size(self) -> tuple[int, int]:
        return self.screen.columns, self.screen.lines

    def snapshot(self) -> Replay:
        """A frozen copy of the screen and the image store (it can't be fed)."""
        import copy

        snap = Replay.__new__(Replay)
        snap.screen = copy.deepcopy(self.screen)
        snap.model = copy.deepcopy(self.model)
        snap.stream = None  # type: ignore[assignment]
        snap.buf = b""
        snap.default_bg = self.default_bg
        return snap

    def resize(self, cols: int, rows: int) -> None:
        self.screen.resize(rows, cols)

    def _feed_text(self, data: bytes) -> None:
        for match in re.finditer(rb"\x1b\]11;#([0-9a-fA-F]{6})\x07|\x1b\]111\x07|\x1b\[\?1049([hl])", data):
            if match.group(1):
                self.default_bg = match.group(1).decode()
            elif match.group(2):
                self.model.alt_screen(match.group(2) == b"h")  # Ghostty: entering the alternate screen clears it
            else:
                self.default_bg = "1d1f21"
        self.stream.feed(data)

    def feed(self, data: bytes) -> None:
        self.buf += data
        while True:
            i = self.buf.find(b"\x1b_G")
            if i < 0:
                unfinished = UNFINISHED.search(self.buf)  # a sequence split across reads waits for the rest
                cut = unfinished.start() if unfinished else len(self.buf)
                self._feed_text(self.buf[:cut])
                self.buf = self.buf[cut:]
                return
            self._feed_text(self.buf[:i])
            j = self.buf.find(b"\x1b\\", i)
            if j < 0:
                self.buf = self.buf[i:]
                return
            cursor = (self.screen.cursor.x, self.screen.cursor.y)
            self.model.apc(self.buf[i + 3:j], cursor)
            self.buf = self.buf[j + 2:]

    def text(self, y: int) -> str:
        cols, _ = self.size
        line = self.screen.buffer[y]
        return "".join(line[x].data[:1] or " " for x in range(cols))

    def all_text(self) -> str:
        return "\n".join(self.text(y) for y in range(self.size[1]))

    def placeholder_ids(self) -> set[int]:
        out = set()
        cols, rows = self.size
        for y in range(rows):
            line = self.screen.buffer[y]
            for x in range(cols):
                c = line[x]
                if c.data and c.data[0] == PLACEHOLDER and re.fullmatch("[0-9a-fA-F]{6}", c.fg):
                    out.add(int(c.fg, 16))
        return out

    def cell_bgs(self) -> list[list[str]]:
        """Every cell's background, one list per row (``"default"`` where none is set)."""
        cols, rows = self.size
        return [[self.screen.buffer[y][x].bg for x in range(cols)] for y in range(rows)]

    def composite(self, path: str) -> None:
        composite(self.screen, self.model, CW, CH, self.default_bg, diacritics=DIACRITICS).save(path)


def replay_until(capture: Capture, t_end: float, initial: tuple[int, int]) -> Replay:
    """Replay everything written before ``t_end`` (applying resizes on the way)."""
    rp = Replay(*initial)
    sizes = list(capture.sizes[1:])
    for t, data in capture.chunks:
        if t >= t_end:
            break
        while sizes and sizes[0][0] <= t:
            rp.resize(*sizes.pop(0)[1])
        rp.feed(data)
    return rp


def replay_steps(capture: Capture, initial: tuple[int, int], png_dir: str | None = None):  # type: ignore[no-untyped-def]
    """Yield (label, Replay) just before each step's keys are sent, then ("end", Replay).

    It is one live Replay, fed on between steps: keep a step's screen with
    ``rp.snapshot()`` (``{label: rp.snapshot() for label, rp in replay_steps(...)}``)."""
    rp = Replay(*initial)
    chunks = list(capture.chunks)
    sizes = list(capture.sizes[1:])
    i = 0
    for n, (t_mark, label, _) in enumerate(capture.marks):
        while i < len(chunks) and chunks[i][0] < t_mark:
            while sizes and sizes[0][0] <= chunks[i][0]:
                rp.resize(*sizes.pop(0)[1])
            rp.feed(chunks[i][1])
            i += 1
        if png_dir:
            os.makedirs(png_dir, exist_ok=True)
            rp.composite(os.path.join(png_dir, f"{n:02d}_{re.sub('[^a-zA-Z0-9]+', '_', label)}.png"))
        yield label, rp
    while i < len(chunks):
        rp.feed(chunks[i][1])
        i += 1
    yield "end", rp


def graphics_in(data: bytes) -> int:
    return data.count(b"\x1b_G")
