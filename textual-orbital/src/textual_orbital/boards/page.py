"""``BoardsPage``: the boards and their panels, an orbital page.

A row at the top names every board with its panel count (``◆ main 4   incident-db 2``), and the current board's
panels fill the page in a 12-unit grid laid out by :func:`~.layout.plan` from each panel's size: packed in
``order_key`` order, every row full, spare height to the medium and large panels, and more panels than fit scroll.
The user arranges with ``+`` ``-`` (size) and ``<`` ``>`` (order): the page lays out again at once and saves it
through ``arrange`` (into the panel's file, so every viewer agrees); arranging never runs a panel again.

The page runs the panels. Each ``cmd`` panel runs when it appears, then every ``spec.every`` seconds, through
``app.fetch`` (a daemon thread with a deadline: rule 25), one run per panel at a time; a ``data`` panel runs once.
Every panel runs on its schedule, also while another board shows; only the current board's panels have widgets.
Results are folded into each panel's history with ``accumulate`` and kept for as long as the panel lives. A result
for a panel that has gone, or was replaced meanwhile, is dropped.

Keys: ``[`` ``]`` board, arrows or ``tab`` panel, ``enter`` the panel full screen, ``+`` ``-`` grow and shrink it,
``<`` ``>`` move it earlier and later, ``x`` remove it (after a confirmation), ``r`` run it now.
"""

from __future__ import annotations

import bisect
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial
from typing import ClassVar, Protocol, cast

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.strip import Strip
from textual.timer import Timer
from textual.widgets import Static

from textual_glass import PixelContext, PixelWidget
from textual_glass.items import Pill
from textual_glass.render import Item
from textual_glass.widgets import DIM, FAINT, SOFT, GlassPanel, Part, hex_colour, strip, tone

from ..app import OrbitalApp
from ..model import Snapshot
from ..theme import PALETTE
from ..widgets import OrbitalModal, Page, hint
from .layout import UNITS, Need, Plan, need, plan, reposition, step_size
from .model import DEFAULT_BOARD, Frame, PanelSpec, Size, accumulate
from .runner import run
from .widgets import PanelView

__all__ = ["EMPTY_HINT", "EMPTY_TITLE", "Arrange", "BoardRow", "BoardsPage", "ConfirmRemove", "EmptyState",
           "PanelGrid", "PanelScreen", "PanelState"]

#: What the empty page says, and the hint under it (``BoardsPage(empty_hint=...)``). A hint too wide for the page
#: is cut at its ``", or: "``.
EMPTY_TITLE = "No panels yet"
EMPTY_HINT = "ask Claude to add one"


class Arrange(Protocol):
    """Saves a panel's size or place on its board (the store's ``arrange``); None leaves that field as it is."""

    def __call__(self, panel_id: str, *, size: Size | None = None, position: float | None = None) -> object: ...


def describe_error(error: BaseException) -> str:
    """One line for the panel's bottom line."""
    text = str(error).strip()
    first = text.splitlines()[0] if text else ""
    return first or type(error).__name__


# ============================================================================================== the parts ==


class BoardRow(PixelWidget):
    """One line naming every board with its panel count, the current one marked (``◆``, and a pill with glass);
    a board with failing panels says how many (``✕1``)."""

    DEFAULT_CSS = """
    BoardRow { height: 1; padding: 0 1; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.boards: tuple[tuple[str, int, int], ...] = ()
        self.current: str | None = None

    def set_boards(self, boards: Sequence[tuple[str, int, int]], current: str | None) -> None:
        """``(name, panels, failing)`` for each board, and the current board's name."""
        new = (tuple(boards), current)
        if new != (self.boards, self.current):
            self.boards, self.current = new
            self.refresh()

    def _entries(self) -> list[tuple[str, list[Part]]]:
        pal = self.theme_palette
        out: list[tuple[str, list[Part]]] = []
        for name, count, failing in self.boards:
            on = name == self.current
            accent = hex_colour(pal.primary)
            parts: list[Part] = [("◆ " if on else "  ", f"bold {accent}" if on else ""),
                                 (name, f"bold {tone(pal, -0.3)}" if on else tone(pal, SOFT)),
                                 (f" {count}", f"bold {accent}" if on else tone(pal, DIM))]
            if failing:
                parts.append((f" ✕{failing}", f"bold {PALETTE['red']}"))
            parts.append((" ", ""))
            out.append((name, parts))
        return out

    def _placed(self, width: int) -> tuple[list[Part], tuple[int, int] | None]:
        """The parts that fit (starting late enough that the current board shows) and the current entry's span."""
        entries = self._entries()
        sizes = [sum(len(t) for t, _ in parts) for _, parts in entries]
        names = [name for name, _ in entries]
        at = names.index(self.current) if self.current in names else 0
        first = 0
        while first < at and sum(sizes[first:at + 1]) + 2 > width:
            first += 1
        parts: list[Part] = [("‹ ", tone(self.theme_palette, FAINT))] if first else []
        x = 2 if first else 0
        span = None
        for k in range(first, len(entries)):
            if x + sizes[k] > width:
                if x < width:
                    parts.append(("›", tone(self.theme_palette, FAINT)))
                break
            if k == at:
                span = (x, sizes[k] - 1)
            parts += entries[k][1]
            x += sizes[k] + 2
            parts.append(("  ", ""))
        return parts, span

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if y or not self.boards:
            return Strip.blank(width)
        parts, _ = self._placed(width)
        return strip(parts, width)

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        r = self.content_region
        if not self.boards or len(self.boards) < 2:
            return ()  # one board: nothing to pick between
        _, span = self._placed(r.width)
        if span is None:
            return ()
        x, w = span
        return (Pill((float(r.x + x) - 0.4, float(r.y), float(w) + 0.8, 1.0), ctx.palette.primary, focused=False,
                     ident=f"board:{id(self)}", clip=self.visible_clip()),)


class EmptyState(PixelWidget):
    """``No panels yet`` and a faint hint, centred. A hint too wide for the page is cut at its ``", or: "``."""

    DEFAULT_CSS = """
    EmptyState { height: 1fr; }
    """

    def __init__(self, hint: str = EMPTY_HINT, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.hint = hint

    def render_line(self, y: int) -> Strip:
        width, height = self.size
        top = max(0, (height - 3) // 2)
        pal = self.theme_palette
        if y == top:
            return strip([(EMPTY_TITLE.center(width), f"bold {tone(pal, SOFT)}")], width)
        if y == top + 2:
            text = self.hint if len(self.hint) <= width else self.hint.split(", or: ")[0]
            return strip([(text.center(width), tone(pal, FAINT))], width)
        return Strip.blank(width)


class ConfirmRemove(OrbitalModal):
    """``Remove "p95 latency"? y / n``: dismisses with True on ``y``, False on ``n`` or ``esc``."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "answer(True)", show=False),
        Binding("n,escape", "answer(False)", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmRemove #confirm { width: 52; height: auto; }
    ConfirmRemove.-narrow #confirm { width: 90%; }
    ConfirmRemove #confirm-text { margin: 1 0; }
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self.panel_title = title

    def compose(self) -> ComposeResult:
        text = Text.assemble(("Remove ", PALETTE["text"]), (f'"{self.panel_title}"', f"bold {PALETTE['text_bright']}"),
                             ("? ", PALETTE["text"]), ("y", f"bold {PALETTE['red']}"), (" / ", PALETTE["text_dim"]),
                             ("n", f"bold {PALETTE['accent']}"))
        with GlassPanel(title="REMOVE", accent=PALETTE["red"], accent_2=PALETTE["orange"], dialog=True, id="confirm"):
            yield Static(text, id="confirm-text")
            yield Static(hint(("y", "remove"), ("n", "keep")), classes="dialog-hint")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class PanelScreen(OrbitalModal):
    """One panel, full screen: a big version, its command and when it last ran. ``esc`` back, ``r`` run it now."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,enter", "close", show=False),
        Binding("r", "run_now", show=False),
    ]

    DEFAULT_CSS = """
    PanelScreen #full-panel { width: 96%; height: 94%; }
    PanelScreen #full-hint { height: 1; }
    """

    def __init__(self, spec: PanelSpec, frame: Frame | None, error: str | None, *, last_run: datetime | None = None,
                 running: bool = False, at: datetime | None = None,
                 on_run: Callable[[str], object] | None = None) -> None:
        super().__init__()
        self.view = PanelView(spec, at=at, big=True, dialog=True, id="full-panel")
        self.view.set_frame(frame, error)
        self.view.set_run(last_run, running)
        self.on_run = on_run
        self.panel_id = spec.id

    def compose(self) -> ComposeResult:
        with self.view:
            yield Static(hint(("esc", "back"), ("r", "run now")), id="full-hint", classes="dialog-hint")

    def minute_tick(self, at: datetime) -> None:
        self.view.tick(at)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_run_now(self) -> None:
        if self.on_run is not None:
            self.on_run(self.panel_id)


class PanelGrid(VerticalScroll, can_focus=False, inherit_bindings=False):
    """The panels' grid: it scrolls, but leaves the arrow keys to the page (they move between panels)."""


# ================================================================================================= the page ==


@dataclass
class PanelState:
    """What the page keeps per live panel, on every board."""

    spec: PanelSpec
    frame: Frame | None = None
    error: str | None = None
    running: bool = False
    again: bool = False  # ``r`` while it ran: run once more when it ends
    timer: Timer | None = None
    last_run: datetime | None = None


class BoardsPage(Page, can_focus=True):
    """The boards and their panels (see the module docstring).

    Args:
        source_key: The key of the source whose payload is the live panels (``BoardsSource``).
        runner: Runs one panel and returns its frame (raises with a one-line message); called in a thread.
        remove: Removes a panel for good (the store's ``remove``); None: ``x`` can't remove.
        arrange: Saves a panel's size or position (the store's ``arrange``); None: the panels can't be arranged.
        empty_hint: The faint line under ``No panels yet`` (default :data:`EMPTY_HINT`): how panels get here.
    """

    KEYS: ClassVar[Sequence[tuple[str, str]]] = [
        ("[ ]", "board"), ("←→↑↓", "panel"), ("⏎", "full"), ("+ -", "size"), ("< >", "move"), ("x", "remove"),
        ("r", "run"), ("?", "help"), ("q", "quit")]
    #: Seconds an arrangement the page saved wins over a folder read that doesn't show it yet (a read that began
    #: before the write); the store's own copy wins once it agrees, or after this.
    ARRANGE_GRACE: ClassVar[float] = 10.0

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left_square_bracket", "board(-1)", "board", show=False),
        Binding("right_square_bracket", "board(1)", "board", show=False),
        Binding("left", "move(-1, 0)", show=False),
        Binding("right", "move(1, 0)", show=False),
        Binding("up", "move(0, -1)", show=False),
        Binding("down", "move(0, 1)", show=False),
        Binding("tab", "move(1, 0)", show=False),
        Binding("shift+tab", "move(-1, 0)", show=False),
        Binding("enter", "open", show=False),
        Binding("plus", "resize(1)", show=False),
        Binding("minus", "resize(-1)", show=False),
        Binding("less_than_sign", "reorder(-1)", show=False),
        Binding("greater_than_sign", "reorder(1)", show=False),
        Binding("x", "remove_panel", show=False),
        Binding("r", "run_panel", show=False),
    ]

    DEFAULT_CSS = """
    BoardsPage { height: 1fr; }
    BoardsPage > #boards-grid { layout: grid; grid-size: 12; grid-gutter: 0; height: 1fr;
                                scrollbar-size-vertical: 1; }
    BoardsPage > #boards-empty { height: 1fr; }
    BoardsPage PanelView:focus { border: double $primary; border-right: double $secondary;
                                 border-bottom: double $secondary; }
    """

    def __init__(self, *, source_key: str = "BD", runner: Callable[[PanelSpec], Frame] = run,
                 remove: Callable[[PanelSpec], object] | None = None, arrange: Arrange | None = None,
                 empty_hint: str = EMPTY_HINT, id: str | None = None) -> None:
        super().__init__(id=id)
        self.empty_hint = empty_hint
        self.source_key = source_key
        self.runner = runner
        self.remover = remove
        self.arranger = arrange
        self.panels: tuple[PanelSpec, ...] = ()
        self.runs: dict[str, PanelState] = {}
        self.current: str | None = None
        self.full: PanelScreen | None = None
        self.plan: Plan = Plan((), ())  # the current board's layout
        self._payload: object = None
        self._removed: set[str] = set()
        self._arranged: dict[str, tuple[Size, float | None, float]] = {}  # id: (size, position, until)
        self._focus_id: str | None = None
        self._views: dict[str, PanelView] = {}  # the current board's panel widgets, by panel id, in order
        self._layout_key: tuple[object, ...] = ()

    def compose(self) -> ComposeResult:
        yield BoardRow(id="board-row")
        yield EmptyState(self.empty_hint, id="boards-empty")
        yield PanelGrid(id="boards-grid")

    def on_mount(self) -> None:
        self._show_board()

    # -- data -------------------------------------------------------------------------------------------------

    def update_snapshot(self, snapshot: Snapshot) -> None:
        payload = snapshot.payload(self.source_key)
        if payload is None or payload is self._payload:
            return
        self._payload = payload
        self.set_panels(cast("Sequence[PanelSpec]", payload))

    def set_panels(self, panels: Sequence[PanelSpec]) -> None:
        """The live panels on every board, ordered by board, then as the store orders them (``order_key``).

        A panel whose only change is its size or position (the same ``changed_at``: it was arranged) keeps its
        runs and its history; the page only lays out again."""
        incoming = {p.id for p in panels}
        self._removed &= incoming  # a removal the store has seen is forgotten
        panels = self._with_arranged(tuple(p for p in panels if p.id not in self._removed))
        live = {p.id: p for p in panels}
        for pid in [pid for pid in self.runs if pid not in live]:
            self._drop(pid)
        for spec in panels:
            state = self.runs.get(spec.id)
            if state is None:
                self.runs[spec.id] = PanelState(spec)
                self._run(spec.id)
            elif state.spec.changed_at != spec.changed_at:  # replaced: a fresh start, run at once
                self._stop_timer(state)
                self.runs[spec.id] = PanelState(spec)
                self._close_full(spec.id, unless_kind=spec.kind)
                self._run(spec.id)
            elif state.spec != spec:
                state.spec = spec
        self.panels = panels
        boards = self.board_names()
        if self.current not in boards:
            self.current = self._nearest(boards)
        self._show_board()

    def _with_arranged(self, panels: tuple[PanelSpec, ...]) -> tuple[PanelSpec, ...]:
        """``panels`` with the arrangements this page saved that the folder read doesn't show yet (it began
        before the write). Each one is forgotten once the read agrees, or after :attr:`ARRANGE_GRACE`."""
        if not self._arranged:
            return panels
        now = time.monotonic()
        self._arranged = {pid: a for pid, a in self._arranged.items() if any(p.id == pid for p in panels)}
        out: list[PanelSpec] = []
        moved = False
        for spec in panels:
            pending = self._arranged.get(spec.id)
            if pending is not None:
                size, position, until = pending
                if (spec.size, spec.position) == (size, position) or now > until:
                    del self._arranged[spec.id]
                else:
                    moved = moved or spec.position != position
                    spec = replace(spec, size=size, position=position)
            out.append(spec)
        if moved:
            out.sort(key=lambda p: (p.board, p.order_key))
        return tuple(out)

    def board_names(self) -> list[str]:
        return sorted({p.board for p in self.panels})

    def board_panels(self, board: str | None = None) -> list[PanelSpec]:
        name = self.current if board is None else board
        return [p for p in self.panels if p.board == name]

    def _nearest(self, boards: list[str]) -> str | None:
        """The board where the current one was (the next one, or the last); at first, the default board."""
        if not boards:
            return None
        if self.current is None:
            return DEFAULT_BOARD if DEFAULT_BOARD in boards else boards[0]
        return boards[min(bisect.bisect_left(boards, self.current), len(boards) - 1)]

    # -- running ----------------------------------------------------------------------------------------------

    def _run(self, pid: str) -> None:
        state = self.runs.get(pid)
        if state is None:
            return
        if state.running:
            state.again = True
            return
        self._stop_timer(state)
        state.running = True
        spec = state.spec
        app = cast(OrbitalApp, self.app)
        app.fetch(self.runner, spec, then=partial(self._ran, pid, spec.changed_at), timeout=spec.timeout + 5)
        self._run_info(pid)

    def _ran(self, pid: str, changed_at: datetime, frame: Frame | None, error: BaseException | None) -> None:
        state = self.runs.get(pid)
        if state is None or state.spec.changed_at != changed_at:
            return  # the panel has gone, or was replaced while this ran: dropped
        state.running = False
        state.last_run = datetime.now(timezone.utc)
        if error is not None:
            state.error = describe_error(error)
        elif frame is None:
            state.error = "the run gave nothing"
        else:
            state.frame = accumulate(state.frame, frame, state.spec.window)
            state.error = None
        view = self._view(pid)
        if view is not None:
            view.set_frame(state.frame, state.error)
            self._layout()  # a table's rows or a bar chart's series may need another height
        if self.full is not None and self.full.panel_id == pid:
            self.full.view.set_frame(state.frame, state.error)
        self._run_info(pid)
        self._board_row()
        if state.again:
            state.again = False
            self._run(pid)
        elif state.spec.cmd is not None:
            state.timer = self.set_timer(state.spec.every, partial(self._due, pid), name=f"panel-{pid}")

    def _due(self, pid: str) -> None:
        try:
            state = self.runs.get(pid)
            if state is not None:
                state.timer = None
                self._run(pid)
        except Exception as error:  # noqa: BLE001 - a timer callback: reported, never silent (rule 26)
            self._report("panel run", error)

    def _run_info(self, pid: str) -> None:
        state = self.runs.get(pid)
        if state is not None and self.full is not None and self.full.panel_id == pid:
            self.full.view.set_run(state.last_run, state.running)

    @staticmethod
    def _stop_timer(state: PanelState) -> None:
        if state.timer is not None:
            state.timer.stop()
            state.timer = None

    def _drop(self, pid: str) -> None:
        state = self.runs.pop(pid, None)
        if state is not None:
            self._stop_timer(state)
        self._close_full(pid)

    def _close_full(self, pid: str, unless_kind: str | None = None) -> None:
        """The full-screen view of a panel that went (or changed kind) closes, if it is on top (rule 28)."""
        full = self.full
        if full is None or full.panel_id != pid:
            return
        if unless_kind is not None and full.view.spec.kind == unless_kind:
            state = self.runs.get(pid)
            if state is not None:
                full.view.set_spec(state.spec)
                full.view.set_frame(state.frame, state.error)
            return
        if self.app.screen is full:
            full.dismiss(None)

    def _report(self, where: str, error: BaseException) -> None:
        report = getattr(self.app, "report_error", None)
        if callable(report):
            report(where, error)
        else:  # pragma: no cover - outside an orbital app
            raise error

    # -- layout -----------------------------------------------------------------------------------------------

    def _view(self, pid: str) -> PanelView | None:
        return self._views.get(pid)

    def views(self) -> list[PanelView]:
        """The current board's panel widgets, in order."""
        return list(self._views.values())

    def _board_row(self) -> None:
        counts: dict[str, list[int]] = {}
        for p in self.panels:
            entry = counts.setdefault(p.board, [0, 0])
            entry[0] += 1
            state = self.runs.get(p.id)
            if state is not None and state.error is not None:
                entry[1] += 1
        self.query_one(BoardRow).set_boards([(b, n, f) for b, (n, f) in sorted(counts.items())], self.current)

    def _show_board(self) -> None:
        """Show the current board's panels: widgets for new panels, none for gone ones, the rest kept as they are."""
        if not self.is_attached:
            return
        self._board_row()
        grid = self.query_one("#boards-grid", PanelGrid)
        empty = self.query_one(EmptyState)
        has = bool(self.panels)
        if grid.display != has:
            grid.display = has
        if empty.display == has:
            empty.display = not has
        want = [(p.id, p.kind) for p in self.board_panels()]
        shown = [(pid, view.kind) for pid, view in self._views.items()]
        if want != shown:
            self._focus_id = self._focused_id() or self._focus_id
            for pid, kind in shown:
                if (pid, kind) not in want:
                    self._views.pop(pid).remove()
            # Panels that moved (arranged) keep their widgets: they only change places in the grid.
            reorder = list(self._views) != [pid for pid, _ in want if pid in self._views]
            at = datetime.now(timezone.utc)
            views: dict[str, PanelView] = {}
            previous: PanelView | None = None
            for pid, _ in want:
                found = self._views.get(pid)
                if found is None:
                    state = self.runs[pid]
                    view = PanelView(state.spec, at=at)
                    view.set_frame(state.frame, state.error)
                    if previous is not None:
                        grid.mount(view, after=previous)
                    elif self._views:
                        grid.mount(view, before=next(iter(self._views.values())))
                    else:
                        grid.mount(view)
                else:
                    view = found
                    if reorder:
                        if previous is not None:
                            grid.move_child(view, after=previous)
                        else:
                            grid.move_child(view, before=0)
                views[pid] = view
                previous = view
            self._views = views
            self.call_after_refresh(self._restore_focus)
        for pid, shown_view in self._views.items():
            live = self.runs.get(pid)
            if live is not None:
                shown_view.set_spec(live.spec)
                shown_view.set_frame(live.frame, live.error)
        self._layout()

    @staticmethod
    def _need(view: PanelView) -> Need:
        """What a panel needs from the layout: its size, and its kind's minimum height for what it shows."""
        spec, frame = view.spec, view.frame
        count = 0
        if frame is not None and spec.kind == "bars":
            count = len(frame.series)
        elif frame is not None and spec.kind == "table" and frame.table is not None:
            count = len(frame.table.rows)
        return need(spec.kind, spec.size, count)

    def _layout(self) -> None:
        """Lay the current board's panels out (:func:`~.layout.plan`): each one's column span on the
        12-unit grid, and the rows' heights. Nothing is touched while the plan stays the same."""
        if not self.is_attached:
            return
        views = self.views()
        width, height = self.size.width, max(1, self.size.height - 1)
        laid = plan([self._need(view) for view in views], width, height)
        key = (tuple(views), laid)  # the widgets themselves: a panel that changed kind has a new one
        if key == self._layout_key:
            return
        self._layout_key = key
        self.plan = laid
        for view, units in zip(views, laid.spans(), strict=True):
            if view.styles.column_span != units:
                view.styles.column_span = units
        grid = self.query_one("#boards-grid", PanelGrid)
        grid.styles.grid_size_columns = UNITS
        grid.styles.grid_rows = " ".join(str(h) for h in laid.heights) if laid.heights else "1"

    def on_resize(self, event: events.Resize) -> None:
        self._layout()

    # -- focus and keys ---------------------------------------------------------------------------------------

    def _focused_id(self) -> str | None:
        focused = self.app.focused
        return focused.spec.id if isinstance(focused, PanelView) else None

    def _restore_focus(self) -> None:
        if not self.is_attached or self.app.screen is not self.screen:
            return
        views = self.views()
        if not views:
            if self.app.focused is None or not self.app.focused.is_attached:
                self.focus()
            return
        focused = self.app.focused
        if isinstance(focused, PanelView) and focused in views:
            return
        target = next((v for v in views if v.spec.id == self._focus_id), views[0])
        target.focus()

    def focus_default(self) -> None:
        self._restore_focus()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if isinstance(event.widget, PanelView):
            self._focus_id = event.widget.spec.id

    def focused_view(self) -> PanelView | None:
        focused = self.app.focused
        return focused if isinstance(focused, PanelView) and focused in self.views() else None

    def action_board(self, step: int) -> None:
        boards = self.board_names()
        if not boards:
            return
        at = boards.index(self.current) if self.current in boards else 0
        self.current = boards[(at + step) % len(boards)]
        self._focus_id = None
        self._show_board()

    def action_move(self, dx: int, dy: int) -> None:
        views = self.views()
        if not views:
            return
        current = self.focused_view()
        at = views.index(current) if current is not None else -1
        if at < 0:
            views[0].focus()
            return
        if dy and len(self.plan.spans()) == len(views):
            target = self.plan.vertical(at, dy)  # the panel above or below the middle of this one
        else:
            target = min(len(views) - 1, max(0, at + dx + dy))
        views[target].focus()

    def action_open(self) -> None:
        view = self.focused_view()
        if view is None:
            return
        state = self.runs.get(view.spec.id)
        if state is None:
            return
        screen = PanelScreen(state.spec, state.frame, state.error, last_run=state.last_run, running=state.running,
                             on_run=self._run)
        self.full = screen
        self.app.push_screen(screen, partial(self._full_closed, screen))

    def _full_closed(self, screen: PanelScreen, result: object) -> None:
        if self.full is screen:
            self.full = None

    def action_remove_panel(self) -> None:
        view = self.focused_view()
        if view is None:
            return
        if self.remover is None:
            self.app.notify("Panels can't be removed here", timeout=2)
            return
        spec = view.spec
        self.app.push_screen(ConfirmRemove(spec.title), partial(self._confirmed, spec))

    def _confirmed(self, spec: PanelSpec, yes: bool | None) -> None:
        if not yes or self.remover is None:
            return
        try:
            self.remover(spec)
        except Exception as error:  # noqa: BLE001 - reported, and the panel stays (rule 26)
            self._report("remove panel", error)
            return
        view = self._view(spec.id)
        glass = getattr(self.app, "glass", None)
        if view is not None and glass is not None:
            try:
                glass.flourish("black-hole", at=view)  # gone: a small flourish where it was (glass only)
            except Exception as error:  # noqa: BLE001 - a flourish is decoration; never in the way
                self._report("remove flourish", error)
        self._removed.add(spec.id)
        self.set_panels(self.panels)
        self._poll()

    def _poll(self) -> None:
        """Read the folder again now: the store was just written."""
        poll = getattr(self.app, "poll", None)
        if callable(poll):
            try:
                poll(self.source_key)
            except KeyError:
                pass

    # -- arranging --------------------------------------------------------------------------------------------

    def action_resize(self, step: int) -> None:
        """``+`` / ``-``: the focused panel one size bigger or smaller (small, medium, large)."""
        view = self.focused_view()
        if view is None:
            return
        size = step_size(view.spec.kind, view.spec.size, step)
        if size is not None:
            self._arrange([(view.spec.id, cast(Size, size), None)])

    def action_reorder(self, step: int) -> None:
        """``<`` / ``>``: the focused panel one place earlier or later on its board."""
        view = self.focused_view()
        if view is None:
            return
        board = self.board_panels()
        ids = [p.id for p in board]
        if view.spec.id not in ids:
            return
        at = ids.index(view.spec.id)
        changes = reposition([p.order_key[0] for p in board], at, at + step)
        self._arrange([(ids[k], None, position) for k, position in sorted(changes.items())])

    def _arrange(self, changes: Sequence[tuple[str, Size | None, float | None]]) -> None:
        """Save each ``(panel id, size, position)`` through ``arrange``, then lay out again at once."""
        if not changes:
            return
        if self.arranger is None:
            self.app.notify("Panels can't be arranged here", timeout=2)
            return
        done = False
        for pid, size, position in changes:
            state = self.runs.get(pid)
            if state is None:
                continue
            try:
                self.arranger(pid, size=size, position=position)
            except Exception as error:  # noqa: BLE001 - reported, and the layout stays as it was saved (rule 26)
                self._report("arrange panel", error)
                break
            spec = replace(state.spec, size=size or state.spec.size,
                           position=position if position is not None else state.spec.position)
            state.spec = spec
            self._arranged[pid] = (spec.size, spec.position, time.monotonic() + self.ARRANGE_GRACE)
            done = True
        if not done:
            return
        live = {pid: state.spec for pid, state in self.runs.items()}
        self.panels = tuple(sorted((live.get(p.id, p) for p in self.panels), key=lambda p: (p.board, p.order_key)))
        self._show_board()
        view = self.focused_view()
        if view is not None:
            self.call_after_refresh(view.scroll_visible)
        self._poll()

    def action_run_panel(self) -> None:
        view = self.focused_view()
        if view is not None:
            self._run(view.spec.id)

    def on_unmount(self) -> None:
        for state in self.runs.values():
            self._stop_timer(state)

