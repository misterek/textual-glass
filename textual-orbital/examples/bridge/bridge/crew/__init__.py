"""Crew packs: the art of the bridge. No Textual imports.

THE CONTRACT (the app uses only these names; the art behind them may change freely):

- ``POSES``: every pose a station can show.
- ``Member``: one character in a pack (``id``, ``name``, ``uniform``: the identity colour, also used
  for the station's label).
- ``Pack``: ``name``, ``style``, ``members`` (roster order), and three sprite makers. Each returns
  ``(frames, gap_ms)``: 1 to 8 straight-alpha RGBA frames as bytes, ``width * height * 4`` each, looped by the
  terminal (one frame is a still). **Every frame's outermost pixel ring is fully transparent** (glass
  rule 17). Same arguments, same bytes.

  - ``station(member_id, pose, lamp, ensigns, width, height)``: a console with its chair and, unless
    ``member_id`` is None (pose ``"empty"``), the crew member sitting or standing at it in ``pose``.
    ``lamp`` is the console lamp's colour (RGB). ``ensigns`` (0 to 3) little helpers stand beside the
    console: the session's running subagents.
  - ``viewscreen(alert, width, height)``: the big screen at the front of the bridge, at its true size (up
    to about 1600 x 270 px on a big Retina window). ``alert`` tints it for red alert. A pack's viewscreen
    has the same number of frames and the same gap at every size, so tiles cut from one
    ``(alert, width, height)`` picture and played as separate terminal animations stay frame-aligned. Only
    the whole picture's outermost ring is transparent: a tile cut from it is opaque at its cut edges.
  - ``chair(waiting, width, height)``: the captain's chair (the user). ``waiting`` is how many crew
    members are waiting on the user.

- ``load(path=None) -> Pack``: the built-in crew (the smooth one) for None or ``"smooth"``, otherwise the pack
  in that folder. A broken pack raises ``PackError`` (a ``ValueError``) at ``load()``, naming the file and the
  problem.

The poses:

| Pose | Session state |
| --- | --- |
| ``empty`` | no session at this station |
| ``seated`` | just started or resumed, nothing asked yet |
| ``typing`` | working |
| ``done`` | finished its turn: your move (leaning back) |
| ``hand_up`` | waiting on you (a permission, a question), the first couple of minutes |
| ``waving`` | still waiting, a few minutes on |
| ``standing`` | still waiting, a long time: standing and turned toward the chair |
| ``jolt`` | the turn died on an error (the console sparks) |
| ``tea`` | compacting its context |
| ``dozing`` | idle a long time |

A pack has one of two styles, read from its ``pack.json`` (``PACKS.md`` in the bridge folder documents both):

- **smooth** (``bridge/crew/smooth/``, the built-in crew): every sprite drawn at the exact pixel size asked
  for, from anti-aliased signed-distance shapes, softly shaded and lit by the console lamp. Members are
  described by parameters (colours, a hair style, extras, a build). A pack of your own usually extends it.
- **picture**: your own PNGs, one per pose (numbered frames for animation), one folder per member. The
  renderer draws the station round them, the smooth style's console, chair, lamp, ensigns and effects, scales
  them to the station's real size and lights them with the lamp. ``tools/export_pictures.py`` writes a
  starter pack from the smooth crew to paint over.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from . import _picture, _smooth
from ._pack import POSES, STYLES, Member, PackError, read_manifest
from ._picture import PictureData, PictureMember
from ._smooth import SmoothData, SmoothMember

RGB = tuple[int, int, int]
Frames = tuple[list[bytes], int]

__all__ = ["POSES", "RGB", "STYLES", "Frames", "Member", "Pack", "PackError", "load"]

_CACHE_SIZE = 192


class Pack:
    """A crew pack: its members and the three sprite makers. ``Pack()`` is the built-in (smooth) crew;
    ``load(path)`` gives the pack in a folder. ``style`` is ``"smooth"`` or ``"picture"``."""

    name: str
    members: tuple[Member, ...]
    style: str

    def __init__(self, data: SmoothData | PictureData | None = None) -> None:
        self._data = data if data is not None else _smooth.load_smooth(None)
        self.name = self._data.name
        self.style = _picture.STYLE if isinstance(self._data, PictureData) else _smooth.STYLE
        self.members = tuple(m.member for m in self._data.members)
        self._art: dict[str, SmoothMember | PictureMember] = {m.member.id: m for m in self._data.members}
        self._cache: OrderedDict[tuple[object, ...], Frames] = OrderedDict()
        self._lock = threading.Lock()  # the sprite makers may be called from a worker thread

    def __repr__(self) -> str:
        return f"<Pack {self.name!r} ({self.style}): {', '.join(m.id for m in self.members)}>"

    def member(self, member_id: str) -> Member:
        art = self._art.get(member_id)
        if art is None:
            raise KeyError(member_id)
        return art.member

    def _cached(self, key: tuple[object, ...], make: Callable[[], Frames]) -> Frames:
        """``make()``, remembered for the most recent few hundred calls (renders are pure)."""
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit[0][:], hit[1]
        value = make()
        with self._lock:
            self._cache[key] = value
            if len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)
        return value[0][:], value[1]

    def station(self, member_id: str | None, pose: str, lamp: RGB, ensigns: int, width: int,
                height: int) -> Frames:
        if pose not in POSES:
            raise ValueError(f"unknown pose {pose!r} (poses: {', '.join(POSES)})")
        art = None if member_id is None else self._art.get(member_id)
        if member_id is not None and art is None:
            raise KeyError(member_id)
        if art is None or pose == "empty":
            art, member_id, pose = None, None, "empty"
        lamp = (int(lamp[0]), int(lamp[1]), int(lamp[2]))
        count = min(max(0, int(ensigns)), 3) if art is not None else 0
        w, h = int(width), int(height)
        key = ("station", member_id, pose, lamp, count, w, h)
        data = self._data
        if isinstance(data, PictureData):
            picture = art if isinstance(art, PictureMember) else None
            return self._cached(key, lambda: _picture.station(data, picture, pose, lamp, count, w, h))
        smooth = art if isinstance(art, SmoothMember) else None
        return self._cached(key, lambda: _smooth.station(data, smooth, pose, lamp, count, w, h))

    def viewscreen(self, alert: bool, width: int, height: int) -> Frames:
        data = self._data
        return self._cached(("viewscreen", bool(alert), int(width), int(height)),
                            lambda: _smooth.viewscreen(data, bool(alert), int(width), int(height)))

    def chair(self, waiting: int, width: int, height: int) -> Frames:
        count = min(max(0, int(waiting)), 3)
        data = self._data
        return self._cached(("chair", count, int(width), int(height)),
                            lambda: _smooth.chair(data, count, int(width), int(height)))


#: The built-in crew, by name.
BUILT_IN = (_smooth.STYLE,)


@lru_cache(maxsize=1)
def _built_in() -> Pack:
    return Pack(_smooth.load_smooth(None))


def load(path: str | Path | None = None) -> Pack:
    """The built-in crew (None or ``"smooth"``; loaded once), or the pack in a folder: its ``pack.json`` says
    the style (``"smooth"`` or ``"picture"``). The built-in name wins over a folder of the same name in the
    current directory (say ``./smooth`` for such a folder)."""
    if path is None or (isinstance(path, str) and path in BUILT_IN):
        return _built_in()
    folder = Path(path).expanduser()
    manifest, m = read_manifest(folder)
    style = manifest.get("style")
    if style == _smooth.STYLE:
        return Pack(_smooth.load_smooth(folder))
    if style == _picture.STYLE:
        return Pack(_picture.load_picture(folder))
    raise PackError(f"{m}: style should be {' or '.join(repr(s) for s in STYLES)}, not {style!r}")
