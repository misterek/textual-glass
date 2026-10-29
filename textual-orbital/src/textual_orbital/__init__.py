"""Orbital dashboard widgets for Textual, built on textual-glass.

* :class:`OrbitalApp` (:mod:`textual_orbital.app`): polls a provider, raises
  alerts, notifies, badges the window title.
* The widgets (:mod:`textual_orbital.widgets`): beacons, the priority queue,
  the track, tables and lists, the header and key bar, pages and popups, and
  the week in review (``WeekReplay``).
* The data layer: generic records (:mod:`textual_orbital.model`), the source
  seam (:mod:`textual_orbital.provider`), alert rules and the queue
  (:mod:`textual_orbital.alerts`), notifications (:mod:`textual_orbital.notify`)
  and the Orbital theme (:mod:`textual_orbital.theme`).
"""

from .alerts import AlertQueue, AlertRules, Condition, Rule, WaitingRule, alert_color
from .app import OrbitalApp
from .model import (
    ALERT_STYLES,
    ENTITY_URGENCY,
    Alert,
    AttentionItem,
    DayCount,
    DoneTask,
    Entity,
    EntityState,
    ErrorReport,
    Event,
    Goal,
    Health,
    Metrics,
    Snapshot,
    SourceStatus,
    Task,
    TrackHealth,
    TrackItem,
    WeekStats,
    queue_load,
    week_stats,
)
from .notify import Notifier
from .provider import (
    Provider,
    Source,
    SourceData,
    StaticProvider,
    Store,
    fetch_timeout,
    poll_delay,
    timed_fetch,
)
from .theme import (
    ORBITAL,
    ORBITAL_NAVY,
    PALETTE,
    SOURCE_COLORS,
    SOURCE_NAMES,
    WHY_COLORS,
    ellipsize,
    fmt_age,
    fmt_interval,
    health_gradient,
    load_color,
    now,
    source_color,
    urgency_color,
    why_color,
)
from .widgets import (
    AlertScreen,
    AlertToast,
    BeaconRow,
    Brand,
    Clock,
    DetailScreen,
    DoneList,
    EventLog,
    HeaderStrip,
    HelpScreen,
    KeyBar,
    OrbitalModal,
    Page,
    PriorityList,
    SourceHealth,
    SourcesScreen,
    TaskBoard,
    Track,
    TrackTable,
    WeekReplay,
)

__version__ = "0.3.0"

__all__ = [
    "AlertScreen", "AlertToast", "BeaconRow", "Brand", "Clock", "DetailScreen", "DoneList", "EventLog", "HeaderStrip",
    "HelpScreen", "KeyBar", "OrbitalApp", "OrbitalModal", "Page", "PriorityList", "SourceHealth", "SourcesScreen",
    "TaskBoard", "Track", "TrackTable", "WeekReplay",
    "ALERT_STYLES", "ENTITY_URGENCY", "ORBITAL", "ORBITAL_NAVY", "PALETTE", "SOURCE_COLORS", "SOURCE_NAMES",
    "WHY_COLORS",
    "Alert", "AlertQueue", "AlertRules", "AttentionItem", "Condition", "DayCount", "DoneTask", "Entity",
    "EntityState", "ErrorReport", "Event", "Goal", "Health", "Metrics", "Notifier", "Provider", "Rule", "Snapshot",
    "Source", "SourceData", "SourceStatus", "StaticProvider", "Store", "Task", "TrackHealth", "TrackItem",
    "WaitingRule", "WeekStats",
    "alert_color", "ellipsize", "fetch_timeout", "fmt_age", "fmt_interval", "health_gradient", "load_color", "now",
    "poll_delay", "queue_load", "source_color", "timed_fetch", "urgency_color", "week_stats", "why_color",
]
