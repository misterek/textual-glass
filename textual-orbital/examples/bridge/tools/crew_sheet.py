"""Contact sheets and GIFs of a crew pack (smooth or picture), for looking at the art without a terminal.

    uv run python tools/crew_sheet.py --out DIR [--pack PATH] [--members ash,bo] [--zoom 2]
    uv run python tools/crew_sheet.py --pack ~/my-crew --against smooth --out DIR --only sheets
    uv run python tools/crew_sheet.py --out DIR --prefix smooth_ --sizes small,retina --gif-size retina \
        --only sheets,gifs,bits

Writes to DIR (never inside the repo unless you say so), every name starting with ``--prefix``:

- ``sheet_<size>.png``: every member in every pose (first frame) at a station size, over dark navy. Sizes are
  ``WxH`` or a name: ``small`` (63x57, 7x3 cells at 9x19 px), ``medium`` (90x76, 10x4), ``large`` (126x114,
  14x6; 7x3 on Retina) and ``retina`` (180x152: 20x8, or 10x4 on Retina's 18x38 px cells; glass's biggest).
  With ``--against PACK``, each member's row is followed by the same member (by id) from that pack, so a
  picture pack exported from the smooth crew can be compared with the live one;
- ``sheet_light_90x76.png``: the same over a light background (does the art hold up without the dark?);
- ``frames_<member>.png``: every frame of every pose for one member, at 90x76;
- ``ensigns.png``: 0 to 3 ensigns, a few poses;
- ``chair_screen.png``: the captain's chair (waiting 0 to 3) and the viewscreen (calm, red alert);
- ``bits.png``: ensigns, the captain's chair (both pulse frames) and the viewscreen, calm and red alert, one to
  one at its true sizes (up to 1566 x 266 for a big Retina window);
- ``<member>_WxH.gif`` (or ``<member>.gif`` at ``--gif-size``): all poses of a member at their real timing;
- ``bridge.gif``: a little mock bridge (viewscreen, six stations, chair) animated.

Never opens a window; look at the files with an image viewer of your choice (or the Read tool).
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.crew import POSES, Pack, load  # noqa: E402

NAVY = (11, 18, 42)
LIGHT = (226, 231, 240)
INK = (170, 182, 210)
NAMED = {"small": (63, 57), "medium": (90, 76), "large": (126, 114), "retina": (180, 152)}
SIZES = tuple(NAMED.values())
#: The lamp colour per pose, roughly as the app might pass it.
LAMPS: dict[str, tuple[int, int, int]] = {
    "empty": (90, 100, 130), "seated": (90, 170, 255), "typing": (80, 220, 130), "done": (120, 200, 255),
    "hand_up": (255, 178, 56), "waving": (255, 178, 56), "standing": (255, 150, 40), "jolt": (255, 60, 80),
    "tea": (190, 140, 255), "dozing": (110, 120, 170),
}


def rgba(frame: bytes, w: int, h: int) -> Image.Image:
    return Image.frombuffer("RGBA", (w, h), frame, "raw", "RGBA", 0, 1)


def font() -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    return ImageFont.load_default()


def label_strip(width: int, text: str, bg: tuple[int, int, int], ink: tuple[int, int, int]) -> Image.Image:
    im = Image.new("RGB", (width, 14), bg)
    ImageDraw.Draw(im).text((2, 1), text, fill=ink, font=font())
    return im


def grid_sheet(cells: list[list[Image.Image | None]], row_labels: list[str], col_labels: list[str],
               cw: int, ch: int, bg: tuple[int, int, int], ink: tuple[int, int, int], pad: int = 6,
               zoom: int = 1) -> Image.Image:
    left, top = 60, 18
    w = left + len(col_labels) * (cw + pad) + pad
    h = top + len(row_labels) * (ch + pad) + pad
    sheet = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(sheet)
    for j, c in enumerate(col_labels):
        d.text((left + j * (cw + pad) + 2, 3), c, fill=ink, font=font())
    for i, r in enumerate(row_labels):
        d.text((4, top + i * (ch + pad) + ch // 2 - 6), r, fill=ink, font=font())
        for j, im in enumerate(cells[i]):
            x, y = left + j * (cw + pad), top + i * (ch + pad)
            # the cell box, faintly, so the transparent margin shows
            d.rectangle((x - 1, y - 1, x + cw, y + ch), outline=tuple(int(v * 0.8 + k * 0.2) for v, k in
                                                                       zip(bg, ink, strict=True)))
            if im is not None:
                sheet.paste(im, (x, y), im)
    if zoom > 1:
        sheet = sheet.resize((sheet.width * zoom, sheet.height * zoom), Image.Resampling.NEAREST)
    return sheet


def member_sheet(pack: Pack, w: int, h: int, bg: tuple[int, int, int], ink: tuple[int, int, int], members: list[str],
                 zoom: int, against: Pack | None = None) -> Image.Image:
    """Every member in every pose; with ``against``, each member's row is followed by that pack's member of the
    same id (where it has one)."""
    rows = []
    labels = []
    for mid in members:
        for source, label in ((pack, mid), (against, f"{mid} ({against.name})" if against else "")):
            if source is None or (source is against and mid not in {m.id for m in source.members}):
                continue
            row: list[Image.Image | None] = []
            for pose in POSES:
                frames, _ = source.station(mid, pose, LAMPS[pose], 0, w, h)
                row.append(rgba(frames[0], w, h))
            rows.append(row)
            labels.append(label)
    return grid_sheet(rows, labels, list(POSES), w, h, bg, ink, zoom=zoom)


def frames_sheet(pack: Pack, mid: str, w: int, h: int, zoom: int) -> Image.Image:
    rows: list[list[Image.Image | None]] = []
    labels = []
    most = 0
    for pose in POSES:
        frames, gap = pack.station(mid, pose, LAMPS[pose], 0, w, h)
        rows.append([rgba(f, w, h) for f in frames])
        labels.append(f"{pose} {gap}")
        most = max(most, len(frames))
    for r in rows:
        r.extend([None] * (most - len(r)))
    return grid_sheet(rows, labels, [str(i) for i in range(most)], w, h, NAVY, INK, zoom=zoom)


def ensign_sheet(pack: Pack, mid: str, w: int, h: int, zoom: int) -> Image.Image:
    poses = ("seated", "typing", "hand_up", "standing")
    rows: list[list[Image.Image | None]] = []
    for n in range(4):
        rows.append([rgba(pack.station(mid, p, LAMPS[p], n, w, h)[0][0], w, h) for p in poses])
    return grid_sheet(rows, [f"{n} ensigns" for n in range(4)], list(poses), w, h, NAVY, INK, zoom=zoom)


def chair_screen_sheet(pack: Pack, zoom: int) -> Image.Image:
    parts: list[list[Image.Image]] = []
    for cw, chh in ((90, 57), (90, 76), (160, 102)):
        row: list[Image.Image] = []
        for waiting in range(4):
            frames, _ = pack.chair(waiting, cw, chh)
            row.extend(rgba(f, cw, chh) for f in frames[:2])
        parts.append(row)
    pad = 6
    width = max(sum(im.width + pad for im in row) for row in parts) + pad
    screens = []
    for sw, sh in ((360, 57), (360, 76), (640, 136)):
        for alert in (False, True):
            frames, _ = pack.viewscreen(alert, sw, sh)
            screens.append([rgba(f, sw, sh) for f in frames[:2]])
    width = max(width, max(sum(im.width + pad for im in row) for row in screens) + pad)
    height = sum(max(im.height for im in row) + pad for row in parts + screens) + pad
    sheet = Image.new("RGB", (width, height), NAVY)
    y = pad
    for row in parts + screens:
        x = pad
        for im in row:
            sheet.paste(im, (x, y), im)
            ImageDraw.Draw(sheet).rectangle((x - 1, y - 1, x + im.width, y + im.height), outline=(30, 40, 70))
            x += im.width + pad
        y += max(im.height for im in row) + pad
    if zoom > 1:
        sheet = sheet.resize((sheet.width * zoom, sheet.height * zoom), Image.Resampling.NEAREST)
    return sheet


def bits_sheet(pack: Pack, mid: str) -> Image.Image:
    """Ensigns (0 to 3, a few poses), the captain's chair (waiting 0 to 3, both pulse frames) and the viewscreen
    (calm and red alert) at its true sizes, one to one: a 56 x 4 cell strip on 9 x 19 px cells (90 x 28) and on
    Retina's 18 x 38, and an 87 x 7 one on a big Retina window."""
    w, h = NAMED["large"]
    poses = ("seated", "typing", "hand_up")
    blocks: list[tuple[str, list[Image.Image]]] = []
    for n in range(4):
        blocks.append((f"{n} ensigns", [rgba(pack.station(mid, p, LAMPS[p], n, w, h)[0][0], w, h) for p in poses]))
    for cw, chh in (NAMED["small"], NAMED["large"], NAMED["retina"]):
        row: list[Image.Image] = []
        for waiting in range(4):
            frames, _ = pack.chair(waiting, cw, chh)
            row.extend(rgba(f, cw, chh) for f in frames[:2])
        blocks.append((f"captain's chair {cw}x{chh}: 0, 1, 2, 3 waiting (both pulse frames)", row))
    for cols, rows, (cw, ch), where in ((56, 4, (9, 19), "90x28 cells"), (56, 4, (18, 38), "90x28 on Retina"),
                                       (87, 7, (18, 38), "a big Retina window")):
        sw, sh = cols * cw, rows * ch
        for alert in (False, True):
            frames, gap = pack.viewscreen(alert, sw, sh)
            shown = frames[:2] if sw <= 600 else frames[:1]
            mood = "red alert" if alert else "calm"
            label = f"viewscreen {mood}, {sw}x{sh} ({where}), {len(frames)} frames at {gap} ms"
            blocks.append((label, [rgba(f, sw, sh) for f in shown]))
    pad = 8
    width = max(sum(im.width + pad for im in ims) for _, ims in blocks) + pad
    height = sum(14 + max(im.height for im in ims) + pad for _, ims in blocks) + pad
    sheet = Image.new("RGB", (width, height), NAVY)
    d = ImageDraw.Draw(sheet)
    y = pad
    for label, ims in blocks:
        d.text((pad, y), label, fill=INK, font=font())
        y += 14
        x = pad
        for im in ims:
            sheet.paste(im, (x, y), im)
            x += im.width + pad
        y += max(im.height for im in ims) + pad
    return sheet


def _size(text: str) -> tuple[str, tuple[int, int]]:
    if text in NAMED:
        return text, NAMED[text]
    w, _, h = text.partition("x")
    return text, (int(w), int(h))


def _at(frames: list[bytes], gap: int, t: int) -> bytes:
    return frames[(t // gap) % len(frames)]


def member_gif(pack: Pack, mid: str, w: int, h: int, path: Path, seconds: float = 6.0, tick: int = 20,
               ensigns: int = 0) -> None:
    anims = [pack.station(mid, p, LAMPS[p], ensigns, w, h) for p in POSES]
    cols = 5
    pad = 4
    rows = math.ceil(len(POSES) / cols)
    sw, sh = cols * (w + pad) + pad, rows * (h + pad + 12) + pad
    images, durations = [], []
    last = None
    for t in range(0, int(seconds * 1000), tick):
        sheet = Image.new("RGB", (sw, sh), NAVY)
        d = ImageDraw.Draw(sheet)
        for k, (pose, (frames, gap)) in enumerate(zip(POSES, anims, strict=True)):
            x, y = pad + (k % cols) * (w + pad), pad + (k // cols) * (h + pad + 12)
            d.text((x + 2, y), pose, fill=INK, font=font())
            im = rgba(_at(frames, gap, t), w, h)
            sheet.paste(im, (x, y + 12), im)
        data = sheet.tobytes()
        if data == last:
            durations[-1] += tick
            continue
        last = data
        images.append(sheet)
        durations.append(tick)
    save_gif(images, durations, path)


def save_gif(images: list[Image.Image], durations: list[int], path: Path) -> None:
    """Every frame in one shared palette, undithered: what doesn't move stays the same bytes, so the GIF only
    carries what changes (a palette and dithering per frame made each frame new: megabytes for smooth art)."""
    step = max(1, len(images) // 12)
    sample = images[::step][:12]
    mosaic = Image.new("RGB", (sample[0].width, sample[0].height * len(sample)))
    for k, im in enumerate(sample):
        mosaic.paste(im, (0, k * im.height))
    palette = mosaic.quantize(colors=255, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    frames = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=False)


def bridge_gif(pack: Pack, path: Path, seconds: float = 6.0, tick: int = 20) -> None:
    """A mock bridge at 9x19 px cells: a viewscreen, six stations in two rows, the chair."""
    cw, ch = 9, 19
    cols = 3 * 11 + 1
    screen = pack.viewscreen(True, cols * cw, 4 * ch)
    poses = ["typing", "hand_up", "done", "waving", "jolt", "tea"]
    stations = [pack.station(m.id, p, LAMPS[p], (1, 0, 2, 0, 0, 3)[i], 10 * cw, 4 * ch)
                for i, (m, p) in enumerate(zip(pack.members, poses, strict=False))]
    chair = pack.chair(2, 12 * cw, 3 * ch)
    W, H = cols * cw, (4 + 1 + 4 + 1 + 4 + 1 + 3) * ch
    images, durations, last = [], [], None
    for t in range(0, int(seconds * 1000), tick):
        im = Image.new("RGB", (W, H), NAVY)
        s = rgba(_at(*screen, t), cols * cw, 4 * ch)
        im.paste(s, (0, 0), s)
        for i, st in enumerate(stations):
            x = (1 + (i % 3) * 11) * cw
            y = (5 + (i // 3) * 5) * ch
            f = rgba(_at(*st, t), 10 * cw, 4 * ch)
            im.paste(f, (x, y), f)
            ImageDraw.Draw(im).text((x + 2, y + 4 * ch + 2), pack.members[i].name, fill=pack.members[i].uniform,
                                    font=font())
        c = rgba(_at(*chair, t), 12 * cw, 3 * ch)
        im.paste(c, ((cols - 12) // 2 * cw, 15 * ch), c)
        data = im.tobytes()
        if data == last:
            durations[-1] += tick
            continue
        last = data
        images.append(im)
        durations.append(tick)
    save_gif(images, durations, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--pack", default=None, help="a pack folder (smooth or picture), or smooth (the default)")
    ap.add_argument("--against", default=None, help="another pack to show under each member's row in the sheets")
    ap.add_argument("--members", default="", help="comma-separated member ids for the GIFs (default: first two)")
    ap.add_argument("--zoom", type=int, default=1, help="scale the sheets up (nearest neighbour) for close looks")
    ap.add_argument("--only", default="",
                    help="comma-separated: sheets, light, frames, ensigns, chair, bits, gifs, bridge")
    ap.add_argument("--prefix", default="", help="put this in front of every file name (like smooth_)")
    ap.add_argument("--sizes", default="", help="station sizes for the sheets: small, medium, large, retina, WxH")
    ap.add_argument("--gif-size", default="", help="one size for the GIFs, named <prefix><member>.gif")
    args = ap.parse_args(argv)
    pack = load(args.pack)
    against = load(args.against) if args.against else None
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    ids = [m.id for m in pack.members]
    gif_ids = [m for m in args.members.split(",") if m] or ids[:2]
    only = set(filter(None, args.only.split(","))) or {"sheets", "light", "frames", "ensigns", "chair", "bits",
                                                        "gifs", "bridge"}
    pre = args.prefix
    sizes = [_size(t) for t in args.sizes.split(",") if t] or [(label, size) for label, size in NAMED.items()]
    written = []
    if "sheets" in only:
        for label, (w, h) in sizes:
            p = out / f"{pre}sheet_{label}.png"
            member_sheet(pack, w, h, NAVY, INK, ids, args.zoom, against).save(p)
            written.append(p)
    if "light" in only:
        p = out / f"{pre}sheet_light_90x76.png"
        member_sheet(pack, 90, 76, LIGHT, (60, 70, 90), ids, args.zoom, against).save(p)
        written.append(p)
    if "frames" in only:
        for mid in gif_ids:
            p = out / f"{pre}frames_{mid}.png"
            frames_sheet(pack, mid, 90, 76, args.zoom).save(p)
            written.append(p)
    if "ensigns" in only:
        p = out / f"{pre}ensigns.png"
        ensign_sheet(pack, ids[0], 90, 76, args.zoom).save(p)
        written.append(p)
    if "chair" in only:
        p = out / f"{pre}chair_screen.png"
        chair_screen_sheet(pack, args.zoom).save(p)
        written.append(p)
    if "bits" in only:
        p = out / f"{pre}bits.png"
        bits_sheet(pack, ids[0]).save(p)
        written.append(p)
    if "gifs" in only:
        for mid in gif_ids:
            if args.gif_size:
                w, h = _size(args.gif_size)[1]
                p = out / f"{pre}{mid}.gif"
                member_gif(pack, mid, w, h, p)
                written.append(p)
                continue
            for w, h in ((90, 76), (160, 136)):
                p = out / f"{pre}{mid}_{w}x{h}.gif"
                member_gif(pack, mid, w, h, p)
                written.append(p)
    if "bridge" in only:
        p = out / f"{pre}bridge.gif"
        bridge_gif(pack, p)
        written.append(p)
    for p in written:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
