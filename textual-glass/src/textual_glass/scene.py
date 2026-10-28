"""Read the widget tree and its computed styles, and describe the glass to draw.

Nothing here is specific to one app: bordered widgets become glass panels in
their own border colour, unbordered widgets with a neutral background become
soft glass sheets, and a few stock widgets get extra chrome (DataTable header,
zebra rows and cursor pill; OptionList and ListView highlights; Footer key
badges; ProgressBar bars; scrollbars). The app's CSS, including ``:focus``
rules and theme variables, decides every colour.

Widgets that mix in :class:`PixelMixin` add their own items in the same walk
(``pixel_items(ctx)``), so CSS chrome and custom pixels share one paint order.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, cast

from textual.color import Color
from textual.screen import ModalScreen, Screen
from textual.widget import Widget

from . import _textual_hooks as hooks
from .filter import NO_RULES, Rules
from .items import BAR, HEAVY, LINE, NONE, Badge, HeaderStrip, Panel, Pill, Progress, Rows, Surface, Thumb
from .palette import RGB, Palette, border_pair, hue_shift, lighten, luminance, mix, to_rgb
from .render import Item

if TYPE_CHECKING:
    from rich.color import Color as RichColor
    from textual.app import App
    from textual.geometry import Region
    from textual.scrollbar import ScrollBar
    from textual.widgets import DataTable, OptionList
    from textual.widgets._footer import FooterKey

    from .engine import GlassEngine
    from .render import Rect

#: Border types drawn in pixels. Their glyphs are blanked while glass is on.
#: Half-block styles that make 3D bevels (tall, wide, panel, tab, block) are
#: left to Textual.
GLASS_EDGES: dict[str, str] = {
    "ascii": LINE, "round": LINE, "solid": LINE, "dashed": LINE, "hkey": LINE, "vkey": LINE,
    "double": HEAVY, "heavy": HEAVY,
    "thick": BAR, "outer": BAR, "inner": BAR,
}

#: Component classes whose backgrounds glass replaces with a pill, so it owns
#: them even though they are colourful.
CURSOR_COMPONENTS = {
    "DataTable": ("datatable--cursor", "datatable--fixed-cursor"),
    "OptionList": ("option-list--option-highlighted",),
}
MODAL_DIM = 0.45


@dataclass
class LevelScene:
    """The glass for one screen of the visible stack (0 = the bottom screen)."""

    level: int
    static: list[Item] = field(default_factory=list)
    dynamic: list[Item] = field(default_factory=list)
    float_static: list[Item] = field(default_factory=list)
    float_dynamic: list[Item] = field(default_factory=list)
    focus: Panel | None = None
    corners: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Scene:
    cols: int
    rows: int
    palette: Palette
    levels: list[LevelScene]
    busy: bool = False  # something is mid-animation


# =========================================================== motion ==


def ease_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


class Tweens:
    """Slides selection pills between rows instead of jumping."""

    DURATION = 0.14

    def __init__(self) -> None:
        self._state: dict[object, tuple[tuple[float, ...], tuple[float, ...], float, float]] = {}
        self._seen: set[object] = set()

    def value(self, key: object, target: tuple[float, ...], now: float, animate: bool) -> tuple[float, ...]:
        self._seen.add(key)
        state = self._state.get(key)
        if state is None or not animate:
            self._state[key] = (target, target, now, 0.0)
            return target
        if state[1] != target:
            current = self._at(state, now)
            x, y, w, h = current
            vertical = (x, w, h) == (target[0], target[2], target[3]) and abs(y - target[1]) <= 12
            horizontal = (y, h) == (target[1], target[3]) and abs(x - target[0]) <= 60
            if vertical or horizontal:
                self._state[key] = (current, target, now, self.DURATION)
            else:
                self._state[key] = (target, target, now, 0.0)
            state = self._state[key]
        return self._at(state, now)

    @staticmethod
    def _at(state: tuple[tuple[float, ...], tuple[float, ...], float, float], now: float) -> tuple[float, ...]:
        start, end, t0, duration = state
        if duration <= 0 or now - t0 >= duration:
            return end
        k = ease_out((now - t0) / duration)
        return tuple(a + (b - a) * k for a, b in zip(start, end, strict=True))

    def busy(self, now: float) -> bool:
        return any(d > 0 and now - t0 < d for _, _, t0, d in self._state.values())

    def end_frame(self) -> None:
        """Forget pills that were not drawn this frame."""
        for key in [k for k in self._state if k not in self._seen]:
            del self._state[key]
        self._seen.clear()


# ======================================================= classification ==


def _rgb(c: Color) -> RGB:
    return (c.r, c.g, c.b)


def _rich_rgb(color: RichColor | None, fallback: RGB) -> RGB:
    """A Rich colour as RGB (``fallback`` for none, or an ANSI colour)."""
    if color is None or color.triplet is None:
        return fallback
    t = color.triplet
    return (t.red, t.green, t.blue)


_TYPE_NAMES: dict[type, frozenset[str]] = {}


def _type_names(cls: type) -> frozenset[str]:
    names = _TYPE_NAMES.get(cls)
    if names is None:
        names = _TYPE_NAMES[cls] = frozenset(base.__name__ for base in cls.__mro__)
    return names


def is_a(widget: object, name: str) -> bool:
    """Is ``widget`` an instance of a class called ``name``? (No imports of
    widget modules needed, and subclasses count.)"""
    return name in _type_names(widget.__class__)


def skipped(widget: Widget, skip: Sequence[str]) -> bool:
    names = _type_names(widget.__class__)
    return any(name in names for name in skip)


def visible_screens(app: App) -> list[Screen]:
    """The top screen plus the screens that show through below it (modals)."""
    stack = list(app.screen_stack)
    i = len(stack) - 1
    while i > 0 and isinstance(stack[i], ModalScreen):
        i -= 1
    return stack[i:]


def rules_for(widget: Widget, palette: Palette, skip: Sequence[str]) -> Rules:
    """Which of this widget's cell backgrounds the glass replaces."""
    if isinstance(widget, Screen):
        if isinstance(widget, ModalScreen) and widget is not widget.app.screen_stack[0]:
            ansi_bg = widget.app.ansi_theme.background_color
            # Pixel widgets below draw text with no background. Seen through the modal (Textual's
            # BackgroundScreen) those cells pick up the modal's own background, so reset that too.
            base, bg = widget.background_colors
            return Rules(owned=frozenset({(ansi_bg.red, ansi_bg.green, ansi_bg.blue), _rgb(base), _rgb(bg)}),
                         dim=(palette.scrim, MODAL_DIM if palette.dark else MODAL_DIM * 0.7))
        base, bg = widget.background_colors
        return Rules(owned=frozenset({_rgb(bg), _rgb(base)}))
    if skipped(widget, skip):
        return NO_RULES
    if is_a(widget, "ScrollBar") or is_a(widget, "ScrollBarCorner"):
        return Rules(blank=True)
    if is_a(widget, "Bar") and getattr(widget, "percentage", None) is not None:
        return Rules(blank=True)
    owned: set[RGB] = set()
    base, bg = widget.background_colors
    for c in (bg, base):
        rgb = _rgb(c)
        if c.a > 0 and palette.is_structural(rgb):
            owned.add(rgb)
    if is_a(widget, "ListItem") and widget.has_class("-highlight"):
        owned.add(_rgb(bg))
    cursors = next((v for k, v in CURSOR_COMPONENTS.items() if is_a(widget, k)), ())
    if getattr(widget, "cursor_type", "row") not in ("row", "cell"):
        cursors = ()  # a column cursor has no pill: leave Textual's highlight alone
    components = sorted(getattr(type(widget), "COMPONENT_CLASSES", ()) or ())
    for name in components:
        try:
            style = widget.get_component_rich_style(name)
        except Exception:  # noqa: BLE001
            continue
        if style.bgcolor is None or style.bgcolor.triplet is None:
            continue
        triplet = style.bgcolor.triplet
        rgb = (triplet.red, triplet.green, triplet.blue)
        if name in cursors or palette.is_structural(rgb):
            owned.add(rgb)
            if is_a(widget, "DataTable"):
                # cell/column cursors extend rows with a faded copy of the row colour
                faded = Color(*rgb).blend(bg, 0.25)
                owned.add(_rgb(faded))
    return Rules(owned=frozenset(owned)) if owned else NO_RULES


# =============================================================== edges ==


Edges = tuple[tuple[str, str, str, str], tuple[RGB, RGB, RGB, RGB]]


def _edges(widget: Widget, dark: bool = True) -> Edges | None:
    """Glass edge styles (top, right, bottom, left) and the colours of those sides, if any.

    Near-black separator lines (the command palette's) vanish on dark themes:
    on glass they would read as cracks.
    """
    styles = widget.styles
    base = widget.background_colors[0]
    for sides in (
        (styles.border_top, styles.border_right, styles.border_bottom, styles.border_left),
        (styles.outline_top, styles.outline_right, styles.outline_bottom, styles.outline_left),
    ):
        kinds: list[str] = []
        colours: list[RGB] = []
        for kind, colour in sides:
            style = GLASS_EDGES.get(kind, NONE)
            rgb = _rgb(base + colour)
            if style and dark and luminance(rgb) < 0.06:
                style = NONE
            kinds.append(style)
            colours.append(rgb)
        if any(kinds):
            return (kinds[0], kinds[1], kinds[2], kinds[3]), (colours[0], colours[1], colours[2], colours[3])
    return None


def _gradient_ends(edges: Edges) -> tuple[RGB, RGB]:
    """The two ends of a panel's border gradient: the left and right border
    colours when they differ (``GlassPanel.set_accent(c1, c2)``), otherwise the
    first drawn side's colour and a hue-shifted copy of it."""
    kinds, colours = edges
    if kinds[3] and kinds[1] and colours[3] != colours[1]:
        return colours[3], colours[1]
    return border_pair(next(c for k, c in zip(kinds, colours, strict=True) if k))


def _label_span(widget: Widget, title: bool) -> tuple[float, float] | None:
    """Where Textual draws the border title (or subtitle), in screen cells.

    Mirrors ``textual._border.render_border_label`` and ``render_row``.
    """
    length = hooks.border_label_length(widget, title)
    if not length:
        return None
    styles = widget.styles
    region = widget.region
    width = region.width
    has_left = styles.border_left[0] != ""
    has_right = styles.border_right[0] != ""
    corners = int(has_left) + int(has_right)
    available = (width - 2) - 2 * corners
    if available <= 0:
        return None
    label = min(length, available) + int(has_left) + int(has_right)
    space = max(0, width - corners - label)
    align = styles.border_title_align if title else styles.border_subtitle_align
    if not space:
        start = int(has_left)
    elif align == "left":
        start = int(has_left) + 1
    elif align == "right":
        start = int(has_left) + space - 1
    else:
        start = int(has_left) + space // 2
    x = region.x + start
    return (float(x), float(x + label))


def _floating(widget: Widget, screen: Screen) -> bool:
    """On one of Textual's own overlay layers (toasts, tooltips) or ``overlay: screen``."""
    for node in widget.ancestors_with_self:
        if node is screen:
            return False
        styles = node.styles
        if styles.overlay == "screen" or styles.layer.startswith("_"):
            return True
    return False


# =============================================================== build ==


@dataclass
class PixelContext:
    """What ``pixel_items`` gets (and what the CSS scene reads): the engine, the
    palette, the time, the screen level, focus, and a way to ask for frames."""

    engine: GlassEngine
    palette: Palette
    now: float
    level: int
    focused: Widget | None
    animate: bool  # motion is not "minimal": tweens may slide
    tweens: Tweens
    skip: Sequence[str]  # GLASS_SKIP: widgets whose CSS chrome is left alone
    busy: bool = False

    def animating(self) -> None:
        """Call from ``pixel_items`` while an item is mid-animation (a sliding
        pill): the engine then keeps ticking at 60 Hz until it settles."""
        self.busy = True


class PixelMixin:
    """Mix into a Textual widget to give it pixels: ``class Gauge(PixelMixin, Widget)``.

    Override :meth:`pixel_items` to yield items (``textual_glass.items``) in
    *screen* cells (use ``self.region`` / ``self.content_region``). Give each a
    stable ``ident`` (``f"ring:{id(self)}"``) so a change is diffed precisely.
    Nothing calls it while glass is off, so ``render_line`` should always draw a
    sensible text fallback (:attr:`glass_on` says which mode is current).
    """

    def pixel_items(self, ctx: PixelContext) -> Iterable[Item]:
        return ()

    @property
    def glass(self) -> GlassEngine | None:
        """The app's glass engine (``None`` in an app without the ``Glass`` mixin)."""
        from .engine import GlassEngine

        try:
            engine = getattr(getattr(self, "app", None), "glass", None)
        except Exception:  # noqa: BLE001 - not attached to an app
            return None
        return engine if isinstance(engine, GlassEngine) else None

    @property
    def glass_on(self) -> bool:
        engine = self.glass
        return engine is not None and engine.active


def build(app: App, ctx: PixelContext) -> Scene:
    cols, rows = app.size
    levels = []
    for level, screen in enumerate(visible_screens(app)):
        ctx.level = level
        levels.append(_build_level(screen, level, ctx))
    ctx.tweens.end_frame()
    return Scene(cols, rows, ctx.palette, levels, busy=ctx.tweens.busy(ctx.now) or ctx.busy)


def _build_level(screen: Screen, level: int, ctx: PixelContext) -> LevelScene:
    out = LevelScene(level)
    pal = ctx.palette
    depth_of: dict[Widget, int] = {}
    painted: dict[Widget, RGB] = {}  # the glass colour each drawn widget leaves under its children
    panels: dict[Widget, int] = {}
    screen_rgb = _rgb(screen.background_colors[1])
    variables = screen.app.theme_variables
    cursor_rgb = to_rgb(variables.get("block-cursor-background"), pal.primary)

    for widget, visible in hooks.visible_widgets(screen):
        if widget is screen or not widget.visible or widget.styles.display == "none":
            continue
        depth, under = 0, screen_rgb
        for ancestor in widget.ancestors:
            if ancestor is screen:
                break
            if ancestor in depth_of:
                depth, under = depth_of[ancestor] + 1, painted[ancestor]
                break
        region = widget.region
        clip = None if visible == region else (visible.x, visible.y, visible.width, visible.height)
        floating = _floating(widget, screen)
        static = out.float_static if floating else out.static
        dynamic = out.float_dynamic if floating else out.dynamic
        drawn = _base_chrome(widget, region, clip, level, depth, under, ctx, static, panels)
        if drawn is not None:
            depth_of[widget], painted[widget] = depth, drawn
        _widget_chrome(widget, region, clip, depth, ctx, cursor_rgb, static, dynamic)
        if isinstance(widget, PixelMixin):
            for item in widget.pixel_items(ctx):
                (dynamic if item.dynamic else static).append(item)

    _mark_focus(screen, out, panels)
    if level == 0:
        out.corners = [(p.rect[0], p.rect[1]) for p in out.static if isinstance(p, Panel) and p.depth == 0][:6]
    return out


def _base_chrome(widget: Widget, region: Region, clip: Rect | None, level: int, depth: int, under: RGB,
                 ctx: PixelContext, static: list[Item], panels: dict[Widget, int]) -> RGB | None:
    """A glass panel for bordered widgets; a glass sheet for unbordered widgets whose
    background differs from the glass beneath them. Returns the colour drawn, if any."""
    pal = ctx.palette
    if not region.width or not region.height:
        return None
    rect = (region.x, region.y, region.width, region.height)
    base, bg = widget.background_colors
    bg_rgb = _rgb(bg)
    structural = pal.is_structural(bg_rgb) and not skipped(widget, ctx.skip)
    dialog = (level > 0 and depth == 0) or is_a(widget, "Toast")
    edges = _edges(widget, pal.dark)
    if edges is not None and not is_a(widget, "ScrollBar"):
        kinds = edges[0]
        c1, c2 = _gradient_ends(edges)
        fill = mix(bg_rgb, c1, 0.035) if structural else None
        titles = tuple(s for s in [_label_span(widget, True)] if s and kinds[0])
        subtitles = tuple(s for s in [_label_span(widget, False)] if s and kinds[2])
        panels[widget] = len(static)
        static.append(Panel(rect, kinds, c1, c2, fill, 0.9 if dialog else 0.74, titles, subtitles,
                            dialog=dialog, dark=pal.dark, ident=f"panel:{id(widget)}", depth=depth, clip=clip))
        return bg_rgb
    # A background of its own, or one inherited from a hidden container (the
    # command palette's input and results sit in an invisible box).
    if structural and bg_rgb != under:
        static.append(Surface(rect, bg_rgb, 0.9 if dialog else 0.6, 0.35, dialog=dialog,
                              rim=pal.primary if dialog else None, dark=pal.dark, depth=depth, clip=clip))
        return bg_rgb
    return None


def _widget_chrome(widget: Widget, region: Region, clip: Rect | None, depth: int, ctx: PixelContext, cursor_rgb: RGB,
                   static: list[Item], dynamic: list[Item]) -> None:
    pal = ctx.palette
    if is_a(widget, "DataTable"):
        _table(cast("DataTable", widget), depth, ctx, cursor_rgb, static, dynamic)
    elif is_a(widget, "OptionList"):
        _option_list(cast("OptionList", widget), depth, ctx, cursor_rgb, dynamic)
    elif is_a(widget, "ListItem") and widget.has_class("-highlight"):
        scroll_y = int(getattr(widget.parent, "scroll_y", 0))
        target = (float(region.x), float(region.y + scroll_y), float(region.width), float(region.height))
        x, y, w, h = ctx.tweens.value(("list", id(widget.parent)), target, ctx.now, ctx.animate)
        rect = (x, y - scroll_y, w, h)
        focused = bool(getattr(widget.parent, "has_focus", False))
        dynamic.append(Pill(rect, cursor_rgb, focused, depth=depth + 1, clip=clip))
    elif is_a(widget, "FooterKey"):
        _footer_key(cast("FooterKey", widget), depth, static)
    elif is_a(widget, "Bar"):
        percentage = getattr(widget, "percentage", None)
        if percentage is not None and region.width:
            try:
                name = "bar--complete" if percentage >= 1 else "bar--bar"
                c1 = _rich_rgb(widget.get_component_rich_style(name).color, pal.primary)
            except Exception:  # noqa: BLE001
                c1 = pal.primary
            c2 = hue_shift(c1, 24, light=0.08)
            rect = (region.x, region.y, region.width, region.height)
            dynamic.append(Progress(rect, float(percentage), c1, c2, pal.dark, depth=depth + 1, clip=clip))
    elif is_a(widget, "ScrollBar"):
        _scrollbar(cast("ScrollBar", widget), region, depth, ctx, dynamic)


def _table(table: DataTable, depth: int, ctx: PixelContext, cursor_rgb: RGB,
           static: list[Item], dynamic: list[Item]) -> None:
    pal = ctx.palette
    content = table.content_region
    if not content.width or not content.height:
        return
    edges = _edges(table, pal.dark)
    accent = _gradient_ends(edges) if edges else border_pair(pal.primary)
    header_h = table.header_height if table.show_header else 0
    if header_h:
        try:
            header_bg = table.get_component_rich_style("datatable--header").bgcolor
            header_rgb = _rich_rgb(header_bg, pal.panel)
        except Exception:  # noqa: BLE001
            header_rgb = pal.panel
        rect = (content.x, content.y, content.width, min(header_h, content.height))
        static.append(HeaderStrip(rect, header_rgb, accent[0], accent[1], pal.dark, depth=depth + 1))
    rows = table.ordered_rows
    if not rows:
        return
    heights = [max(1, row.height) for row in rows]
    fixed = min(table.fixed_rows, len(rows))
    fixed_h = sum(heights[:fixed])
    top = content.y + header_h
    scroll_top = top + fixed_h
    bottom = content.bottom
    scroll_y = int(table.scroll_y)

    def screen_y(index: int, offset: int) -> int:
        return top + offset - (0 if index < fixed else scroll_y)

    # zebra stripes, only for rows on screen
    zebra: list[tuple[int, int]] = []
    cursor_row = table.cursor_coordinate.row if table.show_cursor and table.cursor_type in ("row", "cell") else -1
    cursor_box = None
    offset = 0
    for index, h in enumerate(heights):
        y = screen_y(index, offset)
        area_top = top if index < fixed else scroll_top
        if y >= bottom and index >= fixed:
            break
        y0, y1 = max(y, area_top), min(y + h, bottom)
        if y1 > y0 and table.zebra_stripes and index % 2 == 0 and index >= fixed:
            zebra.append((y0, y1 - y0))
        if index == cursor_row:
            cursor_box = (offset, h, area_top, index < fixed)
        offset += h
    if zebra:
        dynamic.append(Rows(content.x, content.width, tuple(zebra), pal.foreground,
                            0.03 if pal.dark else 0.045, depth=depth + 1))
    if cursor_box is None:
        return
    # The pill slides in table coordinates, so scrolling moves it with the rows
    # at once and only cursor moves animate.
    row_offset, h, area_top, fixed_row = cursor_box
    x_scroll = 0
    if table.cursor_type == "row":
        x, w = content.x, content.width
    else:
        column = table.cursor_coordinate.column
        widths = [col.get_render_width(table) for col in table.ordered_columns]
        if not 0 <= column < len(widths):
            return
        x = content.x + hooks.row_label_width(table) + sum(widths[:column])
        x_scroll = int(table.scroll_x) if column >= table.fixed_columns else 0
        w = widths[column]
    focused = table.has_focus
    colour = cursor_rgb
    solid = False
    try:
        cursor = table.get_component_rich_style("datatable--cursor")
        if focused:
            colour = _rich_rgb(cursor.bgcolor, colour)
        solid = _needs_solid(cursor.color, pal)
    except Exception:  # noqa: BLE001
        pass
    target = (float(x), float(row_offset), float(w), float(h))
    tx, ty, tw, th = ctx.tweens.value(("table", id(table)), target, ctx.now, ctx.animate)
    pill = (tx - x_scroll, top + ty - (0 if fixed_row else scroll_y), tw, th)
    area = (content.x, area_top, content.width, max(0, bottom - area_top))
    dynamic.append(Pill(pill, colour, focused, solid, depth=depth + 2, clip=area))


def _needs_solid(text_colour: RichColor | None, pal: Palette) -> bool:
    """Would the cursor's text be hard to read on translucent glass?

    Themes pick the cursor text colour to contrast with a solid cursor
    background. When it is close to the glass's own brightness (dark text in
    Nord, light text in a light theme), the pill is drawn solid instead.
    """
    if text_colour is None or text_colour.triplet is None:
        return False
    glass = mix(pal.bg_top, pal.surface, 0.5)
    return abs(luminance(_rich_rgb(text_colour, glass)) - luminance(glass)) < 0.42


def _option_list(widget: OptionList, depth: int, ctx: PixelContext, cursor_rgb: RGB, dynamic: list[Item]) -> None:
    highlighted = widget.highlighted
    if highlighted is None:
        return
    content = widget.content_region
    lines = [y for y, index in enumerate(hooks.option_lines(widget)) if index == highlighted]
    if not lines or not content.height:
        return
    target = (float(content.x), float(lines[0]), float(content.width), float(len(lines)))
    x, line, w, h = ctx.tweens.value(("options", id(widget)), target, ctx.now, ctx.animate)
    rect = (x, content.y + line - int(widget.scroll_y), w, h)
    clip = (content.x, content.y, content.width, content.height)
    in_modal = isinstance(widget.screen, ModalScreen)
    try:
        solid = _needs_solid(widget.get_component_rich_style("option-list--option-highlighted").color, ctx.palette)
    except Exception:  # noqa: BLE001
        solid = False
    dynamic.append(Pill(rect, cursor_rgb, widget.has_focus or in_modal, solid, depth=depth + 1, clip=clip))


def _footer_key(widget: FooterKey, depth: int, static: list[Item]) -> None:
    content = widget.content_region
    if not content.width:
        return
    try:
        key_styles = widget.get_component_styles("footer-key--key")
        padding = key_styles.padding
        colour = widget.get_component_rich_style("footer-key--key").color
        rgb = _rich_rgb(colour, (255, 166, 43))
    except Exception:  # noqa: BLE001
        return
    from rich.cells import cell_len

    width = padding.left + cell_len(str(widget.key_display)) + padding.right
    if not getattr(widget, "description", ""):
        width = content.width
    static.append(Badge((content.x, content.y, max(1, min(width, content.width)), 1), rgb, depth=depth + 1))


def _scrollbar(bar: ScrollBar, region: Region, depth: int, ctx: PixelContext, dynamic: list[Item]) -> None:
    vertical = bool(bar.vertical)
    length = region.height if vertical else region.width
    virtual, window, position = float(bar.window_virtual_size), float(bar.window_size), float(bar.position)
    if not length or not virtual or not window or window >= virtual:
        return
    thumb = max(1.0, length * window / virtual)
    start = (length - thumb) * (position / max(1.0, virtual - window))
    parent = bar.parent
    if not isinstance(parent, Widget):
        return
    try:
        styles = parent.styles
        base, _ = parent.background_colors
        active = bool(bar.grabbed) or bool(bar.mouse_over)
        colour = styles.scrollbar_color_active if bar.grabbed else (
            styles.scrollbar_color_hover if bar.mouse_over else styles.scrollbar_color)
        rgb = _rgb(base + colour)
    except Exception:  # noqa: BLE001
        rgb, active = ctx.palette.primary, False
    rgb = lighten(mix(rgb, ctx.palette.primary, 0.35), 0.18) if ctx.palette.dark else rgb
    rect = (region.x, region.y, region.width, region.height)
    dynamic.append(Thumb(rect, start, thumb, vertical, rgb, active, depth=depth + 3))


def _mark_focus(screen: Screen, out: LevelScene, panels: dict[Widget, int]) -> None:
    """Light up the innermost glass panel that contains the focused widget."""
    node: Widget | None = screen.focused
    while node is not None and node is not screen:
        index = panels.get(node)
        panel = out.static[index] if index is not None else None
        if index is not None and isinstance(panel, Panel):
            out.static[index] = out.focus = replace(panel, focused=True)
            return
        node = node.parent  # type: ignore[assignment]
