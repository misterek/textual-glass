"""``OrbitalApp``: a glass app that polls a provider, keeps the merged
:class:`~textual_orbital.model.Snapshot`, raises alerts and gets your
attention when the window is half hidden.

Subclass it, compose your pages from the widgets, and implement the hooks you
need:

* :meth:`OrbitalApp.snapshot_changed` (by default: ``update_snapshot(snapshot)``
  on every widget that has it, such as each :class:`~textual_orbital.widgets.Page`),
* :meth:`OrbitalApp.health_changed` (by default: ``set_statuses`` on every
  widget that has it, such as the header's source health),
* :meth:`OrbitalApp.minute_tick` (by default: ``minute_tick(at)`` on every
  widget that has it, for ages and timers),
* :meth:`OrbitalApp.show_detail` (what ``jump_to`` shows for a record),
* :meth:`OrbitalApp.badge_changed` (the "◉ 2 waiting" badge, for a header).

:meth:`OrbitalApp.fetch` runs a one-off call with parameters (a past week, a
record's comments) the same way as a source's fetch: a daemon thread, a
deadline, the result back on the app thread.

Cost model: each fetch runs in a daemon thread of its own, and the next one is
scheduled an interval after it ends (longer while the source fails). A fetch
whose data equals the last one changes nothing and sends nothing; the UI only
redraws on real changes and once a minute for ages.

Nothing raised on the app thread is lost: hooks, rules, pages and callbacks
from threads are guarded one step at a time, and what they raise goes to
:meth:`OrbitalApp.report_error` (the log, a toast, ``errors``).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import threading
import time
import webbrowser
from collections import deque
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, TypeVar
from urllib.parse import urlsplit

from textual import events, on
from textual.app import App
from textual.screen import Screen
from textual.timer import Timer

from textual_glass import Glass, LoadedImage, load_image, terminal_write

from .alerts import AlertQueue, AlertRules, Rule, WaitingRule
from .model import (
    ALERT_STYLES,
    Alert,
    AttentionItem,
    EntityState,
    ErrorReport,
    Health,
    Snapshot,
    SourceStatus,
    describe_error,
)
from .notify import Notifier
from .provider import Provider, Source, SourceData, Store, fetch_timeout, poll_delay, timed_fetch
from .theme import ORBITAL, ORBITAL_NAVY, now
from .widgets.screens import AlertScreen, AlertToast

log = logging.getLogger("textual_orbital")

T = TypeVar("T")

#: The Orbital look for panels: a border from the theme's primary colour (left,
#: top) to its secondary colour (right, bottom), which glass draws as a gradient.
#: ``Screen GlassPanel`` outranks the panel's own default rule; the app's CSS
#: and ``GlassPanel.set_accent`` outrank it.
ORBITAL_CSS = """
Screen GlassPanel {
    border: round $primary;
    border-right: round $secondary;
    border-bottom: round $secondary;
}
"""


def _name(callback: Callable[..., Any]) -> str:
    """What a callback is called, for an error report."""
    return getattr(callback, "__qualname__", None) or repr(callback)


def _read_snoozes(text: str) -> tuple[dict[str, datetime], dict[str, datetime]]:
    """Parse a snooze file: ``{"items": {key: until}, "alerts": {key: until}}``,
    each ``until`` an ISO time with an offset. Raises ValueError (or TypeError)
    for anything else."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    parts: list[dict[str, datetime]] = []
    for part in ("items", "alerts"):
        entries = data.get(part, {})
        if not isinstance(entries, dict):
            raise ValueError(f"{part!r} is not an object")
        parsed: dict[str, datetime] = {}
        for key, value in entries.items():
            until = datetime.fromisoformat(value)
            if until.tzinfo is None:
                raise ValueError(f"{part} {key!r}: a time without an offset")
            parsed[key] = until
        parts.append(parsed)
    return parts[0], parts[1]


@dataclass
class _Poll:
    """The app's side of one source: its fetch in flight and its next one."""

    source: Source
    interval: float
    timeout: float
    token: int = 0  # the newest fetch's token; a result with an older one is late
    running: bool = False  # the fetch with ``token`` is in flight
    hung: int = 0  # fetches that timed out and haven't returned yet
    timer: Timer | None = None  # the next fetch
    deadline: Timer | None = None  # the deadline of the fetch in flight


class OrbitalApp(Glass, App[None]):
    """Polls ``provider`` and keeps ``self.snapshot`` current.

    Args:
        provider: Where the data comes from (see :mod:`textual_orbital.provider`).
        rules: The alert rules (see :mod:`textual_orbital.alerts`).
        alert_images: A picture or GIF per alert kind (paths), e.g. ``{"waiting": "~/art.gif"}``.
        alerts: Raise "incoming transmission" popups.
        alert_style: How alerts show: ``"modal"`` (a popup that takes the keyboard) or ``"toast"`` (a
            corner toast that takes no keys and goes away after ``toast_timeout`` seconds). An alert's
            own ``style`` overrides it.
        toast_timeout: Seconds a toast stays (a timeout counts as dismissed).
        notify: Desktop notifications: ``"auto"``, ``"osc777"``, ``"osc9"`` or ``"off"``.
        bell: Ring the bell with each alert (Ghostty bounces the dock icon).
        title_badge: Put "◉ 2 waiting" in the window title.
        open_urls: ``open_link`` opens the browser. False (the default) just shows the link; pass True
            in a real dashboard.
        open_hosts: With ``open_urls``, the hosts ``open_link`` may open (``{"github.com"}``); None
            means any http or https link.
        snooze_file: A JSON file that keeps item and alert snoozes across restarts (read at
            startup, written after every change); None keeps them in memory only.

    Other keyword arguments go to ``Glass`` and ``App`` (``glass=False`` for text only).
    """

    #: Classes set on every screen by width and height (for the layouts' CSS).
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (80, "-compact"), (112, "-regular"), (140, "-wide")]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (25, "-mid"), (38, "-tall")]

    DEFAULT_CSS = ORBITAL_CSS
    SCOPED_CSS = False  # rules for widgets, not scoped to the app's type (rule 11)

    #: How long ``s`` on an alert hides it, and how long an item snooze lasts.
    ALERT_SNOOZE: ClassVar[timedelta] = timedelta(minutes=10)
    ITEM_SNOOZE: ClassVar[timedelta] = timedelta(hours=1)
    #: The popup class for alerts, and the toast class for alerts shown as toasts.
    ALERT_SCREEN: ClassVar[type[AlertScreen]] = AlertScreen
    ALERT_TOAST: ClassVar[type[AlertToast]] = AlertToast
    #: Fetches of one source that may hang past their deadline at once. A thread
    #: can't be stopped, so at this many the source stops polling until one returns.
    HUNG_LIMIT: ClassVar[int] = 2
    #: How many reports ``errors`` keeps, and the least time between two error toasts (seconds).
    ERRORS_KEPT: ClassVar[int] = 20
    ERROR_TOAST_SPACING: ClassVar[float] = 30.0

    def __init__(self, provider: Provider, *, rules: Sequence[Rule] = (WaitingRule(),),
                 alert_images: dict[str, str] | None = None, alerts: bool = True, alert_style: str = "modal",
                 toast_timeout: float = 8.0, notify: str = "auto",
                 bell: bool = True, title_badge: bool = True, open_urls: bool = False,
                 open_hosts: Collection[str] | None = None, snooze_file: str | Path | None = None,
                 **kwargs: Any) -> None:
        if alert_style not in ALERT_STYLES:
            raise ValueError(f"alert_style must be one of {ALERT_STYLES}, not {alert_style!r}")
        super().__init__(**kwargs)
        self.provider = provider
        self.store = Store(provider.sources())
        self.snapshot: Snapshot | None = None
        self.alerts_enabled = alerts
        self.alert_style = alert_style
        self.toast_timeout = toast_timeout
        self.alert_queue = AlertQueue()
        images = {kind: str(Path(path).expanduser()) for kind, path in (alert_images or {}).items()}
        self.alert_rules = AlertRules(rules, images)
        self.alert_images: dict[str, LoadedImage | None] = {}
        self.notifier = Notifier(self, lambda data: terminal_write(self, data), mode=notify, bell=bell,
                                 title=title_badge)
        self.open_urls = open_urls
        self.open_hosts = None if open_hosts is None else frozenset(host.lower() for host in open_hosts)
        #: Snoozed attention items: key -> when they come back. Alert snoozes are ``alert_queue.snoozed``.
        self.snoozed: dict[str, datetime] = {}
        self.snooze_file = None if snooze_file is None else Path(snooze_file).expanduser()
        self._orbital_saved: str | None = None  # the snooze file's content as last read or written
        self.fetches = 0
        #: What ``report_error`` caught, oldest first (the same error once, with a count).
        self.errors: deque[ErrorReport] = deque(maxlen=self.ERRORS_KEPT)
        self._orbital_published = -1
        self._orbital_day: date | None = None  # the date of the last snapshot the pages got
        self._orbital_health: tuple[Health, ...] = ()
        self._orbital_loop: asyncio.AbstractEventLoop | None = None
        self._orbital_context = contextvars.copy_context()
        self._orbital_closed = False
        self._orbital_polls = {s.key: _Poll(s, max(0.25, float(s.interval)), fetch_timeout(s))
                               for s in self.store.sources}
        self._orbital_minute: Timer | None = None
        self._orbital_loading: set[str] = set()
        self._orbital_unshown = 0  # errors since the last toast
        self._orbital_toast_at = -1e9  # time.monotonic() of the last toast
        self._orbital_toast_timer: Timer | None = None
        self._orbital_calls = 0  # tokens for fetch()
        self._orbital_waiting: dict[int, tuple[Callable[[Any, BaseException | None], object], Timer]] = {}
        self._alert_view: AlertScreen | AlertToast | None = None  # the alert on screen
        self.register_theme(ORBITAL)
        self.register_theme(ORBITAL_NAVY)
        self.theme = ORBITAL.name

    # -- lifecycle -----------------------------------------------------------------------

    @on(events.Mount)
    def _orbital_mount(self) -> None:
        self._orbital_loop = asyncio.get_running_loop()
        # Results from worker threads run in (a copy of) the app's context, so
        # they can push screens and compose widgets like any handler (rule 13).
        self._orbital_context = contextvars.copy_context()
        self.notifier.badge("")  # the window title says what this window is
        self._attempt("snooze file", self._load_snoozes)  # before the first fetch can raise an alert
        self._load_images(sorted(set(self.alert_rules.images.values())))
        # immediate: a toast moves before a popped screen is removed with it on
        self.screen_change_signal.subscribe(self, self._screen_changed, immediate=True)
        for key in self._orbital_polls:
            self._poll(key)
        self._arm_minute()

    def exit(self, *args: Any, **kwargs: Any) -> None:
        # Fetch threads are daemons: quitting never waits for one (rule 21).
        self._orbital_closed = True
        self.notifier.restore()
        super().exit(*args, **kwargs)

    @on(events.Unmount)
    def _orbital_unmount(self) -> None:
        self._orbital_closed = True

    @on(events.Resize)
    def _orbital_resize(self, event: events.Resize) -> None:
        if isinstance(self._alert_view, AlertToast) and self._alert_view.is_attached:
            self._alert_view.place(event.size.width)

    def call_from_worker(self, callback: Callable[..., Any], *args: Any) -> None:
        """Run ``callback(*args)`` on the app's loop, in the app's context, from
        any thread (rule 13). What it raises goes to :meth:`report_error`.
        Does nothing once the app has stopped."""
        loop = self._orbital_loop
        if loop is None:
            return

        def run() -> None:
            try:
                callback(*args)
            except Exception as error:  # noqa: BLE001 - reported, never lost in the loop's handler (rule 22)
                self.report_error(_name(callback), error)

        try:
            loop.call_soon_threadsafe(run, context=self._orbital_context.copy())
        except RuntimeError:  # the loop has closed
            pass

    # -- errors --------------------------------------------------------------------------

    def report_error(self, where: str, error: BaseException) -> None:
        """Report an exception caught on the app thread: log it with its
        traceback (the ``textual_orbital`` logger), keep it in :attr:`errors` for
        the health screen, and show it in a toast, at most one every
        ``ERROR_TOAST_SPACING`` seconds (later ones are counted in the next).

        ``where`` names what raised (``"rules"``, ``"MyPage.update_snapshot"``)."""
        message = describe_error(error)
        log.error("%s: %s", where, message, exc_info=error)
        count = 1
        for i, old in enumerate(self.errors):
            if (old.where, old.message) == (where, message):
                count += old.count
                del self.errors[i]
                break
        self.errors.append(ErrorReport(now(), where, message, count))
        self._orbital_unshown += 1
        self._toast_errors()

    def _toast_errors(self) -> None:
        if not self._orbital_unshown or not self.errors:
            return
        wait = self._orbital_toast_at + self.ERROR_TOAST_SPACING - time.monotonic()
        if wait > 0:
            if self._orbital_toast_timer is None:
                try:
                    self._orbital_toast_timer = self.set_timer(wait, self._toast_due, name="error-toast")
                except Exception:  # noqa: BLE001 - no running app: the log and ``errors`` have it
                    pass
            return
        last, more = self.errors[-1], self._orbital_unshown - 1
        self._orbital_unshown = 0
        self._orbital_toast_at = time.monotonic()
        text = f"{last.where}: {last.message}" + (f"\n(and {more} more)" if more else "")
        try:
            self.notify(text, title="Error", severity="error", timeout=10, markup=False)
        except Exception:  # noqa: BLE001 - no screen to toast on
            log.exception("could not show an error toast")

    def _toast_due(self) -> None:
        self._orbital_toast_timer = None
        self._toast_errors()

    def _attempt(self, where: str, step: Callable[..., object], *args: Any) -> bool:
        """Run one step; report what it raises (rule 22). False if it raised."""
        try:
            step(*args)
        except Exception as error:  # noqa: BLE001 - reported
            self.report_error(where, error)
            return False
        return True

    # -- polling -------------------------------------------------------------------------

    def poll_all(self) -> None:
        """Fetch every source now, failing ones too (their backoff is skipped)."""
        for key in self._orbital_polls:
            self._poll(key)

    def _poll(self, key: str) -> None:
        """Start a fetch of source ``key`` in a daemon thread of its own, with a deadline."""
        poll = self._orbital_polls[key]
        if self._orbital_closed or self._orbital_loop is None or poll.running:
            return
        if poll.timer is not None:
            poll.timer.stop()
            poll.timer = None
        if poll.hung >= self.HUNG_LIMIT:
            return  # until a hung fetch returns (see _fetched)
        poll.token += 1
        token, source = poll.token, poll.source

        def work() -> None:
            result: tuple[SourceData | None, str | None, float] = (None, "the fetch thread stopped", 0.0)
            try:
                result = timed_fetch(source)
            finally:  # always report back, or the source would stay in flight for good
                self.call_from_worker(self._fetched, key, token, *result)

        try:
            threading.Thread(target=work, daemon=True, name=f"orbital-fetch-{key}").start()
        except RuntimeError as error:  # no more threads
            self._finish(poll, None, describe_error(error))
            return
        poll.running = True
        poll.deadline = self.set_timer(poll.timeout, lambda: self._attempt("deadline", self._timed_out, key, token),
                                       name=f"deadline-{key}")

    def _timed_out(self, key: str, token: int) -> None:
        poll = self._orbital_polls[key]
        if not poll.running or poll.token != token or self._orbital_closed:
            return
        poll.running, poll.deadline = False, None
        poll.hung += 1  # its thread runs on; what it returns is ignored
        error = f"timed out after {poll.timeout:g} s"
        if poll.hung >= self.HUNG_LIMIT:
            error += f"; {poll.hung} fetches hung, paused until one returns"
        elif poll.hung > 1:
            error += f" ({poll.hung} fetches still hung)"
        self._finish(poll, None, error)

    def _fetched(self, key: str, token: int, data: SourceData | None, error: str | None, ms: float) -> None:
        poll = self._orbital_polls.get(key)
        if poll is None or self._orbital_closed:
            return
        if not poll.running or token != poll.token:  # a fetch that timed out came back: ignore it
            poll.hung = max(0, poll.hung - 1)
            if poll.timer is None and not poll.running:  # paused at HUNG_LIMIT: go on
                self._poll(key)
            return
        poll.running = False
        if poll.deadline is not None:
            poll.deadline.stop()
            poll.deadline = None
        self._finish(poll, data, error, ms)

    def _finish(self, poll: _Poll, data: SourceData | None, error: str | None, ms: float = 0.0) -> None:
        """A fetch ended (or timed out): merge or fail it, schedule the next one, publish."""
        key = poll.source.key
        self.fetches += 1
        at = now()
        try:
            if data is None:
                self.store.fail(key, error or "failed", at)
            else:
                self.store.apply(key, data, at, ms)
        except Exception as problem:  # noqa: BLE001 - reported, and the source still polls
            self.report_error(f"Store.apply {key}", problem)
            self.store.fail(key, describe_error(problem), at)
        finally:
            self._schedule(poll, at)
        self._publish(at)

    def _schedule(self, poll: _Poll, at: datetime) -> None:
        """The next fetch, an interval after this one ended; longer while it fails (O8)."""
        key = poll.source.key
        if poll.hung >= self.HUNG_LIMIT:
            self.store.schedule(key, None)
            return
        delay = poll_delay(poll.interval, self.store.status(key).failures)
        poll.timer = self.set_timer(delay, lambda: self._attempt("poll", self._poll, key), name=f"poll-{key}")
        self.store.schedule(key, at + timedelta(seconds=delay))

    # -- one-off fetches -----------------------------------------------------------------

    def fetch(self, func: Callable[..., T], *args: Any, then: Callable[[T | None, BaseException | None], object],
              timeout: float = 30.0) -> None:
        """Run ``func(*args)`` once, in a daemon thread of its own, and hand the
        result to ``then(data, error)`` on the app thread, in the app's context:
        ``(result, None)``, or ``(None, exception)`` when ``func`` raised (anything,
        ``SystemExit`` included) or ran past ``timeout`` seconds (a ``TimeoutError``;
        its late result is dropped). What ``then`` raises goes to :meth:`report_error`.

        This is how to fetch something with parameters on demand (a past week, a
        record's comments); sources are fetched on their interval only. ``func``
        must set its own network and subprocess timeouts: a thread can't be
        stopped (rule 21). ``then`` runs even if what asked has gone (a page
        switched away, a popup closed): check there whether the result is still wanted.
        """
        if self._orbital_closed:
            return
        if self._orbital_loop is None:  # called before the app's Mount (from a widget's on_mount)
            self._orbital_loop = asyncio.get_running_loop()
            self._orbital_context = contextvars.copy_context()
        self._orbital_calls += 1
        token = self._orbital_calls

        def work() -> None:
            data: T | None = None
            error: BaseException | None = None
            try:
                data = func(*args)
            except BaseException as problem:  # noqa: BLE001 - handed to then(), never lost (rule 22)
                error = problem
            self.call_from_worker(self._fetch_done, token, data, error)

        try:
            threading.Thread(target=work, daemon=True, name=f"orbital-call-{token}").start()
        except RuntimeError as problem:  # no more threads
            self._attempt(_name(then), then, None, problem)
            return
        deadline = self.set_timer(timeout, lambda: self._fetch_late(token, timeout), name=f"call-{token}")
        self._orbital_waiting[token] = (then, deadline)

    def _fetch_done(self, token: int, data: Any, error: BaseException | None) -> None:
        waiting = self._orbital_waiting.pop(token, None)
        if waiting is None or self._orbital_closed:  # past its deadline: dropped
            return
        then, deadline = waiting
        deadline.stop()
        self._attempt(_name(then), then, data, error)

    def _fetch_late(self, token: int, timeout: float) -> None:
        waiting = self._orbital_waiting.pop(token, None)
        if waiting is not None and not self._orbital_closed:
            then, _ = waiting
            self._attempt(_name(then), then, None, TimeoutError(f"timed out after {timeout:g} s"))

    # -- publishing ----------------------------------------------------------------------

    def _publish(self, at: datetime) -> None:
        """After every fetch: a snapshot taken at ``at``; the pages get it if the
        content changed; then health, alerts and the badge. Each step is guarded
        on its own, so one that raises doesn't skip the others (rule 22)."""
        try:
            snapshot: Snapshot | None = self.store.snapshot(at)
        except Exception as error:  # noqa: BLE001 - reported; health and the badge still update
            self.report_error("Store.snapshot", error)
            snapshot = None
        if snapshot is not None:
            changed = self.store.content_version != self._orbital_published
            self._orbital_published = self.store.content_version
            self.snapshot = snapshot  # re-stamped on every publish: taken_at is this fetch's time
            if changed:
                self._deliver(snapshot)
            self._attempt("health", self._check_health, snapshot.sources, at)
            if self.alerts_enabled:
                self._attempt("rules", self._evaluate_alerts, snapshot)
        else:
            self._attempt("health", self._check_health, self.store.statuses(), at)
        self._attempt("badge", self._update_badge)

    def _deliver(self, snapshot: Snapshot) -> None:
        self._orbital_day = snapshot.taken_at.astimezone().date()
        self._attempt("snapshot_changed", self.snapshot_changed, snapshot)

    def _republish(self, at: datetime) -> None:
        """The same content, taken now, to the pages (snoozing, a new day)."""
        if self.snapshot is not None:
            self.snapshot = replace(self.snapshot, taken_at=at, sources=self.store.statuses())
            self._deliver(self.snapshot)

    def _check_health(self, statuses: tuple[SourceStatus, ...], at: datetime) -> None:
        health = tuple(s.health(at) for s in statuses)
        if health != self._orbital_health:
            self._orbital_health = health
            self.health_changed(statuses, at)

    def _each_widget(self, method: str, *args: Any) -> None:
        for screen in self.screen_stack:
            for widget in screen.walk_children(with_self=True):
                found = getattr(widget, method, None)
                if callable(found):
                    self._attempt(f"{type(widget).__name__}.{method}", found, *args)

    # -- hooks -------------------------------------------------------------------------------

    def snapshot_changed(self, snapshot: Snapshot) -> None:
        """New data, items snoozed or woken, or a new day. By default every
        widget with ``update_snapshot`` gets it (one that raises is reported and
        the others still get it)."""
        self._each_widget("update_snapshot", snapshot)

    def health_changed(self, statuses: tuple[SourceStatus, ...], at: datetime) -> None:
        """A source went fresh, stale or failing. By default every widget with
        ``set_statuses`` gets the statuses."""
        self._each_widget("set_statuses", statuses, at)

    def minute_tick(self, at: datetime) -> None:
        """On every minute boundary: ages and timers move on. By default every
        widget with ``minute_tick`` gets it."""
        self._each_widget("minute_tick", at)

    def badge_changed(self, text: str | None, waiting: int) -> None:
        """The attention badge changed ("◉ 2 waiting", or None); for a header."""

    def show_detail(self, record: Any) -> None:
        """Show a record (an entity, track item or attention item) in detail:
        what ``jump_to`` ends in. Override it; by default it does nothing."""

    def jump_to(self, target: tuple[str, str] | None) -> None:
        """Enter on an alert: find the record ``target`` names (``("entity", id)``,
        ``("track", key)`` or ``("item", key)``) and :meth:`show_detail` it."""
        if target is None or self.snapshot is None:
            return
        kind, key = target
        lookups: dict[str, Callable[[str], Any]] = {
            "entity": self.snapshot.entity, "track": self.snapshot.track_item, "item": self.snapshot.item}
        lookup = lookups.get(kind)
        record = lookup(key) if lookup is not None else None
        if record is not None:
            self.show_detail(record)

    # -- time ------------------------------------------------------------------------------------

    def _arm_minute(self) -> None:
        t = datetime.now()
        self._orbital_minute = self.set_timer(60.5 - t.second - t.microsecond / 1e6, self._on_minute,
                                              name="minute")

    def _on_minute(self) -> None:
        try:
            at = now()
            self._attempt("snooze_expire", self.snooze_expire, at)
            if self.snapshot is not None and at.astimezone().date() != self._orbital_day:
                self._republish(at)  # a new day: Snapshot.week() rolls over on Monday
            self._attempt("alert snoozes", self._wake_alerts, at)
            self._attempt("health", self._check_health, self.store.statuses(), at)
            self._attempt("minute_tick", self.minute_tick, at)
        finally:
            self._arm_minute()

    def _wake_alerts(self, at: datetime) -> None:
        if self.alert_queue.wake(at):
            self._save_snoozes()
            self._show_next_alert()

    # -- snoozing items -------------------------------------------------------------------------

    def snooze_item(self, key: str, duration: timedelta | None = None) -> None:
        """Hide an attention item (by key) for ``duration`` (default ``ITEM_SNOOZE``)."""
        at = now()
        self.snoozed[key] = at + (duration or self.ITEM_SNOOZE)
        self._save_snoozes()
        self._republish(at)

    def unsnooze(self, key: str) -> bool:
        """Bring back one snoozed item (by key); False if it wasn't snoozed."""
        if self.snoozed.pop(key, None) is None:
            return False
        self._save_snoozes()
        self._republish(now())
        return True

    def wake_items(self) -> int:
        """Bring back every snoozed item; returns how many."""
        count = len(self.snoozed)
        self.snoozed.clear()
        if count:
            self._save_snoozes()
            self._republish(now())
        return count

    def snooze_expire(self, at: datetime) -> None:
        due = [k for k, until in self.snoozed.items() if until <= at]
        for key in due:
            del self.snoozed[key]
        if due:
            self._save_snoozes()
            self._republish(at)

    def visible_items(self, items: Sequence[AttentionItem]) -> list[AttentionItem]:
        """``items`` without the snoozed ones."""
        return [i for i in items if i.key not in self.snoozed]

    # -- the snooze file --------------------------------------------------------------------------

    def _load_snoozes(self) -> None:
        """Read the snooze file, if there is one, and keep the snoozes that
        haven't ended. One that can't be read or parsed is reported and treated
        as empty: it never stops startup (rule 25)."""
        path = self.snooze_file
        if path is None:
            return
        self._orbital_saved = self._snooze_text()  # a missing or broken file is only written after a change
        try:
            text = path.read_text(encoding="utf-8")
            items, alerts = _read_snoozes(text)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError) as error:  # unreadable, not JSON, or not ours
            self.report_error(f"snooze file {path}", error)
            return
        self._orbital_saved = text
        at = now()
        self.snoozed.update({key: until for key, until in items.items() if until > at})
        self.alert_queue.restore({key: until for key, until in alerts.items() if until > at})
        self._save_snoozes()  # without the snoozes that ended while the app was closed

    def _snooze_text(self) -> str:
        state = {"items": {key: until.isoformat() for key, until in sorted(self.snoozed.items())},
                 "alerts": {key: until.isoformat() for key, until in sorted(self.alert_queue.snoozed.items())}}
        return json.dumps(state, indent=1, ensure_ascii=False) + "\n"

    def _save_snoozes(self) -> None:
        """Write the snooze file if the snoozes changed: to a temporary file
        next to it, then ``os.replace``, so a crash never leaves half a file.
        A failed write is reported and tried again at the next change."""
        path = self.snooze_file
        if path is None:
            return
        text = self._snooze_text()
        if text == self._orbital_saved:
            return
        self._orbital_saved = text  # also after a failure: tried again at the next change, not every fetch
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
        except OSError as error:
            self.report_error(f"snooze file {path}", error)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    # -- alerts ----------------------------------------------------------------------------------

    def _load_images(self, paths: Sequence[str]) -> None:
        """Decode alert pictures in a thread; the next alert shows when they are in."""
        paths = [p for p in paths if p not in self.alert_images and p not in self._orbital_loading]
        if not paths:
            return
        self._orbital_loading.update(paths)

        def work() -> None:
            loaded: dict[str, LoadedImage | None] = {}
            errors = []
            for path in paths:
                try:
                    loaded[path] = load_image(path, max_size=(512, 512), max_frames=240)
                except Exception as error:  # noqa: BLE001 - a bad picture falls back to the drawn beacon
                    loaded[path] = None
                    errors.append(f"{path}: {error}")
            self.call_from_worker(self._images_loaded, loaded, errors)

        threading.Thread(target=work, daemon=True, name="orbital-images").start()

    def _images_loaded(self, loaded: dict[str, LoadedImage | None], errors: list[str]) -> None:
        self._orbital_loading.difference_update(loaded)
        self.alert_images.update(loaded)
        self._show_next_alert()
        for error in errors:
            self.notify(error, title="Alert picture not loaded", severity="warning", timeout=8, markup=False)

    def raise_alert(self, alert: Alert) -> bool:
        """Queue an alert (deduplicated by key) and show it if nothing else is up.
        Returns False for a duplicate."""
        if not self.alert_queue.push(alert):
            return False
        self.notifier.notify(alert)
        self._show_next_alert()
        self._count_queued()
        self._update_badge()
        return True

    def _count_queued(self) -> None:
        """The alert on screen says how many more are waiting."""
        if self._alert_view is not None:
            self._alert_view.set_queued(len(self.alert_queue))

    def _evaluate_alerts(self, snapshot: Snapshot) -> None:
        fresh, live = self.alert_rules.evaluate(snapshot)
        if self.alert_queue.resolve(live):
            self._close_resolved_alert()
        self._save_snoozes()  # resolve may have dropped snoozed alerts
        for alert in fresh:
            self.raise_alert(alert)
        self._show_next_alert()  # a queued alert takes the resolved one's place
        self._count_queued()  # resolve may have dropped queued ones

    def _close_resolved_alert(self) -> None:
        """The alert's condition went away. Forget its view at once, so a second
        publish can't close it again (O10). A popup closes only while it is on
        the screen stack: now if it is on top, else once it is on top again
        (rule 24); a toast closes now."""
        view, self._alert_view = self._alert_view, None
        self.alert_queue.dismiss()
        if isinstance(view, AlertToast) or (view is not None and view in self.screen_stack):
            view.close_resolved()

    def alert_beacon(self, alert: Alert) -> str:
        """The drawn beacon's state for an alert without a picture (``"waiting"``,
        ``"error"``...)."""
        return "waiting"

    @property
    def alert_view(self) -> AlertScreen | AlertToast | None:
        """The alert on screen: its popup or its toast, or None."""
        return self._alert_view

    def finish_alert(self, action: str = "dismiss") -> bool:
        """Act on the alert on screen as the popup's keys do: ``"jump"``,
        ``"open"``, ``"snooze"`` or ``"dismiss"``. Bind your own keys to it for
        toasts, which take none. A popup is acted on only while it is on top
        (rule 24). False when no alert is showing, or its popup is covered."""
        view = self._alert_view
        if isinstance(view, AlertToast):
            view.finish(action)
            return True
        if view is not None and view.is_active:
            view.action_finish(action)
            return True
        return False

    def _show_next_alert(self) -> None:
        if self._alert_view is not None:
            return
        upcoming = self.alert_queue.pending[:1]
        path = upcoming[0].image if upcoming else None
        if path and path not in self.alert_images:
            self._load_images([path])  # it shows once its picture is decoded
            return
        alert = self.alert_queue.pop()
        if alert is None:
            return
        image = self.alert_images.get(alert.image) if alert.image else None
        queued, beacon = len(self.alert_queue), self.alert_beacon(alert)
        if (alert.style or self.alert_style) == "toast":
            toast = self._make_toast(alert, image, queued, beacon, self.toast_timeout)
            self._alert_view = toast
            self._mount_toast(toast)
            return
        screen = self.ALERT_SCREEN(alert, image, queued=queued, beacon=beacon)
        self._alert_view = screen
        self.push_screen(screen, lambda action: self._alert_done(screen, alert, action))

    def _make_toast(self, alert: Alert, image: LoadedImage | None, queued: int, beacon: str,
                    timeout: float) -> AlertToast:
        def done(action: str) -> None:
            self._alert_done(toast, alert, action)

        toast = self.ALERT_TOAST(alert, image, queued=queued, beacon=beacon, timeout=timeout, done=done)
        return toast

    def _mount_toast(self, toast: AlertToast) -> None:
        if not toast.closed and self._alert_view is toast:
            self.screen.mount(toast)

    def _screen_changed(self, screen: Screen[Any]) -> None:
        self._attempt("alert toast", self._move_toast)

    def _move_toast(self) -> None:
        """A popup opened or closed: the toast goes to the screen on top with the
        time it had left (a fresh widget; the old one is withdrawn quietly)."""
        toast = self._alert_view
        if not isinstance(toast, AlertToast) or toast.closed or toast.parent is self.screen:
            return
        moved = self._make_toast(toast.alert, toast.image, toast.queued, toast.beacon, toast.remaining())
        toast.withdraw()
        self._alert_view = moved
        self.call_later(self._mount_toast, moved)  # after the new screen is set up

    def _alert_done(self, view: AlertScreen | AlertToast, alert: Alert, action: str | None) -> None:
        """Every alert view ends here, whatever closed it (rule 26)."""
        if self._orbital_closed or not self.is_running:
            return  # quitting: the toast's widgets are being removed
        if self._alert_view is view:  # else it was resolved, and the queue knows already
            self._alert_view = None
            if action == "snooze":
                self.alert_queue.snooze(self.ALERT_SNOOZE)
                self._save_snoozes()
                self.notify(f"Snoozed for {int(self.ALERT_SNOOZE.total_seconds() // 60)} minutes: {alert.title}",
                            timeout=3, markup=False)
            else:
                self.alert_queue.dismiss()  # a toast's timeout counts as dismissed
        if action == "open":
            self.open_link(alert.url)
        elif action == "jump":
            self.jump_to(alert.target)
        self._update_badge()
        self.call_later(self._show_next_alert)

    # -- attention -------------------------------------------------------------------------------

    def _update_badge(self) -> None:
        entities = self.snapshot.entities if self.snapshot else ()
        waiting = sum(1 for e in entities if e.state is EntityState.WAITING)
        queued = len(self.alert_queue) + (1 if self.alert_queue.active else 0)
        if waiting:
            text: str | None = f"◉ {waiting} waiting"
        elif queued:
            text = f"◉ {queued} alert{'s' if queued != 1 else ''}"
        else:
            text = None
        self.notifier.badge(text)
        self.badge_changed(text, waiting)

    def open_link(self, url: str | None) -> None:
        """Open ``url`` in the browser, in a thread (with ``open_urls``, for an
        http or https link to a host in ``open_hosts``), or show it."""
        if not url:
            self.notify("Nothing to open", timeout=2)
            return
        if not self.open_urls:
            self.notify(url, title="Would open", timeout=4, markup=False)
            return
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:
            self.notify(url, title="Link", timeout=4, markup=False)
        elif self.open_hosts is not None and host not in self.open_hosts:
            self.notify(url, title=f"Not opened: {host} is not in open_hosts", severity="warning", timeout=6,
                        markup=False)
        else:
            # webbrowser shells out (``open`` on macOS): never on the UI thread.
            threading.Thread(target=self._browse, args=(url,), daemon=True, name="orbital-browser").start()
            self.notify(url, title="Opening", timeout=3, markup=False)

    def _browse(self, url: str) -> None:
        """In a thread: open ``url`` and report back if no browser could."""
        try:
            opened = webbrowser.open(url)
        except Exception as error:  # noqa: BLE001 - reported on the app thread
            self.call_from_worker(self.report_error, "webbrowser.open", error)
            return
        if not opened:
            self.call_from_worker(lambda: self.notify(url, title="No browser could open it", severity="warning",
                                                      timeout=6, markup=False))
