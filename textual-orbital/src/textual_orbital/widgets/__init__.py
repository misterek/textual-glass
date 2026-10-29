"""The orbital widgets: generic dashboard pieces over :mod:`textual_orbital.model`.

* ``BeaconRow``: entities as terminal-played beacons, by state.
* ``PriorityList``: the needs-attention queue and a foldable be-aware list.
* ``Track``, ``TrackTable``: items moving through named stages.
* ``TaskBoard``, ``EventLog``, ``DoneList``: tasks, a timeline, finished work.
* ``HeaderStrip`` (``Brand``, ``SourceHealth``, ``Clock``), ``KeyBar``.
* ``Page``: one page of a dashboard.
* ``OrbitalModal``, ``AlertScreen``, ``DetailScreen``, ``HelpScreen``, ``SourcesScreen``: popups.
* ``AlertToast``: an alert as a corner toast.
* ``WeekReplay``: the week in review as a space show, held as its summary.

The generic instruments (``GlassPanel``, ``RingGauge``, ``StatTile``,
``ProgressArc``, ``InfoCard``...) come from ``textual_glass.widgets``.
"""

from .beacons import STATE_COLORS, STATE_GLYPHS, BeaconRow, order_entities, state_text
from .header import HEALTH_COLORS as SOURCE_HEALTH_COLORS
from .header import HEALTH_GLYPHS as SOURCE_HEALTH_GLYPHS
from .header import Brand, Clock, HeaderStrip, KeyBar, SourceHealth
from .lists import TASK_COLORS, DoneList, EventLog, PriorityList, Section, TaskBoard, signal_glyphs, task_color
from .page import Page
from .replay import SHIP_CLASSES, SHIPS, ReplayWeek, WeekReplay, week_hours
from .screens import (
    LEGEND,
    REPLAY_LEGEND,
    AlertScreen,
    AlertToast,
    DetailScreen,
    HelpScreen,
    OrbitalModal,
    SourcesScreen,
    beacon_view,
    hint,
)
from .track import HEALTH_COLORS, HEALTH_WORDS, Track, TrackTable, status_word

__all__ = [
    "HEALTH_COLORS", "HEALTH_WORDS", "LEGEND", "REPLAY_LEGEND", "SHIPS", "SHIP_CLASSES", "SOURCE_HEALTH_COLORS",
    "SOURCE_HEALTH_GLYPHS", "STATE_COLORS", "STATE_GLYPHS", "TASK_COLORS", "AlertScreen", "AlertToast", "BeaconRow",
    "Brand", "Clock", "DetailScreen", "DoneList", "EventLog", "HeaderStrip", "HelpScreen", "KeyBar", "OrbitalModal",
    "Page", "PriorityList", "ReplayWeek", "Section", "SourceHealth", "SourcesScreen", "TaskBoard", "Track",
    "TrackTable", "WeekReplay", "beacon_view", "hint", "order_entities", "signal_glyphs", "state_text",
    "status_word", "task_color", "week_hours",
]
