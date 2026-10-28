"""``GlassImage``: real pixels with glass, half-block characters without."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Union

import numpy as np
from rich.segment import Segment
from rich.style import Style
from textual.geometry import Size
from textual.strip import Strip
from textual.widget import Widget

from .. import kitty
from ..images import LoadedImage, Source, load_image

MAX_IMAGE_PIXELS = 4_000_000  # per frame; bigger widgets get an image the terminal scales up
MAX_ANIMATION_PIXELS = 32_000_000  # all frames together (about 128 MB in the terminal)
MAX_NATURAL_CELLS = (64, 32)  # natural size cap for big images; CSS width/height override it
PIXEL_ART_MAX = 128  # images this small or smaller default to crisp nearest-neighbour scaling
ImageData = Union[bytes, bytearray, memoryview, np.ndarray]  # noqa: UP007 - a runtime alias on 3.10


def _as_array(data: ImageData, width: int, height: int) -> np.ndarray:
    array = np.asarray(data, dtype=np.uint8) if isinstance(data, np.ndarray) else np.frombuffer(bytes(data), np.uint8)
    if array.size != width * height * 4:
        raise ValueError(f"expected {width}x{height} RGBA ({width * height * 4} bytes), got {array.size}")
    return array.reshape(height, width, 4)


def _weights(n_in: int, n_out: int) -> np.ndarray:
    """An (n_out, n_in) resampling matrix: area averaging when shrinking, linear when growing."""
    weights = np.zeros((n_out, n_in), np.float32)
    scale = n_in / n_out
    if scale >= 1:
        for i in range(n_out):
            start, end = i * scale, (i + 1) * scale
            for j in range(int(start), min(n_in, math.ceil(end))):
                weights[i, j] = min(end, j + 1) - max(start, j)
        weights /= weights.sum(axis=1, keepdims=True)
    else:
        centres = (np.arange(n_out) + 0.5) * scale - 0.5
        low = np.floor(centres)
        frac = (centres - low).astype(np.float32)
        rows = np.arange(n_out)
        np.add.at(weights, (rows, np.clip(low, 0, n_in - 1).astype(int)), 1 - frac)
        np.add.at(weights, (rows, np.clip(low + 1, 0, n_in - 1).astype(int)), frac)
    return weights


def resample(image: np.ndarray, width: int, height: int) -> np.ndarray:
    """Smoothly resize RGBA pixels (for photos), in premultiplied alpha so
    transparent edges don't pick up dark fringes."""
    ih, iw = image.shape[:2]
    pixels = image.astype(np.float32)
    alpha = pixels[..., 3:4] / 255
    premultiplied = np.concatenate([pixels[..., :3] * alpha, pixels[..., 3:4]], axis=2)
    rows = (_weights(ih, height) @ premultiplied.reshape(ih, -1)).reshape(height, iw, 4)
    out = (_weights(iw, width) @ rows.transpose(1, 0, 2).reshape(iw, -1)).reshape(width, height, 4).transpose(1, 0, 2)
    coverage = out[..., 3:4] / 255
    rgb = np.where(coverage > 0, out[..., :3] / np.maximum(coverage, 1e-6), 0)
    return np.clip(np.concatenate([rgb, out[..., 3:4]], axis=2) + 0.5, 0, 255).astype(np.uint8)


def fit(image: np.ndarray, width: int, height: int, pixelated: bool = True) -> np.ndarray:
    """Scale ``image`` to fit (width, height) keeping its aspect ratio, centred on
    a transparent canvas. Nearest neighbour keeps pixel art crisp; otherwise it
    resamples smoothly."""
    ih, iw = image.shape[:2]
    if not width or not height or not iw or not ih:
        return np.zeros((max(1, height), max(1, width), 4), np.uint8)
    scale = min(width / iw, height / ih)
    if pixelated and scale >= 1:
        scale = math.floor(scale)
    tw, th = max(1, round(iw * scale)), max(1, round(ih * scale))
    if pixelated:
        ys = np.minimum((np.arange(th) + 0.5) * ih / th, ih - 1).astype(int)
        xs = np.minimum((np.arange(tw) + 0.5) * iw / tw, iw - 1).astype(int)
        scaled = image[ys][:, xs]
    else:
        scaled = resample(image, tw, th)
    canvas = np.zeros((height, width, 4), np.uint8)
    oy, ox = (height - th) // 2, (width - tw) // 2
    canvas[oy:oy + th, ox:ox + tw] = scaled
    return canvas


def half_block_lines(image: np.ndarray, cols: int, rows: int, pixelated: bool = True) -> list[Strip]:
    """Draw RGBA pixels two rows per cell with upper and lower half blocks."""
    pixels = fit(image, cols, rows * 2, pixelated)
    lines = []
    for y in range(rows):
        top, bottom = pixels[2 * y], pixels[2 * y + 1]
        segments = []
        for x in range(cols):
            t, b = top[x], bottom[x]
            t_on, b_on = t[3] >= 128, b[3] >= 128
            if t_on and b_on:
                style = Style(color=f"rgb({t[0]},{t[1]},{t[2]})", bgcolor=f"rgb({b[0]},{b[1]},{b[2]})")
                segments.append(Segment("▀", style))
            elif t_on:
                segments.append(Segment("▀", Style(color=f"rgb({t[0]},{t[1]},{t[2]})")))
            elif b_on:
                segments.append(Segment("▄", Style(color=f"rgb({b[0]},{b[1]},{b[2]})")))
            else:
                segments.append(Segment(" "))
        lines.append(Strip(list(Segment.simplify(segments)), cols))
    return lines


class GlassImage(Widget):
    """An RGBA image, optionally animated.

    With glass on, the terminal holds the pixels and the widget prints kitty
    Unicode placeholder cells, so Textual clips, scrolls and covers it like
    text. Animations are uploaded once and the terminal plays them. Without
    glass it draws half blocks (two pixels per cell) and animates with a timer.
    Its natural size is the half-block size, ``width x ceil(height / 2)``
    cells, capped at ``MAX_NATURAL_CELLS`` for big images; set ``width`` and
    ``height`` in CSS to size it yourself. Either way it keeps its aspect ratio.

    Load files with :meth:`from_file` (PNG, JPEG, GIF, WebP...; needs the
    ``images`` extra).

    Args:
        rgba: ``width * height * 4`` bytes, or an (h, w, 4) uint8 array.
        width: Image width in pixels.
        height: Image height in pixels.
        frames: More RGBA frames of the same size, for an animation.
        gap_ms: Time per frame (one number, or one per frame).
        plays: How many times an animation runs; ``None`` loops forever.
        pixelated: Scale with nearest neighbour (crisp pixel art) instead of
            smoothly (photos).
        cache_key: Keep the upload in the terminal after this widget goes away,
            and reuse it for the next widget with the same key and size (a
            popup's picture is uploaded once, not every time the popup opens).

    Under a popup it shows a dimmed copy of its first frame, uploaded with the
    others as a gapless frame that playback skips (the scrim can't cover it:
    placeholder images draw above every glass layer).
    """

    DEFAULT_CSS = """
    GlassImage {
        width: auto;
        height: auto;
    }
    """

    def __init__(
        self,
        rgba: ImageData,
        width: int,
        height: int,
        *,
        frames: Sequence[ImageData] = (),
        gap_ms: int | Sequence[int] = 100,
        plays: int | None = None,
        pixelated: bool = True,
        cache_key: str | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.cache_key = cache_key
        self.image_width = width
        self.image_height = height
        self._frames = [_as_array(rgba, width, height)] + [_as_array(f, width, height) for f in frames]
        self._gaps = [gap_ms] * len(self._frames) if isinstance(gap_ms, int) else list(gap_ms)
        self.plays = plays
        self.pixelated = pixelated
        self._image_id: int | None = None
        self._uploaded: tuple[Any, ...] | None = None
        self._uploading: tuple[Any, ...] | None = None
        self._frame = 0
        self._played = 0
        self._lines: dict[tuple[int, int, int], list[Strip]] = {}
        self._timer: Any = None

    @classmethod
    def from_image(cls, image: LoadedImage, *, pixelated: bool | None = None, **kwargs: Any) -> GlassImage:
        """A widget for frames from :func:`textual_glass.load_image`. ``pixelated``
        defaults to crisp scaling for small images and smooth for photos."""
        if pixelated is None:
            pixelated = max(image.width, image.height) <= PIXEL_ART_MAX
        first, *rest = image.frames
        return cls(first, image.width, image.height, frames=rest, gap_ms=image.gaps_ms, plays=image.plays,
                   pixelated=pixelated, **kwargs)

    @classmethod
    def from_file(cls, source: Source, *, max_size: tuple[int, int] | None = (1024, 1024), max_frames: int = 600,
                  pixelated: bool | None = None, **kwargs: Any) -> GlassImage:
        """Load a PNG, JPEG, GIF, WebP or anything else Pillow reads.

        Animated GIF, WebP and APNG keep their frame timing and loop count. Needs
        the ``images`` extra: ``uv add "textual-glass[images]"``. Other keyword
        arguments go to the widget (``id``, ``classes``...).
        """
        image = load_image(source, max_size=max_size, max_frames=max_frames)
        return cls.from_image(image, pixelated=pixelated, **kwargs)

    # -- sizing ------------------------------------------------------------------------

    def _natural_cells(self) -> tuple[int, int]:
        cols, rows = self.image_width, (self.image_height + 1) // 2
        scale = min(1.0, MAX_NATURAL_CELLS[0] / cols, MAX_NATURAL_CELLS[1] / rows)
        return max(1, round(cols * scale)), max(1, round(rows * scale))

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return self._natural_cells()[0]

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return self._natural_cells()[1]

    # -- glass ------------------------------------------------------------------------------

    @property
    def _engine(self) -> Any:
        return getattr(self.app, "glass", None)

    @property
    def glass_on(self) -> bool:
        engine = self._engine
        return engine is not None and engine.active

    def on_mount(self) -> None:
        engine = self._engine
        if engine is not None:
            engine.register_image(self)
        self.glass_changed()

    def on_unmount(self) -> None:
        engine = self._engine
        if engine is not None:
            engine.unregister_image(self)
            if self._image_id is not None and engine.active:
                if self.cache_key is not None and self._uploaded is not None and self._uploading is None:
                    engine.keep_image((self.cache_key, *self._uploaded), self._image_id)
                else:
                    engine.send(kitty.delete(self._image_id))
                    engine.ids.free(self._image_id)
                    engine.dimmables.pop(self._image_id, None)
        self._image_id = None
        self._uploading = None

    def on_resize(self) -> None:
        if self.glass_on:
            self.call_after_refresh(self._upload)

    def glass_changed(self) -> None:
        """Glass was turned on or off: switch between pixels and half blocks."""
        if self.glass_on:
            if self._timer is not None:
                self._timer.stop()
                self._timer = None
            self.call_after_refresh(self._upload)
        else:
            self.forget_upload()
            # is_attached, not is_mounted: Textual sets is_mounted only after on_mount returns
            if len(self._frames) > 1 and self._timer is None and self.is_attached:
                self._schedule_frame()
        self.refresh()

    def forget_upload(self) -> None:
        """The terminal's images are gone (glass off or exiting)."""
        self._image_id = None
        self._uploaded = None
        self._uploading = None

    # -- half-block animation ------------------------------------------------------------

    def _schedule_frame(self) -> None:
        gap = self._gaps[self._frame % len(self._gaps)]
        self._timer = self.set_timer(max(0.02, gap / 1000), self._next_frame)

    def _next_frame(self) -> None:
        self._timer = None
        following = self._frame + 1
        if following >= len(self._frames):
            self._played += 1
            if self.plays is not None and self._played >= self.plays:
                return  # done: stay on the last frame, like a browser
            following = 0
        self._frame = following
        self.refresh()
        self._schedule_frame()

    # -- upload ------------------------------------------------------------------------

    def _upload(self) -> None:
        engine = self._engine
        if engine is None or not engine.active:
            return
        cols, rows = self.content_size
        if not cols or not rows or cols > len(kitty.DIACRITICS) or rows > len(kitty.DIACRITICS):
            return
        cell_w, cell_h = engine.cell_pixels()
        area = max(1.0, cols * cell_w * rows * cell_h)
        frames_area = area * (len(self._frames) + 1)  # and the dimmed copy
        scale = min(1.0, math.sqrt(MAX_IMAGE_PIXELS / area), math.sqrt(MAX_ANIMATION_PIXELS / frames_area))
        target = (max(1, round(cols * cell_w * scale)), max(1, round(rows * cell_h * scale)))
        key = (cols, rows, target)
        if (key == self._uploaded and self._image_id is not None) or key == self._uploading:
            return
        cached = engine.image_cache.pop((self.cache_key, *key), None) if self.cache_key is not None else None
        if cached is not None and cached in engine.ids.live:
            if self._image_id is not None and self._image_id != cached:
                engine.send(kitty.delete(self._image_id))
                engine.ids.free(self._image_id)
            self._image_id, self._uploaded = cached, key
            engine.register_dimmable(cached, len(self._frames) + 1, len(self._frames) > 1, self)
            self.refresh()
            return
        if self._image_id is None:
            self._image_id = engine.ids.new()
        image_id = self._image_id
        self._uploading = key
        sources, gaps, plays, pixelated = list(self._frames), list(self._gaps), self.plays, self.pixelated
        factor = engine.dim_factor

        def dimmed(frame: np.ndarray) -> str:
            """A gapless copy of ``frame`` at scrim brightness, shown while a popup covers it."""
            dark = frame.copy()
            dark[..., :3] = (dark[..., :3].astype(np.float32) * factor + 0.5).astype(np.uint8)
            keys = dict(a="f", i=image_id, f=100, s=target[0], v=target[1], X=1, z=-1)
            return kitty.command(keys, kitty.png(dark))

        # Two steps, both off the app's thread: the first frame goes up straight away so
        # the picture appears at once, then the rest of an animation follows.
        def encode_first() -> str:
            first = fit(sources[0], target[0], target[1], pixelated)
            commands = kitty.transmit(image_id, first) + kitty.place_virtual(image_id, cols, rows)
            return commands + (dimmed(first) if len(sources) == 1 else "")

        def encode_rest() -> str:
            commands = []
            for k, source in enumerate(sources[1:], 1):
                frame = fit(source, target[0], target[1], pixelated)
                keys = dict(a="f", i=image_id, f=100, s=target[0], v=target[1], X=1, z=gaps[k % len(gaps)])
                commands.append(kitty.command(keys, kitty.png(frame)))
            commands.append(dimmed(fit(sources[0], target[0], target[1], pixelated)))
            # Start playing only once every frame is in (Ghostty 1.4 drops frames
            # sent to a playing animation). v=1 loops forever, v=n plays n-1 times.
            commands.append(kitty.animate(image_id, r=1, z=gaps[0]))
            commands.append(kitty.animate(image_id, s=3, v=1 if plays is None else plays + 1))
            return "".join(commands)

        def current() -> bool:
            """False if the widget was resized, unmounted or switched off meanwhile."""
            return self._uploading == key and self._image_id == image_id and self.glass_on

        def first_done(commands: str) -> None:
            if not current():
                return
            engine.send(commands)
            self._uploaded = key
            self.refresh()
            if len(sources) > 1:
                engine.background_job(encode_rest, rest_done)
            else:
                self._uploading = None
                engine.register_dimmable(image_id, 2, False, self)

        def rest_done(commands: str) -> None:
            if current():
                self._uploading = None
                engine.send(commands)
                engine.register_dimmable(image_id, len(sources) + 1, True, self)

        engine.background_job(encode_first, first_done)

    # -- rendering ---------------------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        cols, rows = self.content_size
        if self.glass_on:
            if self._image_id is None or self._uploaded is None or y >= len(kitty.DIACRITICS):
                return Strip.blank(cols)
            style = Style(color=kitty.id_color(self._image_id))
            return Strip([Segment(kitty.placeholder_row(y, cols), style)], cols)
        key = (cols, rows, self._frame)
        lines = self._lines.get(key)
        if lines is None:
            if len(self._lines) > 64:
                self._lines.clear()
            lines = self._lines[key] = half_block_lines(self._frames[self._frame], cols, rows, self.pixelated)
        return lines[y] if y < len(lines) else Strip.blank(cols)
