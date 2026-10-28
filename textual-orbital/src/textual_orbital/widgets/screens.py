"""Popups: the incoming-transmission alert, detail views, help and source health.

All of them are ``ModalScreen``\\ s. With glass, the screen below stays visible:
a scrim dims its glass, glass's line filter dims its text, and the dialog is a
:class:`~textual_glass.widgets.GlassPanel` drawn on its own layer. Without
glass they are ordinary Textual modals over a tinted screen.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Static

from textual_glass import GlassImage, GlassPanel, LoadedImage, SpriteView
from textual_glass.sprites import beacon_frames

from ..alerts import alert_color
from ..model import Alert, ErrorReport, SourceStatus
from ..theme import PALETTE, SOURCE_NAMES, ellipsize, fmt_age, fmt_interval, now
from .header import HEALTH_COLORS, HEALTH_GLYPHS


class OrbitalModal(ModalScreen[Any]):
    """Base for the popups: centred, a hint line docked at the bottom of the dialog."""

    DEFAULT_CSS = """
    OrbitalModal { align: center middle; background: $background 50%; }
    OrbitalModal .dialog-hint { height: 1; dock: bottom; color: $foreground 50%; }
    """


def spaced(text: str) -> str:
    """Letter-spaced capitals ("N E E D S  Y O U")."""
    return " ".join(text.upper())


def hint(*pairs: tuple[str, str]) -> Text:
    """Key hints: ``hint(("esc", "close"))``."""
    out = Text()
    for key, label in pairs:
        out.append(key, style=f"bold {PALETTE['accent']}")
        out.append(f" {label}   ", style=PALETTE["text_dim"])
    return out


def beacon_view(state: str, colour: str, glyph: str = "◉", **kwargs: Any) -> SpriteView:
    """A big beacon (``state``: waiting, error, working, idle, done) as a sprite,
    a glyph without glass."""
    return SpriteView(("beacon", state, colour), lambda w, h: beacon_frames(w, h, state, colour),
                      fallback=glyph, fallback_color=colour, **kwargs)


# ===================================================================== alert ==


class AlertScreen(OrbitalModal):
    """The big popup for an :class:`~textual_orbital.model.Alert`.

    Dismisses with the action taken: ``"jump"`` (enter), ``"open"`` (o, only with a ``url``),
    ``"snooze"`` (s) or ``"dismiss"`` (escape), or with ``"resolved"`` when the app
    closes it (:meth:`close_resolved`). The hint line offers ``o`` only when the
    alert has a ``url``.

    Args:
        alert: What to show.
        image: A decoded picture or animation (``textual_glass.load_image``), or
            None for the drawn beacon.
        queued: How many more alerts are waiting (shown under the text).
        beacon: The drawn beacon's state when there is no picture
            (``"waiting"``, ``"error"``...).
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q", "finish('dismiss')", "dismiss", show=False),
        Binding("enter", "finish('jump')", "jump", show=False),
        Binding("o", "finish('open')", "open", show=False),
        Binding("s", "finish('snooze')", "snooze", show=False),
    ]

    DEFAULT_CSS = """
    AlertScreen #alert-dialog { width: 78; height: 18; }
    AlertScreen #alert-body { height: 1fr; }
    AlertScreen #alert-picture { width: 26; height: 1fr; margin: 0 2 0 0; }
    AlertScreen #alert-text { width: 1fr; height: 1fr; }
    AlertScreen #alert-kind { height: 2; }
    AlertScreen #alert-title { height: auto; max-height: 3; margin: 0 0 1 0; }
    AlertScreen #alert-lines { height: 1fr; }
    AlertScreen #alert-meta { height: 1; }
    AlertScreen.-narrow #alert-dialog { width: 64; height: 16; }
    AlertScreen.-narrow #alert-picture { width: 18; }
    AlertScreen.-wide #alert-dialog { width: 104; height: 26; }
    AlertScreen.-wide #alert-picture { width: 44; }
    AlertScreen.-short #alert-dialog { height: 14; }
    """

    #: The snooze length shown in the hint ("snooze 10m").
    SNOOZE_HINT: ClassVar[str] = "snooze 10m"

    def __init__(self, alert: Alert, image: LoadedImage | None = None, queued: int = 0,
                 beacon: str = "waiting") -> None:
        super().__init__()
        self.alert = alert
        self.image = image
        self.queued = queued
        self.beacon = beacon
        self.raised = alert.raised_at or now()
        self.resolved = False  # close as soon as this popup is on top again

    def picture(self) -> Widget:
        if self.image is not None:
            return GlassImage.from_image(self.image, id="alert-picture", cache_key=f"alert:{self.alert.image}")
        return beacon_view(self.beacon, alert_color(self.alert), "✕" if self.beacon == "error" else "◉",
                           id="alert-picture")

    def compose(self) -> ComposeResult:
        colour = alert_color(self.alert)
        with GlassPanel(title="◉ INCOMING TRANSMISSION", accent=colour, accent_2=PALETTE["accent_2"], dialog=True,
                        id="alert-dialog"):
            with Horizontal(id="alert-body"):
                yield self.picture()
                with Vertical(id="alert-text"):
                    yield Static(id="alert-kind")
                    yield Static(Text(self.alert.title, style=f"bold {PALETTE['text_bright']}"), id="alert-title")
                    yield Static(self._lines(), id="alert-lines")
                    yield Static(id="alert-meta")
            pairs = [("⏎", "jump to it"), ("o", "open"), ("s", self.SNOOZE_HINT), ("esc", "dismiss")]
            yield Static(hint(*(p for p in pairs if p[0] != "o" or self.alert.url)), classes="dialog-hint")

    def on_mount(self) -> None:
        self._update_kind()
        self._update_meta()

    def minute_tick(self, at: datetime) -> None:
        """Called by ``OrbitalApp`` on the minute: "received 3m ago" moves on."""
        self._update_meta()

    def on_resize(self) -> None:
        self._update_kind()

    def _update_kind(self) -> None:
        heading = self.alert.heading
        kind = self.query_one("#alert-kind", Static)
        text = spaced(heading) if len(spaced(heading)) <= (kind.size.width or 40) else heading.upper()
        kind.update(Text(text, style=f"bold {alert_color(self.alert)}"))

    def _lines(self) -> Text:
        out = Text()
        for i, line in enumerate(self.alert.body):
            out.append(line + "\n", style=PALETTE["text"] if i == 0 else PALETTE["text_soft"])
        return out

    def _update_meta(self) -> None:
        meta = Text(f"received {fmt_age(now() - self.raised)} ago", style=PALETTE["text_dim"])
        if self.queued:
            meta.append(f"  ·  {self.queued} more waiting", style=f"bold {PALETTE['amber']}")
        self.query_one("#alert-meta", Static).update(meta)

    def action_finish(self, action: str) -> None:
        if action == "open" and not self.alert.url:
            return  # nothing to open: keep the alert up, as the hint offers no "o"
        self.dismiss(action)

    def close_resolved(self) -> None:
        """The alert's condition went away: dismiss with ``"resolved"`` now if
        this popup is on top, or as soon as it is again. (Textual's ``dismiss``
        on a screen that is not on top pops the one that is.)"""
        if self.is_active:
            self.dismiss("resolved")
        else:
            self.resolved = True

    def on_screen_resume(self) -> None:
        if self.resolved:
            self.resolved = False
            self.dismiss("resolved")


# ==================================================================== detail ==


class DetailScreen(OrbitalModal):
    """A large popup for one record: a title, a subtitle, label/value fields, an
    optional visual (any widget) and a body text. ``o`` opens ``url``.

    Dismisses with ``"open"`` (o) or None (escape).
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,backspace,q", "close", "back", show=False),
        Binding("o", "open", "open", show=False),
    ]

    DEFAULT_CSS = """
    DetailScreen #detail { width: 92%; height: 90%; max-width: 120; }
    DetailScreen #detail-head { height: auto; margin: 0 0 1 0; }
    DetailScreen #detail-main { height: 1fr; }
    DetailScreen #detail-fields { width: 1fr; height: 1fr; }
    DetailScreen #detail-visual { width: 1fr; height: 1fr; margin: 0 0 0 2; }
    DetailScreen.-narrow #detail-visual { display: none; }
    DetailScreen #detail-body { height: auto; max-height: 8; margin: 1 0 0 0; }
    """

    def __init__(self, title: str, subtitle: Text | str = "", fields: Sequence[tuple[str, Text | str]] = (),
                 body: str = "", visual: Widget | None = None, url: str | None = None,
                 accent: str = PALETTE["accent"], kind: str = "DETAIL") -> None:
        super().__init__()
        self.title_text = title
        self.subtitle = subtitle
        self.fields = list(fields)
        self.body = body
        self.visual = visual
        self.url = url
        self.accent = accent
        self.kind = kind

    def compose(self) -> ComposeResult:
        head = Text(self.title_text, style=f"bold {PALETTE['text_bright']}")
        if self.subtitle:
            head.append("\n")
            head.append_text(self.subtitle if isinstance(self.subtitle, Text)
                             else Text(self.subtitle, style=PALETTE["text_soft"]))
        fields = Text()
        label_w = max((len(k) for k, _ in self.fields), default=0) + 2
        for key, value in self.fields:
            fields.append(f"{key.upper():<{label_w}}", style=PALETTE["text_faint"])
            fields.append_text(value if isinstance(value, Text) else Text(str(value), style=PALETTE["text"]))
            fields.append("\n")
        with GlassPanel(title=f"◆ {self.kind}", accent=self.accent, accent_2=PALETTE["accent_2"], dialog=True,
                        id="detail"):
            yield Static(head, id="detail-head")
            with Horizontal(id="detail-main"):
                yield Static(fields, id="detail-fields")
                if self.visual is not None:
                    with Vertical(id="detail-visual"):
                        yield self.visual
            if self.body:
                yield Static(Text(self.body, style=PALETTE["text_soft"]), id="detail-body")
            pairs = [("esc", "back")]
            if self.url:
                pairs.insert(0, ("o", "open in browser"))
            yield Static(hint(*pairs), classes="dialog-hint")

    def action_close(self) -> None:
        self.dismiss(None)

    def action_open(self) -> None:
        if self.url:
            self.dismiss("open")


# ====================================================================== help ==

#: What every orbital visual means: (glyph, colour, text).
LEGEND: list[tuple[str, str, str]] = [
    ("◉", PALETTE["amber"], "beacon waiting: pulses, timer = how long"),
    ("◌", PALETTE["accent"], "beacon working: a satellite orbits"),
    ("✕", PALETTE["red"], "beacon errored"),
    ("○", PALETTE["slate"], "beacon idle"),
    ("▂▃▅▆█", PALETTE["orange"], "score, 2 points a bar: red 9+ orange 7+"),
    ("●", PALETTE["red"], "track: failing"),
    ("●", PALETTE["magenta"], "track: needs work"),
    ("●", PALETTE["amber"], "track: pending"),
    ("●", PALETTE["green"], "track: clear"),
    ("○", PALETTE["grey"], "track: parked"),
    ("╌●", PALETTE["text_soft"], "trail: time in stage (2 weeks = full)"),
    ("◉", PALETTE["red"], "pulsing dot: stuck (failing or 2+ days)"),
    ("●", PALETTE["green"], "source: green fresh, amber stale"),
    ("✕", PALETTE["red"], "source failing (h shows why)"),
]


class HelpScreen(OrbitalModal):
    """Keys and a legend for every visual.

    Args:
        keys: ``(key, what it does)`` pairs.
        legend: ``(glyph, colour, meaning)`` rows (default :data:`LEGEND`).
    """

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,question_mark,q,enter", "close", show=False)]

    DEFAULT_CSS = """
    HelpScreen #help { width: 86; height: auto; max-height: 95%; }
    HelpScreen.-narrow #help { width: 98%; }
    HelpScreen #help-keys { width: 32; height: auto; }
    HelpScreen #help-legend { width: 1fr; height: auto; }
    HelpScreen.-narrow #help-keys { width: 26; }
    HelpScreen #help-columns { height: auto; }
    """

    def __init__(self, keys: Sequence[tuple[str, str]], legend: Sequence[tuple[str, str, str]] = LEGEND) -> None:
        super().__init__()
        self.keys = list(keys)
        self.legend = list(legend)

    def compose(self) -> ComposeResult:
        keys = Text()
        for key, label in self.keys:
            keys.append(f"{key:>7}  ", style=f"bold {PALETTE['accent']}")
            keys.append(label + "\n", style=PALETTE["text"])
        legend = Text()
        for glyph, colour, text in self.legend:
            legend.append(f"{glyph:>5}  ", style=f"bold {colour}")
            legend.append(text + "\n", style=PALETTE["text_soft"])
        with GlassPanel(title="? KEYS AND LEGEND", accent=PALETTE["accent"], accent_2=PALETTE["accent_2"],
                        dialog=True, id="help"), Horizontal(id="help-columns"):
            yield Static(keys, id="help-keys")
            yield Static(legend, id="help-legend")

    def action_close(self) -> None:
        self.dismiss(None)


# ============================================================ source health ==


class SourcesScreen(OrbitalModal):
    """Every data source: health, when it last worked, how often it polls, and
    the error if it is failing (with when it is tried again); then the app's
    most recent errors, newest first (pass ``app.errors``).

    Args:
        statuses: The sources' statuses (``app.store.statuses()``).
        at: Now (for the ages).
        errors: What ``OrbitalApp.report_error`` caught (``app.errors``); the
            newest ``ERRORS_SHOWN`` are listed.
    """

    #: How many of ``errors`` are listed.
    ERRORS_SHOWN: ClassVar[int] = 5

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,h,q,enter", "close", show=False)]

    DEFAULT_CSS = """
    SourcesScreen #sources { width: 84; height: auto; max-height: 90%; }
    SourcesScreen.-narrow #sources { width: 96%; }
    """

    def __init__(self, statuses: Sequence[SourceStatus], at: datetime | None = None,
                 errors: Sequence[ErrorReport] = ()) -> None:
        super().__init__()
        self.statuses = list(statuses)
        self.at = at or now()
        self.errors = list(errors)

    def compose(self) -> ComposeResult:
        out = Text()
        for s in self.statuses:
            health = s.health(self.at)
            colour = HEALTH_COLORS[health]
            out.append(f"{HEALTH_GLYPHS[health]} ", style=f"bold {colour}")
            out.append(f"{s.key:<3}", style=f"bold {PALETTE['text']}")
            out.append(f"{ellipsize(s.name or SOURCE_NAMES.get(s.key, s.key), 16):<17}", style=PALETTE["text_soft"])
            out.append(f"{health.value:<8}", style=colour)
            ok = f"ok {fmt_age(self.at - s.last_ok)} ago" if s.last_ok else "never fetched"
            out.append(f"{ok:<16}", style=PALETTE["text_dim"])
            out.append(f"every {fmt_interval(s.interval)}  ", style=PALETTE["text_faint"])
            out.append(f"{s.items} items\n" if s.last_ok else "\n", style=PALETTE["text_faint"])
            if s.error:
                out.append(f"    {s.error}", style=PALETTE["red"])
                if s.next_poll is not None:
                    tries = f"{s.failures} failures" if s.failures > 1 else "1 failure"
                    out.append(f"  · {tries}, next try in {fmt_age(s.next_poll - self.at)}",
                               style=PALETTE["text_faint"])
                out.append("\n")
        if self.errors:
            newest = self.errors[::-1][:self.ERRORS_SHOWN]
            out.append(f"\nAPP ERRORS ({len(self.errors)})\n", style=f"bold {PALETTE['red']}")
            for report in newest:
                out.append(f"  {report.at.astimezone():%H:%M}  ", style=PALETTE["text_dim"])
                out.append(f"{report.where}  ", style=f"bold {PALETTE['text']}")
                out.append(report.message, style=PALETTE["text_soft"])
                out.append(f"  ×{report.count}\n" if report.count > 1 else "\n", style=PALETTE["text_faint"])
        with GlassPanel(title="● SOURCE HEALTH", accent=PALETTE["green"], accent_2=PALETTE["accent_2"], dialog=True,
                        id="sources"):
            yield Static(out)
            yield Static(hint(("esc", "close")), classes="dialog-hint")

    def action_close(self) -> None:
        self.dismiss(None)
