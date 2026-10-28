"""A small model of a kitty-graphics terminal (Ghostty 1.4 semantics) for tests.

``KittyModel`` applies a=t/f/a/p/d/q commands to an image store and records
protocol mistakes. ``composite`` draws an approximate screenshot from a pyte
screen plus the model, in Ghostty's pass order (``renderer/generic.zig``):

1. the default background colour,
2. images with z < -1073741824 ("below background"),
3. cell backgrounds, only for cells with an explicit (non-default) colour,
4. images with -1073741824 <= z < 0, and placeholder runs (always z=-1,
   ties broken by image id),
5. text,
6. images with z >= 0.
"""

from __future__ import annotations

import base64
import io
import re
import zlib
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

CHUNK_KEYS = {"m", "q", "a"}
Z_BELOW_BG = -1_073_741_824
PLACEHOLDER = "\U0010EEEE"
NAMED = {
    "black": "000000", "red": "cd3131", "green": "0dbc79", "brown": "e5e510", "yellow": "e5e510",
    "blue": "2472c8", "magenta": "bc3fbc", "cyan": "11a8cd", "white": "e5e5e5",
    "brightblack": "666666", "brightred": "f14c4c", "brightgreen": "23d18b", "brightyellow": "f5f543",
    "brightblue": "3b8eea", "brightmagenta": "d670d6", "brightcyan": "29b8db", "brightwhite": "ffffff",
}


class KittyModel:
    def __init__(self) -> None:
        self.images: dict[int, dict] = {}
        self.placements: dict[tuple[int, int], dict] = {}
        self.errors: list[str] = []
        self.pending: tuple[dict, bytes] | None = None
        self.cmds = 0
        self.counts: dict[str, int] = {}
        self.no_quiet = 0
        self.queries = 0
        self.edits = 0
        self.edit_bytes = 0
        self.upload_bytes = 0
        self.payload_bytes = 0
        #: Which screen the terminal shows, when the caller tracks it (``Replay`` does): None, "alt" or
        #: "primary". Ghostty keeps one image store per screen; glass only ever draws on the alternate one.
        self.screen: str | None = None

    def alt_screen(self, on: bool) -> None:
        """Mode 1049 switched. Entering the alternate screen clears it, which in Ghostty 1.4 runs the
        same delete as ``a=d,d=A`` (``Terminal.zig`` ``eraseDisplay(.complete)``): visible placements and
        unused images go, images behind placeholders (virtual placements) stay."""
        self.screen = "alt" if on else "primary"
        if on:
            self._delete_visible(free=True)

    def _delete_visible(self, free: bool) -> None:
        self.placements = {key: p for key, p in self.placements.items() if p["virtual"]}
        if free:
            used = {iid for iid, _ in self.placements}
            self.images = {iid: img for iid, img in self.images.items() if iid in used}

    def err(self, msg: str, keys: dict) -> None:
        if len(self.errors) < 200:
            self.errors.append(f"{msg} :: {dict(list(keys.items())[:10])}")

    def apc(self, body: bytes, cursor: tuple[int, int]) -> None:
        control, _, payload = body.partition(b";")
        keys: dict[str, str] = {}
        for kv in control.split(b","):
            if kv:
                k, _, v = kv.partition(b"=")
                keys[k.decode()] = v.decode()
        if keys.get("q") != "2":
            self.no_quiet += 1
        self.payload_bytes += len(payload)
        if self.pending is not None:
            first, data = self.pending
            if set(keys) - CHUNK_KEYS:
                self.err("graphics command interleaved into an open chunked transfer", keys)
            data += payload
            if keys.get("m") == "1":
                self.pending = (first, data)
                return
            self.pending = None
            self._run(first, data, cursor)
            return
        if keys.get("m") == "1":
            if len(payload) % 4:
                self.err("chunk not a multiple of 4", keys)
            self.pending = (keys, payload)
            return
        self._run(keys, payload, cursor)

    def _decode(self, k: dict, payload: bytes) -> tuple[np.ndarray | None, int, int]:
        data = base64.b64decode(payload, validate=True)
        fmt = int(k.get("f", 32))
        if k.get("o") == "z":
            data = zlib.decompress(data)
        if fmt == 100:
            image = Image.open(io.BytesIO(data))
            image.load()
            arr = np.array(image.convert("RGBA"), np.uint8)
            return arr, arr.shape[1], arr.shape[0]
        w, h = int(k.get("s", 0)), int(k.get("v", 0))
        bpp = 4 if fmt == 32 else 3
        if len(data) != w * h * bpp:
            raise ValueError(f"bad size {len(data)} vs {w}x{h}x{bpp}")
        arr = np.frombuffer(data, np.uint8).reshape(h, w, bpp)
        if bpp == 3:
            arr = np.concatenate([arr, np.full((h, w, 1), 255, np.uint8)], 2)
        return arr.copy(), w, h

    def _run(self, k: dict, payload: bytes, cursor: tuple[int, int]) -> None:
        self.cmds += 1
        a = k.get("a", "t")
        self.counts[a] = self.counts.get(a, 0) + 1

        def gi(key: str, d: int = 0) -> int:
            return int(k.get(key, d))

        if a == "q":
            self.queries += 1
            return
        if self.screen == "primary":
            self.err("graphics on the primary screen (after leaving the alternate screen)", k)
        iid = gi("i")
        pixels = None
        if payload and a in ("t", "T", "f"):
            try:
                pixels, w, h = self._decode(k, payload)
            except Exception as error:  # noqa: BLE001
                self.err(f"bad payload: {error}", k)
                return
        if a in ("t", "T"):
            if pixels is None:
                self.err("transmit without data", k)
                return
            self.upload_bytes += len(payload)
            # retransmitting replaces the image and deletes its placements
            self.placements = {key: p for key, p in self.placements.items() if key[0] != iid}
            self.images[iid] = dict(w=pixels.shape[1], h=pixels.shape[0], frames=[pixels], gaps=[0], state=1, cur=0)
            if a == "T":
                self._place(k, iid, cursor)
            return
        if a == "d":
            d = k.get("d", "a")
            if d in ("i", "I"):
                self.placements = {key: p for key, p in self.placements.items() if key[0] != iid}
                if d == "I":
                    self.images.pop(iid, None)
            elif d in ("a", "A"):  # Ghostty 1.4: visible placements only (graphics_storage.zig)
                self._delete_visible(free=d == "A")
            elif d in ("r", "R"):  # every placement of the ids in x..y, virtual ones included
                first, last = gi("x"), gi("y")
                if not 0 < first <= last:
                    self.err("EINVAL bad id range", k)
                    return
                self.placements = {key: p for key, p in self.placements.items() if not first <= key[0] <= last}
                if d == "R":
                    self.images = {i: img for i, img in self.images.items() if not first <= i <= last}
            else:
                self.err("unexpected delete mode", k)
            return
        img = self.images.get(iid)
        if img is None:
            self.err("ENOENT image not found", k)
            return
        if a == "f":
            if pixels is None:
                self.err("frame without data", k)
                return
            h, w = pixels.shape[:2]
            x, y = gi("x"), gi("y")
            if w > img["w"] or h > img["h"]:
                self.err("EINVAL frame dimensions exceed image", k)
                return
            if x + w > img["w"] or y + h > img["h"]:
                self.err("frame rect runs off the image (clipped)", k)
            r = gi("r")
            if r and r <= len(img["frames"]):
                frame = img["frames"][r - 1]
                self.edits += 1
                self.edit_bytes += len(payload)
                if gi("z"):  # an edit with z sets the frame's gap; negative makes it gapless
                    img["gaps"][r - 1] = gi("z")
            else:
                if img["state"] == 3:
                    self.err("frame added while the animation is playing (Ghostty 1.4 race)", k)
                # Ghostty 1.4 (graphics_exec.zig): a new frame starts as a copy of
                # frame c=, or the background colour Y= (default transparent black)
                base = gi("c")
                if base and base <= len(img["frames"]):
                    frame = img["frames"][base - 1].copy()
                else:
                    bg = gi("Y")
                    frame = np.zeros((img["h"], img["w"], 4), np.uint8)
                    frame[:] = [(bg >> 24) & 255, (bg >> 16) & 255, (bg >> 8) & 255, bg & 255]
                img["frames"].append(frame)
                img["gaps"].append(gi("z", 40))
                self.upload_bytes += len(payload)
            x1, y1 = min(img["w"], x + w), min(img["h"], y + h)
            src = pixels[: y1 - y, : x1 - x]
            if gi("X") == 1:  # composition mode 1: overwrite
                frame[y:y1, x:x1] = src
            else:  # Ghostty's default: alpha blend, source over (graphics_pixel.zig)
                frame[y:y1, x:x1] = _src_over(frame[y:y1, x:x1], src)
        elif a == "a":
            n = len(img["frames"])
            if "c" in k:
                if not 1 <= gi("c") <= n:
                    self.err("current frame out of range", k)
                else:
                    img["cur"] = gi("c") - 1
            if "r" in k and "z" in k and 1 <= gi("r") <= n:
                img["gaps"][gi("r") - 1] = gi("z")
            if "v" in k:
                img["loops"] = gi("v")
            if "s" in k:
                img["state"] = gi("s")
        elif a == "p":
            self._place(k, iid, cursor)
        else:
            self.err("unknown action", k)

    def _place(self, k: dict, iid: int, cursor: tuple[int, int]) -> None:

        def gi(key: str, d: int = 0) -> int:
            return int(k.get(key, d))

        virtual = gi("U") == 1
        p = dict(c=gi("c"), r=gi("r"), z=gi("z"), X=gi("X"), Y=gi("Y"), virtual=virtual,
                 col=cursor[0], row=cursor[1])
        if k.get("C") != "1" and not virtual:
            self.err("placement moves the cursor (no C=1)", k)
        self.placements[(iid, gi("p"))] = p


def _src_over(dst: np.ndarray, src: np.ndarray) -> np.ndarray:
    """Straight-alpha RGBA source-over, as Ghostty composes frames without X=1."""
    d, sr = dst.astype(np.float32) / 255, src.astype(np.float32) / 255
    sa, da = sr[..., 3:], d[..., 3:]
    out_a = sa + da * (1 - sa)
    out_rgb = (sr[..., :3] * sa + d[..., :3] * da * (1 - sa)) / np.maximum(out_a, 1e-6)
    return (np.clip(np.concatenate([out_rgb, out_a], axis=2), 0, 1) * 255 + 0.5).astype(np.uint8)


# ============================================================ compositing ==

_FONTS: dict[tuple[int, bool], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    key = (size, bold)
    if key not in _FONTS:
        try:
            _FONTS[key] = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size, index=1 if bold else 0)
        except OSError:
            _FONTS[key] = ImageFont.load_default()
    return _FONTS[key]


def _hex(value: str, default: str) -> np.ndarray:
    v = value if re.fullmatch("[0-9a-fA-F]{6}", value or "") else NAMED.get(value, default)
    return np.array([int(v[i:i + 2], 16) for i in (0, 2, 4)], np.float32) / 255


def diacritic_index(ch: str, table: list[int]) -> int:
    try:
        return table.index(ord(ch))
    except ValueError:
        return 0


def composite(screen: Any, model: KittyModel, cw: int, ch: int, default_bg: str = "1d1f21",
              default_fg: str = "c5c8c6", diacritics: list[int] | None = None) -> Image.Image:
    cols, rows = screen.columns, screen.lines
    W, H = cols * cw, rows * ch
    canvas = np.zeros((H, W, 3), np.float32)
    canvas[:] = _hex(default_bg, "1d1f21")
    buffer = screen.buffer

    placeholder_cells: dict[tuple[int, int], tuple[int, str]] = {}
    for y in range(rows):
        line = buffer[y]
        for x in range(cols):
            c = line[x]
            if c.data and c.data[0] == PLACEHOLDER:
                iid = int(c.fg, 16) if re.fullmatch("[0-9a-fA-F]{6}", c.fg) else -1
                placeholder_cells[(x, y)] = (iid, c.data[1:])

    def draw(img: dict, frame: np.ndarray, dx: int, dy: int, dw: int, dh: int,
             sx0: float = 0, sy0: float = 0, sw: float | None = None, sh: float | None = None) -> None:
        sw, sh = sw or img["w"], sh or img["h"]
        if dw <= 0 or dh <= 0:
            return
        arr = frame.astype(np.float32) / 255
        ys = (sy0 + (np.arange(dh) + 0.5) * sh / dh).astype(int)
        xs = (sx0 + (np.arange(dw) + 0.5) * sw / dw).astype(int)
        inside = ((ys >= 0) & (ys < img["h"]))[:, None] & ((xs >= 0) & (xs < img["w"]))[None, :]
        src = arr[ys.clip(0, img["h"] - 1)][:, xs.clip(0, img["w"] - 1)] * inside[..., None]
        x0, y0, x1, y1 = max(0, dx), max(0, dy), min(W, dx + dw), min(H, dy + dh)
        if x1 <= x0 or y1 <= y0:
            return
        s = src[y0 - dy:y1 - dy, x0 - dx:x1 - dx]
        a = s[..., 3:]
        canvas[y0:y1, x0:x1] = canvas[y0:y1, x0:x1] * (1 - a) + s[..., :3] * a

    def draw_placement(iid: int, p: dict) -> None:
        img = model.images.get(iid)
        if img is None:
            return
        frame = img["frames"][img["cur"]]
        if p["virtual"]:
            gw, gh = p["c"] * cw, p["r"] * ch
            s = min(gw / img["w"], gh / img["h"])
            ox, oy = (gw - img["w"] * s) / 2, (gh - img["h"] * s) / 2
            for (x, y), (cid, marks) in placeholder_cells.items():
                if cid == iid and len(marks) >= 2 and diacritics:
                    r_, c_ = diacritic_index(marks[0], diacritics), diacritic_index(marks[1], diacritics)
                    draw(img, frame, x * cw, y * ch, cw, ch, (c_ * cw - ox) / s, (r_ * ch - oy) / s, cw / s, ch / s)
        else:
            draw(img, frame, p["col"] * cw + p["X"], p["row"] * ch + p["Y"], p["c"] * cw, p["r"] * ch)

    def order(kv: tuple[tuple[int, int], dict]) -> tuple[int, int]:
        (iid, _), p = kv
        return (-1 if p["virtual"] else p["z"], iid)

    ordered = sorted(model.placements.items(), key=order)
    below_bg = [kv for kv in ordered if not kv[1]["virtual"] and kv[1]["z"] < Z_BELOW_BG]
    below_text = [kv for kv in ordered if kv[1]["virtual"] or Z_BELOW_BG <= kv[1]["z"] < 0]
    above = [kv for kv in ordered if not kv[1]["virtual"] and kv[1]["z"] >= 0]

    for (iid, _), p in below_bg:
        draw_placement(iid, p)
    # cell backgrounds: only explicit colours are opaque
    for y in range(rows):
        line = buffer[y]
        for x in range(cols):
            c = line[x]
            bg = c.fg if c.reverse else c.bg
            if c.reverse and bg == "default":
                bg = default_fg
            if bg != "default":
                canvas[y * ch:(y + 1) * ch, x * cw:(x + 1) * cw] = _hex(bg, default_bg)
    for (iid, _), p in below_text:
        draw_placement(iid, p)
    image = Image.fromarray((np.clip(canvas, 0, 1) * 255).astype(np.uint8), "RGB")
    pen = ImageDraw.Draw(image)
    size = max(8, int(ch * 0.72))
    for y in range(rows):
        line = buffer[y]
        for x in range(cols):
            c = line[x]
            text = c.data
            if not text or text == " " or text[0] == PLACEHOLDER:
                continue
            fg = c.bg if c.reverse else c.fg
            if c.reverse and fg == "default":
                fg = default_bg
            rgb = tuple(int(v * 255) for v in _hex(fg, default_fg))
            pen.text((x * cw, y * ch + (ch - size) * 0.35), text[0], fill=rgb, font=_font(size, c.bold))
            if c.underscore:
                pen.line([(x * cw, (y + 1) * ch - 2), ((x + 1) * cw, (y + 1) * ch - 2)], fill=rgb)
    if above:
        canvas = np.asarray(image, np.float32) / 255
        for (iid, _), p in above:
            draw_placement(iid, p)
        image = Image.fromarray((np.clip(canvas, 0, 1) * 255).astype(np.uint8), "RGB")
    return image
