"""Boards: panels that agents and scripts add to a folder, shown on an orbital page.

A **panel** is one JSON file (:class:`PanelSpec`): a title, a kind (``timeseries``, ``stat``, ``gauge``, ``bars``,
``table`` or ``text``), and a shell command whose output is read by its format (or inline data). A **board** is a
folder of panels. Anything that can write a file adds a panel (:class:`Store`); the app shows the boards
(:class:`BoardsSource` and :class:`BoardsPage`), runs each command on its schedule (:func:`run`), lays the panels
out, and lets the user arrange or remove them.

* :mod:`~textual_orbital.boards.model`: ``PanelSpec``, ``Frame`` (what a run gave), ``accumulate``, durations.
* :mod:`~textual_orbital.boards.store`: ``Store``, the folder: atomic writes, keys, expiry, arranging.
* :mod:`~textual_orbital.boards.formats`: command output to a ``Frame``; ``guess_kind``, ``describe``.
* :mod:`~textual_orbital.boards.runner`: ``run``: one panel's command, its environment and deadline.
* :mod:`~textual_orbital.boards.source`: ``BoardsSource``, the live panels as an orbital payload.
* :mod:`~textual_orbital.boards.layout`: ``plan``, the page's grid from each panel's size.
* :mod:`~textual_orbital.boards.widgets`: ``PanelView`` and a body per kind.
* :mod:`~textual_orbital.boards.page`: ``BoardsPage``, the full-screen panel and the remove confirmation.

Commands in panel files run with the app's privileges: whoever can write the folder can run commands as you. The
store makes it ``0700``; keep it that way (textual-orbital's README, "Boards").
"""

from .formats import describe, fmt_number, guess_kind, parse, parse_columns, parse_data
from .layout import Need, Plan, need, plan, reposition, step_size
from .model import (
    BOARD_RE,
    DEFAULT_BOARD,
    ID_RE,
    KINDS,
    MAX_SERIES,
    SIZES,
    Frame,
    Kind,
    PanelSpec,
    Series,
    Size,
    SpecError,
    Table,
    accumulate,
    fmt_duration,
    parse_duration,
)
from .page import BoardRow, BoardsPage, ConfirmRemove, EmptyState, PanelScreen
from .runner import PanelError, panel_env, run, run_command, run_data
from .source import BoardsSource
from .store import Store, default_root
from .widgets import BODIES, PanelView, fmt_value, status_colour

__all__ = [
    "BOARD_RE", "BODIES", "DEFAULT_BOARD", "ID_RE", "KINDS", "MAX_SERIES", "SIZES", "BoardRow", "BoardsPage",
    "BoardsSource", "ConfirmRemove", "EmptyState", "Frame", "Kind", "Need", "PanelError", "PanelScreen", "PanelSpec",
    "PanelView", "Plan", "Series", "Size", "SpecError", "Store", "Table", "accumulate", "default_root", "describe",
    "fmt_duration", "fmt_number", "fmt_value", "guess_kind", "need", "panel_env", "parse", "parse_columns",
    "parse_data", "parse_duration", "plan", "reposition", "run", "run_command", "run_data", "status_colour",
    "step_size",
]
