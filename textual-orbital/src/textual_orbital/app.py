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

Cost model: each source is fetched in a worker thread at its own interval. A
fetch whose data equals the last one changes nothing and sends nothing; the
UI only redraws on real changes and once a minute for ages.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import webbrowser
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from textual import events, on
from textual.app import App

from textual_glass import Glass, LoadedImage, load_image, terminal_write

from .alerts import AlertQueue, AlertRules, Rule, WaitingRule
from .model import Alert, AttentionItem, EntityState, Health, Snapshot, SourceStatus
from .notify import Notifier
from .provider import Provider, Source, SourceData, Store, timed_fetch
from .theme import ORBITAL, ORBITAL_NAVY, now
from .widgets.screens import AlertScreen

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


class OrbitalApp(Glass, App[None]):
    """Polls ``provider`` and keeps ``self.snapshot`` current.

    Args:
        provider: Where the data comes from (see :mod:`textual_orbital.provider`).
        rules: The alert rules (see :mod:`textual_orbital.alerts`).
        alert_images: A picture or GIF per alert kind (paths), e.g. ``{"waiting": "~/art.gif"}``.
        alerts: Raise "incoming transmission" popups.
        notify: Desktop notifications: ``"auto"``, ``"osc777"``, ``"osc9"`` or ``"off"``.
        bell: Ring the bell with each alert (Ghostty bounces the dock icon).
        title_badge: Put "◉ 2 waiting" in the window title.
        open_urls: ``open_link`` opens the browser. False (the default) just shows the link; pass True
            in a real dashboard.

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
    #: The popup class for alerts.
    ALERT_SCREEN: ClassVar[type[AlertScreen]] = AlertScreen

    def __init__(self, provider: Provider, *, rules: Sequence[Rule] = (WaitingRule(),),
                 alert_images: dict[str, str] | None = None, alerts: bool = True, notify: str = "auto",
                 bell: bool = True, title_badge: bool = True, open_urls: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.provider = provider
        self.store = Store(provider.sources())
        self.snapshot: Snapshot | None = None
        self.alerts_enabled = alerts
        self.alert_queue = AlertQueue()
        images = {kind: str(Path(path).expanduser()) for kind, path in (alert_images or {}).items()}
        self.alert_rules = AlertRules(rules, images)
        self.alert_images: dict[str, LoadedImage | None] = {}
        self.notifier = Notifier(self, lambda data: terminal_write(self, data), mode=notify, bell=bell,
                                 title=title_badge)
        self.open_urls = open_urls
        self.snoozed: dict[str, datetime] = {}
        self.fetches = 0
        self._orbital_published = -1
        self._orbital_health: tuple[Health, ...] = ()
        self._orbital_loop: asyncio.AbstractEventLoop | None = None
        self._orbital_context = contextvars.copy_context()
        self._orbital_pool: ThreadPoolExecutor | None = None
        self._orbital_in_flight: set[str] = set()
        self._orbital_loading: set[str] = set()
        self._alert_screen: AlertScreen | None = None
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
        self._orbital_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="orbital-fetch")
        self.notifier.badge("")  # the window title says what this window is
        self._load_images(sorted(set(self.alert_rules.images.values())))
        for source in self.store.sources:
            self._poll(source)
            self.set_interval(max(0.25, float(source.interval)), self._poller(source), name=f"poll-{source.key}")
        self._arm_minute()

    def exit(self, *args: Any, **kwargs: Any) -> None:
        self.notifier.restore()
        if self._orbital_pool is not None:
            self._orbital_pool.shutdown(wait=False, cancel_futures=True)
        super().exit(*args, **kwargs)

    def call_from_worker(self, callback: Callable[..., Any], *args: Any) -> None:
        """Run ``callback(*args)`` on the app's loop, in the app's context, from
        any thread (rule 13). Does nothing once the app has stopped."""
        loop = self._orbital_loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(callback, *args, context=self._orbital_context.copy())
        except RuntimeError:  # the loop has closed
            pass

    def _poller(self, source: Source) -> Callable[[], None]:
        return lambda: self._poll(source)

    # -- polling -------------------------------------------------------------------------

    def poll_all(self) -> None:
        """Fetch every source now."""
        for source in self.store.sources:
            self._poll(source)

    def _poll(self, source: Source) -> None:
        pool = self._orbital_pool
        if pool is None or source.key in self._orbital_in_flight:
            return
        self._orbital_in_flight.add(source.key)

        def work() -> None:
            data, error, ms = timed_fetch(source)
            self.call_from_worker(self._fetched, source.key, data, error, ms)

        try:
            pool.submit(work)
        except RuntimeError:  # shutting down
            self._orbital_in_flight.discard(source.key)

    def _fetched(self, key: str, data: SourceData | None, error: str | None, ms: float) -> None:
        self._orbital_in_flight.discard(key)
        self.fetches += 1
        at = now()
        if data is not None:
            self.store.apply(key, data, at, ms)
        else:
            self.store.fail(key, error or "failed", at)
        self._publish(at)

    def _publish(self, at: datetime) -> None:
        snapshot = self.store.snapshot(at)
        if self.store.content_version != self._orbital_published:
            self._orbital_published = self.store.content_version
            self.snapshot = snapshot
            self.snapshot_changed(snapshot)
        elif self.snapshot is not None:
            self.snapshot = replace(self.snapshot, sources=snapshot.sources)
        self._check_health(snapshot.sources, at)
        if self.alerts_enabled:
            self._evaluate_alerts(snapshot)
        self._update_badge()

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
                    found(*args)

    # -- hooks -------------------------------------------------------------------------------

    def snapshot_changed(self, snapshot: Snapshot) -> None:
        """New data (or snoozing changed). By default every widget with
        ``update_snapshot`` gets it."""
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
        self.set_timer(60.5 - t.second - t.microsecond / 1e6, self._on_minute, name="minute")

    def _on_minute(self) -> None:
        at = now()
        self.snooze_expire(at)
        if self.alert_queue.wake(at):
            self._show_next_alert()
        self._check_health(self.store.statuses(), at)
        self.minute_tick(at)
        self._arm_minute()

    # -- snoozing items -------------------------------------------------------------------------

    def snooze_item(self, key: str, duration: timedelta | None = None) -> None:
        """Hide an attention item (by key) for ``duration`` (default ``ITEM_SNOOZE``)."""
        self.snoozed[key] = now() + (duration or self.ITEM_SNOOZE)
        if self.snapshot is not None:
            self.snapshot_changed(self.snapshot)

    def wake_items(self) -> int:
        """Bring back every snoozed item; returns how many."""
        count = len(self.snoozed)
        self.snoozed.clear()
        if count and self.snapshot is not None:
            self.snapshot_changed(self.snapshot)
        return count

    def snooze_expire(self, at: datetime) -> None:
        due = [k for k, until in self.snoozed.items() if until <= at]
        for key in due:
            del self.snoozed[key]
        if due and self.snapshot is not None:
            self.snapshot_changed(self.snapshot)

    def visible_items(self, items: Sequence[AttentionItem]) -> list[AttentionItem]:
        """``items`` without the snoozed ones."""
        return [i for i in items if i.key not in self.snoozed]

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
            self.notify(error, title="Alert picture not loaded", severity="warning", timeout=8)

    def raise_alert(self, alert: Alert) -> bool:
        """Queue an alert (deduplicated by key) and show it if nothing else is up.
        Returns False for a duplicate."""
        if not self.alert_queue.push(alert):
            return False
        self.notifier.notify(alert)
        self._show_next_alert()
        self._update_badge()
        return True

    def _evaluate_alerts(self, snapshot: Snapshot) -> None:
        fresh, live = self.alert_rules.evaluate(snapshot)
        if self.alert_queue.resolve(live) and self._alert_screen is not None:
            self._alert_screen.dismiss("resolved")
        for alert in fresh:
            self.raise_alert(alert)

    def alert_beacon(self, alert: Alert) -> str:
        """The drawn beacon's state for an alert without a picture (``"waiting"``,
        ``"error"``...)."""
        return "waiting"

    def _show_next_alert(self) -> None:
        if self._alert_screen is not None:
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
        screen = self.ALERT_SCREEN(alert, image, queued=len(self.alert_queue), beacon=self.alert_beacon(alert))
        self._alert_screen = screen
        self.push_screen(screen, lambda action: self._alert_done(alert, action))

    def _alert_done(self, alert: Alert, action: str | None) -> None:
        self._alert_screen = None
        if action == "snooze":
            self.alert_queue.snooze(self.ALERT_SNOOZE)
            self.notify(f"Snoozed for {int(self.ALERT_SNOOZE.total_seconds() // 60)} minutes: {alert.title}",
                        timeout=3)
        else:
            self.alert_queue.dismiss()
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
        """Open ``url`` in the browser (with ``open_urls``), or show it."""
        if not url:
            self.notify("Nothing to open", timeout=2)
            return
        if self.open_urls and url.startswith(("http://", "https://")):
            webbrowser.open(url)
            self.notify(url, title="Opening", timeout=3)
        else:
            self.notify(url, title="Link" if self.open_urls else "Would open", timeout=4)
