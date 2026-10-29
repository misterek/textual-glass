"""Kitty graphics protocol encoding. Pure functions, no Textual imports.

Every command carries ``q=2`` so the terminal never replies: Textual's input
parser does not understand APC sequences and would read a reply such as
``ESC _Gi=5;ENOENT ESC \\`` as key presses.

Payloads are chunked at 4096 base64 bytes. A chunked transfer is always built
as one string and written in one go, because while a chunked load is open,
Ghostty treats any graphics command as its continuation.
"""

from __future__ import annotations

import base64
import struct
import zlib
from collections.abc import Mapping, Sequence

import numpy as np

CHUNK = 4096
SYNC_START = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"

#: Everything below this z is drawn *under* cells that have an explicit
#: background colour (kitty spec; Ghostty 1.4 ``imageLayerFromZ``).
Z_BELOW_BACKGROUND = -1_073_741_824


def command(keys: Mapping[str, object], payload: bytes = b"") -> str:
    """One graphics command (chunked when the payload is large), with q=2."""
    keys = dict(keys)
    keys["q"] = 2
    control = ",".join(f"{k}={v}" for k, v in keys.items())
    if not payload:
        return f"\x1b_G{control}\x1b\\"
    data = base64.standard_b64encode(payload).decode("ascii")
    if len(data) <= CHUNK:
        return f"\x1b_G{control};{data}\x1b\\"
    # Continuation chunks only carry m (and a=f for frame loads, as kitty asks).
    rest = ("a=f," if keys.get("a") == "f" else "") + "q=2"
    out = []
    for i in range(0, len(data), CHUNK):
        more = int(i + CHUNK < len(data))
        head = control if i == 0 else rest
        out.append(f"\x1b_G{head},m={more};{data[i:i + CHUNK]}\x1b\\")
    return "".join(out)


def at(col: int, row: int, body: str) -> str:
    """Run ``body`` with the cursor at (col, row), then restore the cursor.

    ``a=p`` places at the cursor, so placements save the cursor (DECSC), move,
    place with C=1 (don't move the cursor) and restore it (DECRC).
    """
    return f"\x1b7\x1b[{row + 1};{col + 1}H{body}\x1b8"


# -- image data -------------------------------------------------------------


def png(pixels: np.ndarray, level: int = 2) -> bytes:
    """Encode an (h, w, 3|4) uint8 array as PNG with the Paeth filter.

    Smooth gradients compress several times better with a PNG row filter than
    raw RGB through zlib, and kitty/Ghostty decode PNG natively (f=100).
    """
    h, w, c = pixels.shape
    a = pixels.astype(np.int16)
    left = np.zeros_like(a)
    left[:, 1:] = a[:, :-1]
    up = np.zeros_like(a)
    up[1:] = a[:-1]
    upleft = np.zeros_like(a)
    upleft[1:, 1:] = a[:-1, :-1]
    p = left + up - upleft
    pa, pb, pc = np.abs(p - left), np.abs(p - up), np.abs(p - upleft)
    pred = np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, up, upleft))
    filtered = ((a - pred) & 0xFF).astype(np.uint8).reshape(h, w * c)
    raw = np.empty((h, w * c + 1), np.uint8)
    raw[:, 0] = 4  # Paeth
    raw[:, 1:] = filtered
    color_type = 6 if c == 4 else 2

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), level)) + chunk(b"IEND", b""))


def transmit(image_id: int, pixels: np.ndarray) -> str:
    """Upload a whole image (replaces the id, which deletes its placements)."""
    h, w = pixels.shape[:2]
    return command(dict(a="t", i=image_id, f=100, s=w, v=h), png(pixels))


def transmit_raw(image_id: int, width: int, height: int, rgba: bytes) -> str:
    """Upload raw RGBA bytes, zlib compressed (for small sprites and frames)."""
    return command(dict(a="t", i=image_id, f=32, s=width, v=height, o="z"), zlib.compress(rgba, 6))


def edit(image_id: int, x: int, y: int, pixels: np.ndarray) -> str:
    """Replace a rectangle of frame 1 in place (a=f, r=1, X=1). Placements stay."""
    h, w = pixels.shape[:2]
    keys: dict[str, object] = dict(a="f", i=image_id, r=1, f=100, s=w, v=h, X=1)
    if x or y:
        keys["x"], keys["y"] = x, y
    return command(keys, png(pixels))


def add_frame(image_id: int, width: int, height: int, rgba: bytes, gap_ms: int | None = None) -> str:
    """Append a frame to a stopped animation (a negative ``gap_ms`` makes it gapless)."""
    keys: dict[str, object] = dict(a="f", i=image_id, f=32, s=width, v=height, o="z", X=1)
    if gap_ms is not None:
        keys["z"] = gap_ms
    return command(keys, zlib.compress(rgba, 6))


def animation(image_id: int, width: int, height: int, frames: Sequence[bytes], gap_ms: int | Sequence[int],
              gapless: Sequence[bytes] = (), start: bool = True) -> str:
    """Upload a looping animation: all frames while it is stopped, then start it.

    Ghostty 1.4 could drop chunked a=f sent to an image that is playing, so the
    frames always go up first. ``gapless`` frames are appended with a negative
    gap: playback skips them, but ``a=a,c=N`` can show them (a dimmed copy).
    With ``start=False`` it stays stopped on frame 1, its gaps set: start it
    later with :func:`play`.
    """
    gaps = [gap_ms] * len(frames) if isinstance(gap_ms, int) else list(gap_ms)
    out = [transmit_raw(image_id, width, height, frames[0])]
    out += [add_frame(image_id, width, height, f, gaps[k]) for k, f in enumerate(frames[1:], 1)]
    out += [add_frame(image_id, width, height, f, -1) for f in gapless]
    out.append(animate(image_id, r=1, z=gaps[0]))
    if start:
        out.append(play(image_id))
    return "".join(out)


def play(image_id: int) -> str:
    """Loop an uploaded animation for ever (every frame must be up already: rule 2)."""
    return animate(image_id, s=3, v=1)


def frames_stopped(image_id: int, width: int, height: int, frames: Sequence[bytes]) -> str:
    """Upload frames that Python steps through itself with ``animate(c=k)``."""
    out = [transmit_raw(image_id, width, height, frames[0])]
    out += [add_frame(image_id, width, height, f) for f in frames[1:]]
    return "".join(out)


def animate(image_id: int, **keys: object) -> str:
    return command(dict(a="a", i=image_id, **keys))


# -- placements ---------------------------------------------------------------


def place(image_id: int, col: int, row: int, cols: int, rows: int, z: int,
          placement_id: int = 1, x_off: int = 0, y_off: int = 0) -> str:
    keys: dict[str, object] = dict(a="p", i=image_id, p=placement_id, c=max(1, cols), r=max(1, rows), z=z, C=1)
    if x_off:
        keys["X"] = x_off
    if y_off:
        keys["Y"] = y_off
    return at(col, row, command(keys))


def place_px(image_id: int, col: int, row: int, z: int, x_off: int = 0, y_off: int = 0,
             crop: tuple[int, int, int, int] | None = None, placement_id: int = 1) -> str:
    """Place an image at its own pixel size (no ``c``/``r``): at cell (col, row) plus
    ``x_off``/``y_off`` pixels, each less than the cell's size. ``crop`` shows only
    that source rectangle (x, y, width, height) of the image, at the same scale. A
    zero width or height means "to the image's edge", so never crop to nothing:
    unplace instead."""
    keys: dict[str, object] = dict(a="p", i=image_id, p=placement_id)
    if crop is not None:
        keys["x"], keys["y"], keys["w"], keys["h"] = crop
    if x_off:
        keys["X"] = x_off
    if y_off:
        keys["Y"] = y_off
    keys["z"], keys["C"] = z, 1
    return at(col, row, command(keys))


def place_virtual(image_id: int, cols: int, rows: int) -> str:
    """A virtual placement for Unicode placeholder cells (U=1)."""
    return command(dict(a="p", U=1, i=image_id, p=1, c=max(1, cols), r=max(1, rows)))


def unplace(image_id: int, placement_id: int | None = None) -> str:
    """Delete an image's placements (or only ``placement_id``) but keep its data."""
    if placement_id is None:
        return command(dict(a="d", d="i", i=image_id))
    return command(dict(a="d", d="i", i=image_id, p=placement_id))


def delete(image_id: int) -> str:
    """Delete an image and free its data."""
    return command(dict(a="d", d="I", i=image_id))


def delete_all() -> str:
    """Delete every visible placement and free the images left without one. Ghostty 1.4 keeps
    virtual placements (Unicode placeholders) and their images: use :func:`delete_range` for those."""
    return command(dict(a="d", d="A"))


def delete_range(first: int, last: int) -> str:
    """Delete every image with an id from ``first`` to ``last``, placeholders included, and free them."""
    return command(dict(a="d", d="R", x=first, y=last))


def set_default_background(hex_color: str) -> str:
    """OSC 11: the window padding then matches the backdrop."""
    return f"\x1b]11;{hex_color}\x07"


RESET_DEFAULT_BACKGROUND = "\x1b]111\x07"


# -- Unicode placeholders -------------------------------------------------------

PLACEHOLDER = "\U0010EEEE"

#: The first diacritics of kitty's rowcolumn-diacritics.txt: index n means row/column n.
DIACRITICS = [
    0x0305, 0x030D, 0x030E, 0x0310, 0x0312, 0x033D, 0x033E, 0x033F, 0x0346, 0x034A, 0x034B, 0x034C,
    0x0350, 0x0351, 0x0352, 0x0357, 0x035B, 0x0363, 0x0364, 0x0365, 0x0366, 0x0367, 0x0368, 0x0369,
    0x036A, 0x036B, 0x036C, 0x036D, 0x036E, 0x036F, 0x0483, 0x0484, 0x0485, 0x0486, 0x0487, 0x0592,
    0x0593, 0x0594, 0x0595, 0x0597, 0x0598, 0x0599, 0x059C, 0x059D, 0x059E, 0x059F, 0x05A0, 0x05A1,
    0x05A8, 0x05A9, 0x05AB, 0x05AC, 0x05AF, 0x05C4, 0x0610, 0x0611, 0x0612, 0x0613, 0x0614, 0x0615,
    0x0616, 0x0617, 0x0657, 0x0658, 0x0659, 0x065A, 0x065B, 0x065D, 0x065E, 0x06D6, 0x06D7, 0x06D8,
    0x06D9, 0x06DA, 0x06DB, 0x06DC, 0x06DF, 0x06E0, 0x06E1, 0x06E2, 0x06E4, 0x06E7, 0x06E8, 0x06EB,
    0x06EC, 0x0730, 0x0732, 0x0733, 0x0735, 0x0736, 0x073A, 0x073D, 0x073F, 0x0740, 0x0741, 0x0743,
    0x0745, 0x0747, 0x0749, 0x074A, 0x07EB, 0x07EC, 0x07ED, 0x07EE, 0x07EF, 0x07F0, 0x07F1, 0x07F3,
    0x0816, 0x0817, 0x0818, 0x0819, 0x081B, 0x081C, 0x081D, 0x081E, 0x081F, 0x0820, 0x0821, 0x0822,
    0x0823, 0x0825, 0x0826, 0x0827, 0x0829, 0x082A, 0x082B, 0x082C, 0x082D, 0x0951, 0x0953, 0x0954,
    0x0F82, 0x0F83, 0x0F86, 0x0F87, 0x135D, 0x135E, 0x135F, 0x17DD, 0x193A, 0x1A17, 0x1A75, 0x1A76,
    0x1A77, 0x1A78, 0x1A79, 0x1A7A, 0x1A7B, 0x1A7C, 0x1B6B, 0x1B6D, 0x1B6E, 0x1B6F, 0x1B70, 0x1B71,
    0x1B72, 0x1B73, 0x1CD0, 0x1CD1, 0x1CD2, 0x1CDA, 0x1CDB, 0x1CE0, 0x1DC0, 0x1DC1, 0x1DC3, 0x1DC4,
]


def placeholder_row(row: int, cols: int) -> str:
    """One row of placeholder cells, each with explicit row and column diacritics."""
    r = chr(DIACRITICS[row])
    return "".join(PLACEHOLDER + r + chr(DIACRITICS[c]) for c in range(cols))


def id_color(image_id: int) -> str:
    """The 24-bit foreground colour that carries an image id in placeholder cells."""
    return f"rgb({image_id >> 16 & 255},{image_id >> 8 & 255},{image_id & 255})"
