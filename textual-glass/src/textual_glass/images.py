"""Read image files into RGBA frames for :class:`~textual_glass.GlassImage`.

PNG, JPEG, GIF, WebP, APNG and anything else Pillow opens. Pillow is an
optional extra, so the theme itself stays light::

    uv add "textual-glass[images]"      # or: pip install "textual-glass[images]"
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import IO, Any, Union

import numpy as np

Source = Union[str, "os.PathLike[str]", bytes, IO[bytes]]  # noqa: UP007 - a runtime alias on 3.10

#: Frame delays shorter than this are shown at ``DEFAULT_GAP_MS``, as browsers do
#: (many GIFs say 0 or 10 ms and mean "default speed").
MIN_GAP_MS = 20
DEFAULT_GAP_MS = 100

INSTALL_HINT = (
    'Loading image files needs Pillow: uv add "textual-glass[images]" (or pip install "textual-glass[images]")'
)


@dataclass(frozen=True)
class LoadedImage:
    """Decoded frames, ready to hand to ``GlassImage``.

    Attributes:
        frames: (height, width, 4) uint8 RGBA arrays, each fully composited
            (GIF disposal and partial frames already applied).
        gaps_ms: How long each frame stays up.
        plays: How many times the animation runs; ``None`` means forever.
    """

    frames: list[np.ndarray]
    gaps_ms: list[int]
    plays: int | None

    @property
    def width(self) -> int:
        return int(self.frames[0].shape[1])

    @property
    def height(self) -> int:
        return int(self.frames[0].shape[0])

    @property
    def animated(self) -> bool:
        return len(self.frames) > 1


def _pillow() -> Any:
    try:
        from PIL import Image
    except ImportError as error:
        raise ImportError(INSTALL_HINT) from error
    return Image


def load_image(
    source: Source,
    *,
    max_size: tuple[int, int] | None = (1024, 1024),
    max_frames: int = 600,
) -> LoadedImage:
    """Decode an image file, or the bytes of one, into RGBA frames.

    Args:
        source: A path, raw file bytes, or a binary file object.
        max_size: Shrink frames to fit inside this many pixels (keeping the aspect
            ratio) so large photos don't cost memory twice. ``None`` keeps them.
        max_frames: Stop after this many frames of a long animation.

    Raises:
        ImportError: Pillow isn't installed.
        OSError: The file is missing or isn't an image Pillow understands.
    """
    Image = _pillow()
    from PIL import ImageOps, ImageSequence

    if isinstance(source, (bytes, bytearray, memoryview)):
        source = io.BytesIO(bytes(source))
    with Image.open(source) as image:
        loop = image.info.get("loop")  # GIF, APNG and WebP: 0 = forever, missing = play once
        frames: list[np.ndarray] = []
        gaps: list[int] = []
        for frame in ImageSequence.Iterator(image):
            if len(frames) >= max_frames:
                break
            rgba = ImageOps.exif_transpose(frame).convert("RGBA")  # phone photos carry their rotation in EXIF
            if max_size is not None:
                rgba.thumbnail(max_size, Image.Resampling.LANCZOS)
            frames.append(np.array(rgba, dtype=np.uint8))
            duration = frame.info.get("duration") or 0
            gaps.append(int(duration) if duration >= MIN_GAP_MS else DEFAULT_GAP_MS)
    if not frames:
        raise OSError("the image has no frames")
    # GIF's loop count is extra repeats after the first run.
    plays = None if loop == 0 else 1 if loop is None else int(loop) + 1
    return LoadedImage(frames, gaps, plays)
