"""The glass engine: owns the terminal images, compares scenes, sends pixels.

Layers, bottom to top. Everything but the placeholder images sits below
z = -1073741824, so it is drawn *under* cells that have an explicit background
colour, and only cells with the default background (see ``filter.py``) reveal it:

=================  ====================  ====================================
layer              z                      contents
=================  ====================  ====================================
backdrop           Z0                     the bottom screen: the backdrop
                                          painter, panels, pills, zebra rows,
                                          badges, bars, ``pixel_items``
ambient            Z0 + 10M               full motion only: shimmer, glints,
                                          the pulsing focus halo
float              Z0 + 30M               toasts and other overlay widgets
scrim (level k)    Z0 + k*100M - 50M      dims everything below a modal screen
dialog (level k)   Z0 + k*100M            the modal screen's own glass
placeholders       -1 (the terminal's)    ``GlassImage`` and sprites, above the
                                          scrims: dimmed with a gapless frame
=================  ====================  ====================================

Each frame the engine reads the scene (``scene.build``: CSS chrome plus every
``PixelMixin`` widget's items), compares it item by item with what the terminal
holds and sends only what changed: small changes as in-place edits inside the
same synchronized update as Textual's text, big ones rendered in a worker
thread. The bottom screen's backdrop is kept per page (its set of panels):
a page seen before is placed again at once, a new one is rendered at half
resolution inside the frame and replaced by the full one from the worker.
Images that Python steps through (scrim fades) are uploaded once and moved with
``a=a,c=N``; ambient effects and sprites are animations the terminal plays by
itself, so an idle app sends nothing at all.
"""

from __future__ import annotations

import asyncio
import contextvars
import fcntl
import math
import random
import struct
import sys
import termios
import time
import weakref
from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from . import _textual_hooks as hooks
from . import backdrop, kitty
from .detection import Support, detect
from .effects import Effects
from .filter import NO_RULES, GlassFilter, Rules
from .hard_exit import on_hard_exit
from .palette import Palette, hex_of
from .render import Box, Canvas, Grid, Item, paint, to_u8
from .scene import GLASS_EDGES, PixelContext, Scene, Tweens, build, rules_for, visible_screens
from .sprites import dim_frame

if TYPE_CHECKING:
    from textual.app import App
    from textual.geometry import Region
    from textual.screen import Screen
    from textual.strip import Strip
    from textual.widget import Widget

MOTIONS = ("minimal", "subtle", "full")

Z0 = -2_000_000_000
LEVEL_STEP = 100_000_000
Z_AMBIENT = 10_000_000
Z_FLOAT = 30_000_000
Z_SCRIM = -50_000_000
MAX_LEVELS = 9

MAIN = (0, "main")      # the bottom screen's backdrop: the only layer with a page cache
MAX_PIXELS = 2_400_000  # the backdrop is rendered at most this big; the terminal scales it
SYNC_FRACTION = 0.16    # static changes smaller than this are redrawn inside the frame
FULL_FRACTION = 0.5     # beyond this, re-render the whole layer
SPRITE_MAX_PX = 192     # sprite frames are rendered at most this big per side
SPRITE_HOLD = 2         # Textual frames a new sprite is kept unshown, while its widgets redraw to show it
SPRITE_RETRY = (5.0, 30.0, 300.0)  # a sprite whose maker raised is tried again after these delays (then every 300 s)
PAGE_CACHE = 4          # pages whose backdrops stay in the terminal for instant switches

ERROR_LIMIT = 5         # more errors than this within ERROR_WINDOW seconds turn glass off...
ERROR_WINDOW = 60.0
RETRY_DELAYS = (60.0, 300.0, 1800.0)  # ...then it tries again after each delay in turn, and stays off after the last
HEALTHY = 600.0         # a retry that stays on this long starts the count of retries again

Rect = tuple[int, int, int, int]


# ============================================================== plumbing ==


class Ids:
    """Image ids below 2**24, so a placeholder's 24-bit colour holds a whole id."""

    FIRST, LAST = 1 << 20, (1 << 24) - 1  # every id glass hands out is in this range

    def __init__(self) -> None:
        self._next = random.randrange(self.FIRST, self.LAST + 1 - self.FIRST)
        self.live: set[int] = set()

    def new(self) -> int:
        self._next += 1
        if self._next >= self.LAST:
            self._next = self.FIRST
        self.live.add(self._next)
        return self._next

    def free(self, image_id: int) -> None:
        self.live.discard(image_id)


class Writer:
    """Queues graphics commands and writes them through Textual's driver."""

    def __init__(self, app: App) -> None:
        self.app = app
        self.pending: list[str] = []
        self.bytes_sent = 0
        self.commands = 0
        #: Set while glass runs inside Textual's synchronized frame (``on_frame``):
        #: a flush then writes without markers of its own, which would end Textual's update early.
        self.in_frame = False

    def queue(self, data: str) -> None:
        if data:
            self.pending.append(data)

    def _take(self) -> str:
        data = "".join(self.pending)
        self.pending.clear()
        return data

    def drain_into_frame(self) -> None:
        """Called inside Textual's synchronized frame (before SYNC_END)."""
        self._write(self._take())

    def flush(self) -> None:
        """Write queued commands as their own synchronized update (or straight into Textual's, inside one)."""
        data = self._take()
        if data:
            self._write(data if self.in_frame else kitty.SYNC_START + data + kitty.SYNC_END)

    def _write(self, data: str) -> None:
        if data and hooks.driver_write(self.app, data):
            self.bytes_sent += len(data)
            self.commands += data.count("\x1b_G")


EXIT_KEY = "textual-glass"  # glass's entry in on_hard_exit


def hard_exit_sequence(terminal_background: bool) -> str:
    """What glass leaves for a hard exit (``on_hard_exit``): every image it may have
    made deleted, and the default background reset (OSC 111).

    It starts with a string terminator, which ends an escape sequence the writer
    thread may have left half-written, and sends delete-all twice: the first
    graphics command after an unfinished chunked transfer is taken as its last
    chunk (rule 3). Ghostty's delete-all keeps images behind placeholders (sprites,
    ``GlassImage``), so the range of ids glass uses is deleted as well.
    """
    data = "\x1b\\" + kitty.delete_all() * 2 + kitty.delete_range(Ids.FIRST, Ids.LAST)
    return data + (kitty.RESET_DEFAULT_BACKGROUND if terminal_background else "")


def terminal_cell_pixels() -> tuple[float, float, bool]:
    """Pixels per cell from TIOCGWINSZ, or a guess (10x20) when unknown."""
    for stream in (sys.__stdout__, sys.__stdin__):
        try:
            fd = stream.fileno()  # type: ignore[union-attr]
            rows, cols, xp, yp = struct.unpack("4H", fcntl.ioctl(fd, termios.TIOCGWINSZ, bytes(8)))
        except (AttributeError, OSError, ValueError):
            continue
        if xp and yp and cols and rows:
            return xp / cols, yp / rows, True
    return 10.0, 20.0, False


def make_grid(cols: int, rows: int, cell_w: float, cell_h: float) -> Grid:
    width, height = cols * cell_w, rows * cell_h
    scale = min(1.0, math.sqrt(MAX_PIXELS / max(1.0, width * height)))
    width, height = max(cols, round(width * scale)), max(rows, round(height * scale))
    return Grid(width / cols, height / rows, width, height)


def merge_rects(rects: list[Rect], gap: int = 4) -> list[Rect]:
    """Merge overlapping or nearby rectangles (x, y, w, h)."""
    boxes = [[x, y, x + w, y + h] for x, y, w, h in rects if w > 0 and h > 0]
    changed = True
    while changed and len(boxes) > 1:
        changed = False
        out: list[list[int]] = []
        for box in boxes:
            for other in out:
                if (box[0] <= other[2] + gap and other[0] <= box[2] + gap
                        and box[1] <= other[3] + gap and other[1] <= box[3] + gap):
                    other[0], other[1] = min(other[0], box[0]), min(other[1], box[1])
                    other[2], other[3] = max(other[2], box[2]), max(other[3], box[3])
                    changed = True
                    break
            else:
                out.append(box)
        boxes = out
    if len(boxes) > 12:
        boxes = [[min(b[0] for b in boxes), min(b[1] for b in boxes),
                  max(b[2] for b in boxes), max(b[3] for b in boxes)]]
    return [(b[0], b[1], b[2] - b[0], b[3] - b[1]) for b in boxes]


def _overlaps(box: Box, rect: Rect) -> bool:
    return box[0] < rect[0] + rect[2] and rect[0] < box[2] and box[1] < rect[1] + rect[3] and rect[1] < box[3]


def _within(items: tuple[Item, ...], rect: Rect, grid: Grid) -> list[Item]:
    """The items whose pixels reach into ``rect``."""
    return [item for item in items if _overlaps(item.visible_bbox(grid), rect)]


NO_IDS: frozenset[int] = frozenset()


def placeholder_ids(strip: Strip) -> frozenset[int]:
    """The image ids of the placeholder cells in a line (each run's id is its foreground colour)."""
    ids: set[int] | None = None
    for text, style, _ in strip:
        if text and text[0] == kitty.PLACEHOLDER and style is not None and style.color is not None:
            rgb = style.color.triplet
            if rgb is not None:
                if ids is None:
                    ids = set()
                ids.add(rgb[0] << 16 | rgb[1] << 8 | rgb[2])
    return frozenset(ids) if ids else NO_IDS


def _attached(user: Any) -> bool:
    """Is ``user`` (a widget, usually) still in the app? Objects that can't tell count as attached."""
    try:
        return bool(getattr(user, "is_attached", True))
    except Exception:  # noqa: BLE001
        return False


# ================================================================ layers ==


@dataclass
class Layer:
    """One terminal image: the bottom screen's backdrop, a modal's dialog glass, or a float layer."""

    key: tuple[int, str]
    z: int
    rgba: bool
    image_id: int | None = None
    cells: Rect = (0, 0, 0, 0)  # where it is placed
    rect: Rect = (0, 0, 0, 0)   # its pixels, in backdrop pixels
    grid: Grid | None = None
    base: np.ndarray | None = None                    # static items, float32
    static: tuple[Item, ...] = ()
    dynamic: tuple[Item, ...] = ()
    busy: bool = False                                # a render is in a worker thread
    stale: bool = False                               # the theme changed: redraw everything
    generation: int = 0
    page_key: frozenset[str] = frozenset()            # the panels it shows (one cached backdrop per page)


@dataclass
class Sprite:
    """A small image (often an animation) shown through placeholder cells."""

    image_id: int
    cols: int
    rows: int
    ready: bool = False
    hold: int = 0  # Textual frames to keep it although nothing shows it yet (it just landed)
    waiters: weakref.WeakSet[Any] = field(default_factory=weakref.WeakSet)  # refreshed when it lands
    users: weakref.WeakSet[Any] = field(default_factory=weakref.WeakSet)    # every widget that asked for it


@dataclass
class Dimmable:
    """An image shown through placeholders that has a dimmed, gapless frame."""

    dim_frame: int  # 1-based frame number of the dimmed copy
    animated: bool  # resume playback (s=3) when it is undimmed
    users: weakref.WeakSet[Any] = field(default_factory=weakref.WeakSet)


#: ``make(width_px, height_px) -> (rgba_frames, gap_ms)``, run in the worker thread.
SpriteMaker = Callable[[int, int], tuple[list[bytes], int]]


@dataclass
class Options:
    enabled: bool | None = None
    motion: str = "subtle"
    background: Any = "auto"
    query: bool = True
    skip: tuple[str, ...] = ("Button",)
    terminal_background: bool = True


# ================================================================ engine ==


class GlassEngine:
    """Everything glass does for one app. Reach it as ``app.glass``."""

    def __init__(self, app: App, options: Options) -> None:
        self.app = app
        self.options = options
        self.motion = options.motion if options.motion in MOTIONS else "subtle"
        self.support: Support | None = None
        self.active = False
        self.palette: Palette | None = None
        self.writer = Writer(app)
        self.ids = Ids()
        self.layers: dict[tuple[int, str], Layer] = {}
        self.effects = Effects(self)
        self.tweens = Tweens()
        self.grid: Grid | None = None
        self.size: tuple[int, int] = (0, 0)
        self.last_scene: Scene | None = None
        self.last_error: str | None = None
        self.errors = 0  # every error since the app started
        #: Glass turned itself off after too many errors and tries again in this many seconds.
        self.retry_in: float | None = None
        self.clock: Callable[[], float] = time.monotonic  # for the error budget and retries
        self._error_times: deque[float] = deque()  # errors within the last ERROR_WINDOW seconds
        self._retries = 0  # automatic retries in a row
        self._retried_at: float | None = None
        self._retry_timer: Any = None
        self._forced = False  # activated with force=True (kept for retries and resumes)
        self._resume = False  # glass was on when the app was suspended
        self._session = 0  # bumped whenever glass turns off: worker results from before are dropped
        self.render_ms: dict[str, float] = {}
        self.pending_jobs = 0  # renders and jobs running in the worker thread
        self.frames_synced = 0
        #: Small terminal-played images behind placeholder cells (see :meth:`sprite`).
        self.sprites: dict[tuple[Any, ...], Sprite] = {}
        #: Which sprite images each pixel widget's lines show: widget -> {line: image ids}.
        self._showing: weakref.WeakKeyDictionary[Any, dict[int, frozenset[int]]] = weakref.WeakKeyDictionary()
        #: Sprites whose maker raised: full key -> (failures in a row, when to try again).
        self._sprite_failures: dict[tuple[Any, ...], tuple[int, float]] = {}
        #: Uploads kept for reuse (``GlassImage(cache_key=...)``): key -> image id.
        self.image_cache: dict[tuple[Any, ...], int] = {}
        #: Backdrops of other pages, unplaced but kept in the terminal (page key -> layer).
        self.page_cache: dict[frozenset[str], Layer] = {}
        #: Images with a dimmed frame, and the ones currently showing it.
        self.dimmables: dict[int, Dimmable] = {}
        self.dimmed: set[int] = set()
        self._bgs: dict[tuple[Grid, Palette], np.ndarray] = {}
        self._palette_key: tuple[Any, ...] | None = None
        self._filters: weakref.WeakKeyDictionary[Widget, GlassFilter] | None = None
        self._rules: weakref.WeakKeyDictionary[Widget, tuple[Any, Rules]] | None = None
        self._generation = 0
        self._borders = hooks.BorderGlyphs()
        self._unwrap: Callable[[], None] | None = None
        self._timer: Any = None
        self._executor: ThreadPoolExecutor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app_context = contextvars.copy_context()  # callbacks from the worker run in it (rule 13)
        self._in_sync = False
        self._images: dict[int, Any] = {}  # GlassImage widgets by id

    # -- detection and lifecycle --------------------------------------------------

    @property
    def supported(self) -> bool:
        return bool(self.support and self.support.enabled)

    def detect(self) -> Support:
        """Decide before Textual's driver starts reading input (called from run_async)."""
        if self.options.enabled is False:
            self.support = Support(False, "disabled by the app")
        else:
            self.support = detect(query=self.options.query)
        return self.support

    def start(self) -> None:
        """Turn glass on, if it is supported (called once the app is mounted)."""
        if self.supported and not self.active:
            self.activate()

    def activate(self, force: bool = False) -> None:
        """Turn glass on, with a fresh error budget. ``force`` also allows it under
        Textual's headless driver (pilot tests); output then goes nowhere unless
        a test captures it."""
        self._cancel_retry()
        self._retries = 0
        self._activate(force)

    def _activate(self, force: bool) -> None:
        app = self.app
        if self.active or (app.is_headless and not force) or app.is_inline or not hooks.has_driver(app):
            return
        self._forced = force
        self._error_times.clear()
        self._loop = asyncio.get_running_loop()
        self._app_context = contextvars.copy_context()  # Textual's active_app is set here
        self._filters = weakref.WeakKeyDictionary()
        self._rules = weakref.WeakKeyDictionary()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="textual-glass")
        self.palette = Palette.from_app(app, self.options.background)
        self._generation += 1
        self.active = True
        hooks.add_filter_provider(app, self.filter_for)
        if not self._borders.blank(GLASS_EDGES) and not self._borders.active:
            self._log("Textual's border table is missing or has an unexpected shape; borders keep their glyphs")
        app.add_class("-textual-glass")
        self._unwrap = hooks.wrap_end_update(app, self.on_frame)
        if self._timer is None:
            self._timer = app.set_interval(1 / 60, self.tick, pause=True, name="textual-glass")
        if self.options.terminal_background:
            self.writer.queue(kitty.set_default_background(hex_of(self.palette.mid_background)))
        if not app.is_headless:  # a real terminal: clean it up even if the process is killed
            on_hard_exit(EXIT_KEY, hard_exit_sequence(self.options.terminal_background))
        self._refresh_all()
        for widget in list(self._images.values()):
            widget.glass_changed()

    def deactivate(self) -> None:
        """Turn glass off: delete every image and give Textual its plain look back
        (and forget a retry after errors that was pending)."""
        self._cancel_retry()
        self._deactivate()

    def _deactivate(self) -> None:
        if not self.active:
            return
        self._delete_everything()
        self.active = False
        on_hard_exit(EXIT_KEY, None)
        self._session += 1
        self.pending_jobs = 0  # queued jobs are cancelled below; a running one is dropped when it lands
        hooks.remove_filter_provider(self.app)
        self._borders.restore()
        self.app.remove_class("-textual-glass")
        if self._unwrap is not None:
            self._unwrap()
            self._unwrap = None
        if self._timer is not None:
            self._timer.pause()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self._refresh_all()
        for widget in list(self._images.values()):
            widget.glass_changed()

    def app_suspended(self, app: Any = None) -> None:
        """Textual's suspend signal (``App.suspend``, ctrl+Z bound to ``suspend_process``):
        Textual is about to hand the terminal over. Reset OSC 11 and delete every
        image now, while it is still on the alternate screen, so the shell gets a
        clean terminal; everything is forgotten and drawn again on resume."""
        if self.active:
            self._resume = True
            self._deactivate()

    def app_resumed(self, app: Any = None) -> None:
        """Textual's resume signal: the app has the terminal again, on an alternate
        screen that was cleared when it came back (in Ghostty that clear keeps
        placeholder images, but nothing else). Draw everything from scratch: new
        images, never edits of old ones. If glass missed the suspend, it starts over too."""
        resume = self._resume or self.active
        self._resume = False
        self._deactivate()
        if resume:
            self._activate(self._forced)

    def shutdown(self) -> None:
        """The app is exiting: clean the terminal up (deletes, OSC 111)."""
        self._cancel_retry()
        if self.active:
            self._delete_everything()
            self.active = False
            on_hard_exit(EXIT_KEY, None)
            self._session += 1
            hooks.remove_filter_provider(self.app)
            self._borders.restore()
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)

    def _delete_everything(self) -> None:
        for image_id in sorted(self.ids.live):
            self.writer.queue(kitty.delete(image_id))
        self.ids.live.clear()
        if self.options.terminal_background:
            self.writer.queue(kitty.RESET_DEFAULT_BACKGROUND)
        self.writer.flush()
        for layer in [*self.layers.values(), *self.page_cache.values()]:
            layer.generation += 1  # results of renders in flight are dropped
        self.layers.clear()
        self.page_cache.clear()
        self.sprites.clear()
        self._showing.clear()
        self._sprite_failures.clear()
        self.image_cache.clear()
        self.dimmables.clear()
        self.dimmed.clear()
        self.effects.reset()
        self.grid = None
        self._bgs.clear()
        for widget in list(self._images.values()):
            widget.forget_upload()

    def _refresh_all(self) -> None:
        """Re-render every widget (new filters and border glyphs)."""
        for screen in self.app.screen_stack:
            for widget in screen.walk_children(with_self=True):
                widget.refresh()
        self.app.refresh(layout=True)

    def set_motion(self, motion: str) -> None:
        if motion not in MOTIONS:
            raise ValueError(f"motion must be one of {MOTIONS}")
        self.motion = motion
        if self.active:
            if motion != "full":
                self.effects.clear_ambient()
            self.kick()

    # -- the line filter ---------------------------------------------------------------

    def filter_for(self, widget: Widget) -> GlassFilter | None:
        if not self.active or self._filters is None:
            return None
        found = self._filters.get(widget)
        if found is None:
            found = self._filters[widget] = GlassFilter(widget, self.rules)
        return found

    def rules(self, widget: Widget) -> Rules:
        if not self.active or self.palette is None or self._rules is None:
            return NO_RULES
        try:
            key = (widget.background_colors, widget.has_focus, widget.classes, getattr(widget, "cursor_type", None),
                   self._generation)
            cached = self._rules.get(widget)
            if cached is not None and cached[0] == key:
                return cached[1]
            rules = rules_for(widget, self.palette, self.options.skip)
            self._rules[widget] = (key, rules)
            return rules
        except Exception:  # noqa: BLE001 - never break Textual's rendering
            return NO_RULES

    # -- frames --------------------------------------------------------------------------

    def on_frame(self) -> None:
        """At the end of each Textual frame: update the glass inside the same frame."""
        if not self.active or self._in_sync:
            return
        self.writer.in_frame = True
        try:
            try:
                self._sync(time.monotonic())
                self._sweep_sprites()
            except Exception as error:  # noqa: BLE001
                self._failed(error)  # may turn glass off: its deletes go into this frame too
            self.writer.drain_into_frame()
        finally:
            self.writer.in_frame = False

    def tick(self) -> None:
        """60 Hz while something animates (pill slides, fades); paused when idle or off."""
        if not self.active:
            if self._timer is not None:
                self._timer.pause()
            return
        now = time.monotonic()
        busy = False
        try:
            busy = self._sync(now)
            self.effects.step(now, {key[0] for key in self.layers})
        except Exception as error:  # noqa: BLE001
            self._failed(error)
        self.writer.flush()
        if not (busy or self.tweens.busy(now) or self.effects.animating(now)) and self._timer is not None:
            self._timer.pause()

    def kick(self) -> None:
        """Run the 60 Hz tick until animations finish (never while glass is off: a
        render that lands after ``deactivate`` must not start it again)."""
        if self.active and self._timer is not None:
            self._timer.resume()

    def _log(self, text: str, error: bool = False) -> None:
        log = self.app.log
        (log.error if error else log.warning)(f"textual-glass: {text}")

    def _failed(self, error: BaseException) -> None:
        """Count an error. More than ERROR_LIMIT within ERROR_WINDOW seconds turn
        glass off (the app carries on, plain); it then tries again later."""
        import traceback

        self.errors += 1
        self.last_error = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        self._log(repr(error), error=True)
        now = self.clock()
        times = self._error_times
        times.append(now)
        while now - times[0] > ERROR_WINDOW:
            times.popleft()
        if self.active and len(times) > ERROR_LIMIT:
            self._give_up(now, len(times))

    def _give_up(self, now: float, count: int) -> None:
        """Too many errors: plain Textual for now, and a retry after the next delay."""
        self._error_times.clear()
        if self._retried_at is not None and now - self._retried_at >= HEALTHY:
            self._retries = 0  # the last retry stayed healthy: this is a new problem
        self._deactivate()
        why = f"{count} errors within {ERROR_WINDOW:g} s"
        if self._retries >= len(RETRY_DELAYS):
            self.retry_in = None
            self._log(f"{why}, turning glass off for good ({len(RETRY_DELAYS)} retries failed)", error=True)
            return
        delay = RETRY_DELAYS[self._retries]
        self._retries += 1
        self.retry_in = delay
        self._log(f"{why}, turning glass off; trying again in {delay:g} s "
                  f"(retry {self._retries} of {len(RETRY_DELAYS)})", error=True)
        self._retry_timer = self.app.set_timer(delay, self._retry, name="textual-glass retry")

    def _retry(self) -> None:
        self._retry_timer = None
        self.retry_in = None
        if self.active or not self.supported:
            return
        self._log(f"trying glass again (retry {self._retries} of {len(RETRY_DELAYS)})")
        self._retried_at = self.clock()
        self._activate(self._forced)

    def _cancel_retry(self) -> None:
        if self._retry_timer is not None:
            self._retry_timer.stop()
            self._retry_timer = None
        self.retry_in = None

    def _from_worker(self, session: int, callback: Callable[..., None], *args: Any) -> None:
        """Every callback from the worker thread runs through here, on the app's
        thread and in its context (rule 13). Results from before glass last
        turned off are dropped; an exception counts like any other glass error
        (asyncio would otherwise just log it)."""
        if session != self._session:
            return
        self.pending_jobs -= 1
        try:
            callback(*args)
        except Exception as error:  # noqa: BLE001
            self._failed(error)

    def _post(self, loop: asyncio.AbstractEventLoop, context: contextvars.Context, session: int,
              callback: Callable[..., None], *args: Any) -> None:
        """From the worker thread: run ``callback(*args)`` on the app's thread (see ``_from_worker``)."""
        try:
            loop.call_soon_threadsafe(self._from_worker, session, callback, *args, context=context.copy())
        except RuntimeError:  # the loop has closed
            pass

    # -- syncing the scene -------------------------------------------------------------

    def _sync(self, now: float) -> bool:
        """Bring the terminal up to date with the scene; True while something animates."""
        self._in_sync = True
        try:
            return self._sync_inner(now)
        finally:
            self._in_sync = False

    def _sync_inner(self, now: float) -> bool:
        app = self.app
        cols, rows = app.size
        if not cols or not rows:
            return False
        palette_key = (app.theme, id(app.current_theme), app.ansi_color)
        if palette_key != self._palette_key or self.palette is None:
            self._palette_key = palette_key
            palette = Palette.from_app(app, self.options.background)
            if palette != self.palette:
                self.palette = palette
                self._generation += 1
                self._bgs.clear()
                self._drop_page_cache()  # other pages' backdrops are in the old colours
                if self.options.terminal_background:
                    self.writer.queue(kitty.set_default_background(hex_of(palette.mid_background)))
                self.effects.recolour()
                for layer in self.layers.values():
                    layer.stale = True
                    if layer.busy:  # its result is out of date: drop it when it lands
                        layer.generation += 1
                        layer.busy = False
        palette = self.palette
        if (cols, rows) != self.size or self.grid is None:
            cell_w, cell_h, _ = terminal_cell_pixels()
            resized = self.grid is not None
            self.size = (cols, rows)
            self.grid = make_grid(cols, rows, cell_w, cell_h)
            if resized:
                self._drop_page_cache()
                self._stretch_for_resize()
        ctx = PixelContext(self, palette, now, 0, app.focused, self.motion != "minimal", self.tweens,
                           self.options.skip)
        scene = build(app, ctx)
        self.last_scene = scene
        levels = scene.levels[:MAX_LEVELS]
        live = set()
        for ls in levels:
            parts = (("main", ls.static, ls.dynamic), ("float", ls.float_static, ls.float_dynamic))
            for kind, static, dynamic in parts:
                key = (ls.level, kind)
                live.add(key)
                self._update_layer(key, static, dynamic, ls.level > 0 or kind == "float")
        for key in [k for k in self.layers if k not in live]:
            self._drop_layer(key)
        self.effects.update_scrims(levels, now, lambda level: Z0 + level * LEVEL_STEP + Z_SCRIM)
        if self.motion == "full":
            self.effects.update_ambient(levels, Z0 + Z_AMBIENT, lambda level: Z0 + level * LEVEL_STEP + Z_AMBIENT)
        self._update_dimming(visible_screens(app)[:MAX_LEVELS])
        self.frames_synced += 1
        if scene.busy:
            self.kick()
        return scene.busy

    def _z(self, key: tuple[int, str]) -> int:
        level, kind = key
        return Z0 + level * LEVEL_STEP + (Z_FLOAT if kind == "float" else 0)

    def _stretch_for_resize(self) -> None:
        """Until the new backdrop arrives, stretch the old one over the new size."""
        cols, rows = self.size
        layer = self.layers.get(MAIN)
        if layer is not None and layer.image_id is not None:
            self.writer.queue(kitty.place(layer.image_id, 0, 0, cols, rows, layer.z))
        self.effects.stretch()

    # -- one layer ---------------------------------------------------------------------------

    def _update_layer(self, key: tuple[int, str], static: list[Item], dynamic: list[Item], rgba: bool) -> None:
        grid = self.grid
        assert grid is not None and self.palette is not None
        static_t, dynamic_t = tuple(static), tuple(dynamic)
        if key == MAIN and self._switch_page(static_t, dynamic_t, grid):
            return
        layer = self.layers.get(key)
        if layer is None:
            if not static_t and not dynamic_t:
                return
            layer = self.layers[key] = Layer(key, self._z(key), rgba,
                                             page_key=self._page_key(static_t) if key == MAIN else frozenset())
        if not static_t and not dynamic_t and key != MAIN:
            self._drop_layer(key)
            return
        if layer.busy:
            return  # the worker's result will be compared with the scene when it lands
        cells = self._layer_cells(key, static_t + dynamic_t, grid)
        new_image = layer.image_id is None or layer.grid != grid or layer.cells != cells
        if layer.stale and not new_image and key == MAIN:
            layer.stale = False  # a theme change: the new colours land in this frame
            self._render_quick(layer, static_t, dynamic_t)
            return
        if new_image or layer.stale:
            layer.stale = False
            self._render_job(layer, grid, cells, static_t, dynamic_t, None, new_image)
            return
        if static_t == layer.static and dynamic_t == layer.dynamic:
            return
        static_rects = self._changed(layer.static, static_t, grid, layer.rect)
        area = sum(w * h for _, _, w, h in static_rects)
        total = max(1, layer.rect[2] * layer.rect[3])
        if area > SYNC_FRACTION * total:
            if area <= FULL_FRACTION * total:
                self._render_job(layer, grid, cells, static_t, dynamic_t, static_rects, False)
            elif key == MAIN:  # a new layout: like a new page, it lands with the text
                self._render_quick(layer, static_t, dynamic_t)
            else:
                self._render_job(layer, grid, cells, static_t, dynamic_t, None, False)
            return
        dynamic_rects = self._changed(layer.dynamic, dynamic_t, grid, layer.rect)
        rects = merge_rects(static_rects + dynamic_rects)
        if not rects:
            layer.static, layer.dynamic = static_t, dynamic_t
            return
        t0 = time.perf_counter()
        out = []
        for rect in rects:
            if static_rects:
                self._restatic(layer, rect, static_t, grid)
            out.append(self._edit(layer, rect, dynamic_t, grid))
        layer.static, layer.dynamic = static_t, dynamic_t
        self.writer.queue("".join(out))
        self.render_ms["edit"] = (time.perf_counter() - t0) * 1000

    def _layer_cells(self, key: tuple[int, str], items: tuple[Item, ...], grid: Grid) -> Rect:
        cols, rows = self.size
        if key == MAIN:
            return (0, 0, cols, rows)
        boxes = [item.visible_bbox(grid) for item in items]
        boxes = [b for b in boxes if b[2] > b[0] and b[3] > b[1]]
        if not boxes:
            return (0, 0, 0, 0)
        x0 = max(0, int(math.floor(min(b[0] for b in boxes) / grid.sx)))
        y0 = max(0, int(math.floor(min(b[1] for b in boxes) / grid.sy)))
        x1 = min(cols, int(math.ceil(max(b[2] for b in boxes) / grid.sx)))
        y1 = min(rows, int(math.ceil(max(b[3] for b in boxes) / grid.sy)))
        return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    @staticmethod
    def _cells_to_rect(cells: Rect, grid: Grid) -> Rect:
        c, r, w, h = cells
        x0, y0 = round(c * grid.sx), round(r * grid.sy)
        x1, y1 = round((c + w) * grid.sx), round((r + h) * grid.sy)
        return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def _changed(self, old: tuple[Item, ...], new: tuple[Item, ...], grid: Grid, bounds: Rect) -> list[Rect]:
        """Pixel rectangles (inside ``bounds``) of items that appeared, went away or changed.

        Items with the same ``ident`` are paired, and the new one says which of
        its pixels changed (``Item.diff_boxes``): a panel gaining focus redraws
        four border strips, not its whole interior."""
        if old == new:
            return []
        a, b = Counter(old), Counter(new)
        removed = list((a - b).elements())
        added = list((b - a).elements())
        by_ident: dict[str, Item] = {}
        for item in removed:
            if item.ident and item.ident not in by_ident:
                by_ident[item.ident] = item
        boxes: list[Box] = []
        for item in added:
            previous = by_ident.pop(item.ident, None) if item.ident else None
            if previous is not None:
                removed.remove(previous)
                precise = item.diff_boxes(previous, grid)
                if precise is not None:
                    boxes.extend(precise)
                    continue
                boxes.append(previous.visible_bbox(grid))
            boxes.append(item.visible_bbox(grid))
        boxes.extend(item.visible_bbox(grid) for item in removed)
        bx, by, bw, bh = bounds
        out = []
        for x0f, y0f, x1f, y1f in boxes:
            x0, y0 = max(bx, int(math.floor(x0f))), max(by, int(math.floor(y0f)))
            x1, y1 = min(bx + bw, int(math.ceil(x1f))), min(by + bh, int(math.ceil(y1f)))
            if x1 > x0 and y1 > y0:
                out.append((x0, y0, x1 - x0, y1 - y0))
        return merge_rects(out)

    def _background_for(self, grid: Grid) -> np.ndarray:
        """The backdrop painter's pixels for ``grid``. Worker renders draw the
        ones they miss off the app's thread (and warm the half-resolution one a
        page switch needs), so this rarely draws anything."""
        assert self.palette is not None
        found = self._bgs.get((grid, self.palette))
        if found is None:
            found = backdrop.draw(grid.width, grid.height, self.palette)
            self._keep_background(grid, self.palette, found)
        return found

    def _keep_background(self, grid: Grid, palette: Palette, data: np.ndarray) -> None:
        """Cache a backdrop: the full grid's and the half-resolution one are kept."""
        if palette != self.palette:
            return
        key = (grid, palette)
        self._bgs.pop(key, None)
        while len(self._bgs) >= 2:
            self._bgs.pop(next(iter(self._bgs)))
        self._bgs[key] = data

    def _quick_grid(self) -> Grid:
        """Half the backdrop's resolution: what a page switch renders inside the frame."""
        assert self.grid is not None
        cols, rows = self.size
        width, height = max(cols, round(self.grid.width / 2)), max(rows, round(self.grid.height / 2))
        return Grid(width / cols, height / rows, width, height)

    @staticmethod
    def _fresh(rgba: bool, rect: Rect, bg: np.ndarray | None) -> np.ndarray:
        x, y, w, h = rect
        if rgba or bg is None:
            return np.zeros((h, w, 4), np.float32)
        return bg[y:y + h, x:x + w].copy()

    def _restatic(self, layer: Layer, rect: Rect, static: tuple[Item, ...], grid: Grid) -> None:
        """Redraw the static items of one rectangle into the layer's base (main thread)."""
        assert layer.base is not None
        bg = None if layer.rgba else self._background_for(grid)
        data = self._fresh(layer.rgba, rect, bg)
        x, y, w, h = rect
        paint(Canvas(data, x, y), grid, _within(static, rect, grid))
        lx, ly = x - layer.rect[0], y - layer.rect[1]
        layer.base[ly:ly + h, lx:lx + w] = data

    def _edit(self, layer: Layer, rect: Rect, dynamic: tuple[Item, ...], grid: Grid) -> str:
        """The base of one rectangle plus the dynamic items, as an in-place edit."""
        assert layer.base is not None and layer.image_id is not None
        x, y, w, h = rect
        lx, ly = x - layer.rect[0], y - layer.rect[1]
        data = layer.base[ly:ly + h, lx:lx + w].copy()
        paint(Canvas(data, x, y), grid, _within(dynamic, rect, grid))
        return kitty.edit(layer.image_id, lx, ly, to_u8(data))

    # -- pages: one backdrop per page, kept in the terminal (rule 19) ----------------------

    @staticmethod
    def _page_key(static: tuple[Item, ...]) -> frozenset[str]:
        """What identifies a page's backdrop: the panels on it. Single-edge
        accents (a footer key's bar) don't count: the Footer recomposes its keys
        on every focus change, and that is no new page."""
        return frozenset(item.ident for item in static
                         if item.ident.startswith("panel:") and getattr(item, "full", True))

    def _switch_page(self, static: tuple[Item, ...], dynamic: tuple[Item, ...], grid: Grid) -> bool:
        """When the panels change (a page switch), swap backdrops in the same
        frame as the text: a cached one is placed at once (edits for whatever
        changed follow), a new one is rendered right here. Rendering it in the
        worker instead would leave the old page's panels under the new page's
        text for a moment. Returns True if it handled the frame."""
        current = self.layers.get(MAIN)
        key = self._page_key(static)
        if current is None or current.page_key == key:
            return False
        if current.image_id is None:
            current.page_key = key  # still on its first render: the normal path copes
            return False
        cols, rows = self.size
        cached = self.page_cache.pop(key, None)
        if cached is not None and (cached.grid != grid or cached.image_id is None):
            self._delete_layer_image(cached)
            cached = None
        if cached is not None:
            assert cached.image_id is not None
            self.layers[MAIN] = cached
            self.writer.queue(kitty.place(cached.image_id, 0, 0, cols, rows, cached.z))
            self._rebase_job(cached)
        else:
            layer = self.layers[MAIN] = Layer(MAIN, self._z(MAIN), rgba=False, page_key=key)
            self._render_quick(layer, static, dynamic)
        if current.grid == grid:
            self._park(current)
        else:  # a half-resolution or pre-resize image: not worth keeping
            self._delete_layer_image(current)
        return True

    def _render_quick(self, layer: Layer, static: tuple[Item, ...], dynamic: tuple[Item, ...]) -> None:
        """Render the main layer at half resolution right here, inside the frame
        (the terminal scales it to the window). The next tick sees the coarse
        grid and renders the full one in the worker, which then replaces it.
        The backdrop painters work in relative coordinates, so the two match."""
        cols, rows = self.size
        self._render_job(layer, self._quick_grid(), (0, 0, cols, rows), static, dynamic, None, True, sync=True)
        self.kick()

    def _park(self, layer: Layer) -> None:
        """Take a backdrop off screen but keep its image for the next visit."""
        if layer.busy:  # drop the result of a render in flight; the image keeps what it had
            layer.generation += 1
            layer.busy = False
        if layer.image_id is None:
            return
        self.writer.queue(kitty.unplace(layer.image_id))
        layer.base = None  # rebuilt from layer.static when it comes back
        old = self.page_cache.pop(layer.page_key, None)
        if old is not None and old is not layer:
            self._delete_layer_image(old)
        self.page_cache[layer.page_key] = layer
        while len(self.page_cache) > PAGE_CACHE:
            self._delete_layer_image(self.page_cache.pop(next(iter(self.page_cache))))

    def _delete_layer_image(self, layer: Layer) -> None:
        layer.generation += 1
        layer.busy = False
        if layer.image_id is not None:
            self.writer.queue(kitty.delete(layer.image_id))
            self.ids.free(layer.image_id)
            layer.image_id = None

    def _drop_page_cache(self) -> None:
        for layer in self.page_cache.values():
            self._delete_layer_image(layer)
        self.page_cache.clear()

    def _rebase_job(self, layer: Layer) -> None:
        """Rebuild a cached layer's base (backdrop plus what its image shows)
        in the worker; until then it takes no edits."""
        if self._executor is None or self._loop is None or layer.grid is None:
            return
        layer.busy = True
        self.pending_jobs += 1
        generation, grid, rect, static = layer.generation, layer.grid, layer.rect, layer.static
        bg = self._background_for(grid)
        loop, context, session = self._loop, self._app_context, self._session

        def work() -> None:
            try:
                data: Any = self._fresh(False, rect, bg)
                paint(Canvas(data, rect[0], rect[1]), grid, static)
            except Exception as error:  # noqa: BLE001
                data = error
            self._post(loop, context, session, self._rebased, layer, generation, data)

        self._executor.submit(work)

    def _rebased(self, layer: Layer, generation: int, base: Any) -> None:
        if generation != layer.generation:
            self.kick()
            return
        layer.busy = False
        if isinstance(base, Exception):
            self._failed(base)
            return
        layer.base = base
        self.tick()  # edits for anything that changed while the page was hidden

    # -- worker-thread renders ----------------------------------------------------------------

    def _render_job(self, layer: Layer, grid: Grid, cells: Rect, static: tuple[Item, ...],
                    dynamic: tuple[Item, ...], rects: list[Rect] | None, new_image: bool,
                    sync: bool = False) -> None:
        """Render in the worker thread, or right now with ``sync`` (so the
        pixels land in the same synchronized update as Textual's text)."""
        if self._executor is None:
            return
        layer.busy = True
        if not sync:
            self.pending_jobs += 1
        generation = layer.generation
        rect = self._cells_to_rect(cells, grid) if (new_image or rects is None) else layer.rect
        rgba, palette = layer.rgba, self.palette
        assert palette is not None
        bg: np.ndarray | None = None
        missing: list[Grid] = []  # backdrops the worker draws (and hands back for the cache)
        if not rgba:
            if sync:
                bg = self._background_for(grid)
            else:
                bg = self._bgs.get((grid, palette))
                missing = [g for g in dict.fromkeys((grid, self._quick_grid())) if (g, palette) not in self._bgs]
        loop, context, session = self._loop, self._app_context, self._session

        def work() -> None:
            nonlocal bg
            t0 = time.perf_counter()
            drawn: dict[Grid, np.ndarray] = {}
            try:
                drawn.update((g, backdrop.draw(g.width, g.height, palette)) for g in missing)
                if bg is None and not rgba:
                    bg = drawn[grid]
                if rects is None:
                    data = self._fresh(rgba, rect, bg)
                    canvas = Canvas(data, rect[0], rect[1])
                    paint(canvas, grid, static)
                    base = data.copy()
                    paint(canvas, grid, dynamic)
                    result: Any = ("full", base, kitty.png(to_u8(data), 1 if sync else 3))
                else:
                    patches = []
                    for r in rects:
                        patch = self._fresh(rgba, r, bg)
                        paint(Canvas(patch, r[0], r[1]), grid, _within(static, r, grid))
                        frame = patch.copy()
                        paint(Canvas(frame, r[0], r[1]), grid, _within(dynamic, r, grid))
                        patches.append((r, patch, to_u8(frame)))
                    result = ("rects", patches, None)
            except Exception as error:  # noqa: BLE001
                result = ("error", error, None)
            ms = (time.perf_counter() - t0) * 1000
            if sync:
                self._job_done(layer, generation, grid, cells, rect, static, dynamic, new_image, result, ms,
                               catch_up=False)
                return
            if loop is not None:
                self._post(loop, context, session, self._job_done, layer, generation, grid, cells, rect, static,
                           dynamic, new_image, result, ms, True, palette, drawn)

        if sync:
            work()
        else:
            self._executor.submit(work)

    def _job_done(self, layer: Layer, generation: int, grid: Grid, cells: Rect, rect: Rect,
                  static: tuple[Item, ...], dynamic: tuple[Item, ...], new_image: bool, result: Any,
                  ms: float, catch_up: bool = True, palette: Palette | None = None,
                  drawn: dict[Grid, np.ndarray] | None = None) -> None:
        if self.active and palette is not None:
            for g, data in (drawn or {}).items():
                self._keep_background(g, palette, data)
        if generation != layer.generation:
            self.kick()  # a newer render is wanted; the next tick schedules it
            return
        layer.busy = False
        if not self.active or self.layers.get(layer.key) is not layer:
            return
        kind, payload, png = result
        if kind == "error":
            self._failed(payload)
            return
        self.render_ms["rects" if kind == "rects" else "full" if catch_up else "quick"] = ms
        if kind == "full":
            layer.base = payload
            if new_image:
                old = layer.image_id
                layer.image_id = self.ids.new()
                c, r, w, h = cells
                self.writer.queue(kitty.command(dict(a="t", i=layer.image_id, f=100, s=rect[2], v=rect[3]), png)
                                  + kitty.place(layer.image_id, c, r, w, h, layer.z))
                if old is not None:
                    self.writer.queue(kitty.delete(old))
                    self.ids.free(old)
            else:
                keys = dict(a="f", i=layer.image_id, r=1, f=100, s=rect[2], v=rect[3], X=1)
                self.writer.queue(kitty.command(keys, png))
            layer.grid, layer.cells, layer.rect = grid, cells, rect
        else:
            assert layer.base is not None and layer.image_id is not None
            out = []
            for (x, y, w, h), patch, frame in payload:
                lx, ly = x - layer.rect[0], y - layer.rect[1]
                layer.base[ly:ly + h, lx:lx + w] = patch
                out.append(kitty.edit(layer.image_id, lx, ly, frame))
            self.writer.queue("".join(out))
        layer.static, layer.dynamic = static, dynamic
        if catch_up:  # anything that changed while the worker ran, in the same update
            self.tick()
            self.kick()

    def _drop_layer(self, key: tuple[int, str]) -> None:
        layer = self.layers.pop(key, None)
        if layer is None:
            return
        layer.generation += 1
        if layer.image_id is not None:
            self.writer.queue(kitty.delete(layer.image_id))
            self.ids.free(layer.image_id)

    def background_job(self, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        """Run ``work()`` in the worker thread, then ``done(result)`` on the app's
        thread, in the app's context (Textual's ``active_app`` is set there),
        followed by a flush of whatever ``done`` queued."""
        executor, loop, context, session = self._executor, self._loop, self._app_context, self._session
        if executor is None or loop is None:
            return

        def run() -> None:
            try:
                result = work()
            except Exception as error:  # noqa: BLE001
                result = error
            self._post(loop, context, session, self._background_done, done, result)

        self.pending_jobs += 1
        executor.submit(run)

    def _background_done(self, done: Callable[[Any], None], result: Any) -> None:
        if not self.active:
            return
        if isinstance(result, Exception):
            self._failed(result)
            return
        done(result)
        self.writer.flush()

    # -- sprites: small terminal-played images behind placeholder cells -----------------------

    def cell_pixels(self) -> tuple[float, float]:
        """Terminal pixels per cell (not capped like the backdrop's grid)."""
        cell_w, cell_h, _ = terminal_cell_pixels()
        return cell_w, cell_h

    @property
    def dim_factor(self) -> float:
        """How bright an image shows below a popup: what the scrim leaves of it."""
        return 1 - (self.palette.scrim_alpha if self.palette is not None else 0.5)

    def sprite(self, key: tuple[Any, ...], cols: int, rows: int, make: SpriteMaker,
               waiter: Any = None) -> int | None:
        """The image id of a sprite ``cols`` x ``rows`` cells big, or None until it
        has been uploaded (``waiter.refresh()`` is called when it is ready).

        ``make(width_px, height_px)`` returns ``(rgba_frames, gap_ms)``; it runs
        in the worker thread. One frame is a still image; several are uploaded
        while stopped and then looped by the terminal (no CPU, no traffic).
        Sprites with the same key and size are shared, so every "waiting"
        beacon on screen uses one image. Each carries a dimmed copy of its first
        frame (gapless, so playback skips it) for while a popup covers it.
        Write ``kitty.placeholder_row(row, cols)`` in ``kitty.id_color(image_id)``
        to show it; returns None while glass is off.

        Pass the widget that shows it as ``waiter``: the sprite is deleted from
        the terminal once no such widget shows it any more (it switched to
        another key or size, or it was removed). A ``PixelMixin`` widget is
        checked line by line, from what it renders; any other widget holds its
        sprites until it is removed. A sprite asked for without a ``waiter`` is
        kept until glass turns off. If ``make`` raises, the error is counted
        and the sprite is tried again on a later call, after 5 s, 30 s, then
        every 5 minutes (``None`` until then, so the widget's fallback shows).
        """
        if not self.active or cols < 1 or rows < 1 or cols > len(kitty.DIACRITICS) or rows > len(kitty.DIACRITICS):
            return None
        cell_w, cell_h = self.cell_pixels()
        scale = min(1.0, SPRITE_MAX_PX / max(cols * cell_w, rows * cell_h))
        width, height = max(2, round(cols * cell_w * scale)), max(2, round(rows * cell_h * scale))
        full_key = (*key, cols, rows, width, height)
        found = self.sprites.get(full_key)
        if found is not None:
            if waiter is not None:
                found.users.add(waiter)
            if found.ready:
                return found.image_id
            if waiter is not None:
                found.waiters.add(waiter)
            return None
        failure = self._sprite_failures.get(full_key)
        if failure is not None and self.clock() < failure[1]:
            return None  # its maker raised a moment ago
        image_id = self.ids.new()
        sprite = self.sprites[full_key] = Sprite(image_id, cols, rows)
        if waiter is not None:
            sprite.waiters.add(waiter)
            sprite.users.add(waiter)
        factor = self.dim_factor

        def work() -> tuple[str, int] | tuple[None, Exception]:
            try:
                frames, gap = make(width, height)
                dim = dim_frame(frames[0], factor)  # gapless: playback never shows it
                if len(frames) == 1:
                    body = kitty.transmit_raw(image_id, width, height, frames[0])
                    body += kitty.add_frame(image_id, width, height, dim, gap_ms=-1)
                else:
                    body = kitty.animation(image_id, width, height, frames, gap, gapless=[dim])
                return body + kitty.place_virtual(image_id, cols, rows), len(frames)
            except Exception as error:  # noqa: BLE001 - the app's maker: retried later (see done)
                return None, error

        def done(result: tuple[str, int] | tuple[None, Exception]) -> None:
            if self.sprites.get(full_key) is not sprite:
                return
            if result[0] is None:
                self._sprite_failed(full_key, sprite, result[1])
                return
            commands, count = result
            self._sprite_failures.pop(full_key, None)
            self.writer.queue(commands)
            self.dimmables[image_id] = Dimmable(count + 1, count > 1, sprite.users)
            sprite.ready = True
            sprite.hold = SPRITE_HOLD
            for widget in list(sprite.waiters):
                try:
                    widget.refresh()
                except Exception:  # noqa: BLE001
                    pass
            sprite.waiters.clear()

        self.background_job(work, done)
        return None

    def _sprite_failed(self, full_key: tuple[Any, ...], sprite: Sprite, error: Exception) -> None:
        """Its maker raised: drop it (nothing was uploaded) and allow a new try after a delay."""
        del self.sprites[full_key]
        self.ids.free(sprite.image_id)
        failures = self._sprite_failures.pop(full_key, (0, 0.0))[0] + 1
        delay = SPRITE_RETRY[min(failures, len(SPRITE_RETRY)) - 1]
        self._sprite_failures[full_key] = (failures, self.clock() + delay)
        while len(self._sprite_failures) > 64:
            del self._sprite_failures[next(iter(self._sprite_failures))]
        self._failed(error)

    def _note_lines(self, widget: Any, crop: Region, strips: list[Strip]) -> None:
        """What ``widget`` just rendered (``PixelMixin.render_lines``): which sprite
        images its lines show. Lines outside ``crop`` keep what they showed before
        (Textual still has them cached), and so does a line cropped narrower than
        the widget: its hidden part may still hold a placeholder."""
        lines = self._showing.get(widget)
        if lines is None:
            lines = self._showing[widget] = {}
        width, height = widget.outer_size
        whole = crop.x == 0 and crop.width >= width
        for y, line in enumerate(strips, crop.y):
            ids = placeholder_ids(line)
            if not whole:
                ids |= lines.get(y, NO_IDS)
            if ids:
                lines[y] = ids
            else:
                lines.pop(y, None)
        for y in [y for y in lines if y >= height]:
            del lines[y]

    def _shown(self, sprite: Sprite) -> bool:
        """Does a widget that asked for ``sprite`` still show it (or wait for it)?"""
        if not sprite.users:
            return True  # asked for without a waiter: kept until glass turns off
        for user in list(sprite.users):
            if not _attached(user):
                continue
            lines = self._showing.get(user)
            if not sprite.ready or lines is None:  # waiting for the upload, or a widget that doesn't report lines
                return True
            if any(sprite.image_id in ids for ids in lines.values()):
                return True
        return False

    def _sweep_sprites(self) -> None:
        """At the end of each Textual frame: delete the sprites no widget shows any more."""
        for full_key, sprite in list(self.sprites.items()):
            if sprite.hold > 0:
                sprite.hold -= 1
            elif not self._shown(sprite):
                del self.sprites[full_key]
                if sprite.ready:  # uploaded (an upload still in the worker is dropped when it lands)
                    self.writer.queue(kitty.delete(sprite.image_id))
                self.ids.free(sprite.image_id)
                self.dimmables.pop(sprite.image_id, None)
                self.dimmed.discard(sprite.image_id)

    # -- dimming placeholder images under popups (rule 18) ------------------------------------

    def register_dimmable(self, image_id: int, dim_frame: int, animated: bool, user: Any) -> None:
        """``image_id`` has a dimmed copy at frame ``dim_frame`` (gapless, so
        playback skips it); ``user`` is a widget that shows it."""
        found = self.dimmables.get(image_id)
        if found is None:
            found = self.dimmables[image_id] = Dimmable(dim_frame, animated)
        if user is not None:
            found.users.add(user)

    def _update_dimming(self, screens: list[Screen[Any]]) -> None:
        """Show the dimmed frame of every image used below the top popup.

        Placeholder images are drawn at z=-1, above the popup's scrim, so they
        would stay bright. Switching frames is one tiny command per image."""
        top = screens[-1] if len(screens) > 1 else None
        want: set[int] = set()
        if top is not None:
            live = self.ids.live
            for image_id, dimmable in self.dimmables.items():
                if image_id not in live:
                    continue
                for user in list(dimmable.users):
                    if not _attached(user):
                        continue
                    try:
                        screen = user.screen
                    except Exception:  # noqa: BLE001 - detached
                        continue
                    if screen is not top:
                        want.add(image_id)
                        break
        for image_id in sorted(want - self.dimmed):
            self.writer.queue(kitty.animate(image_id, c=self.dimmables[image_id].dim_frame, s=1))
        for image_id in sorted(self.dimmed - want):
            undim = self.dimmables.get(image_id)
            if undim is not None and image_id in self.ids.live:
                self.writer.queue(kitty.animate(image_id, c=1, s=3) if undim.animated
                                  else kitty.animate(image_id, c=1))
        self.dimmed = want

    # -- images for GlassImage -----------------------------------------------------------------

    def keep_image(self, key: tuple[Any, ...], image_id: int, limit: int = 4) -> None:
        """Keep an upload for reuse (see ``GlassImage(cache_key=...)``); the
        oldest kept image is deleted beyond ``limit``."""
        self.image_cache[key] = image_id
        while len(self.image_cache) > limit:
            old = self.image_cache.pop(next(iter(self.image_cache)))
            self.send(kitty.delete(old))
            self.ids.free(old)
            self.dimmables.pop(old, None)

    def register_image(self, widget: Any) -> None:
        self._images[id(widget)] = widget

    def unregister_image(self, widget: Any) -> None:
        self._images.pop(id(widget), None)

    def send(self, data: str, now: bool = True) -> None:
        """Queue commands; with ``now`` write them in their own synchronized update."""
        self.writer.queue(data)
        if now:
            self.writer.flush()

    def stats(self) -> dict[str, Any]:
        return dict(bytes_sent=self.writer.bytes_sent, commands=self.writer.commands, images=len(self.ids.live),
                    layers=sorted(self.layers), pages=len(self.page_cache), sprites=len(self.sprites),
                    frames=self.frames_synced, motion=self.motion, render_ms=dict(self.render_ms),
                    support=self.support.reason if self.support else None, active=self.active,
                    errors=self.errors, retry_in=self.retry_in)
