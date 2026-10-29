"""Contact sheets of the flourishes: every few frames of each, over a nebula
backdrop with the anchor and target outlined, plus an animated GIF at the real
timing. Approximate, like every composite here: no Ghostty, no text.

    uv run python textual-glass/tests/flourish_sheet.py --out /tmp/fl            # every kind
    uv run python textual-glass/tests/flourish_sheet.py comet orbit --out /tmp/fl --crop
    uv run python textual-glass/tests/flourish_sheet.py --light --screen 70x22 --out /tmp/fl

For each kind it prints the frame count, the duration, the render time, the
compressed upload size and whether every frame is transparent at its border
(rule 17, apart from sides on the window's edge).
"""

from __future__ import annotations

import argparse
import time
import zlib
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from textual_glass import backdrop
from textual_glass.flourishes import FLOURISHES, make_stage
from textual_glass.palette import Palette, rgb

NAVY = Palette(dark=True, background=rgb("#070b18"), surface=rgb("#0d1430"), panel=rgb("#16203d"),
               foreground=rgb("#c9d6f2"), primary=rgb("#38e1ff"), secondary=rgb("#a46bff"), accent=rgb("#ffc24b"),
               success=rgb("#6dffa8"), warning=rgb("#ffc24b"), error=rgb("#ff4d6d"), bg_top=rgb("#101a38"),
               bg_bottom=rgb("#04060f"), backdrop="nebula")
LIGHT = Palette(dark=False, background=rgb("#f4f6fb"), surface=rgb("#e9edf6"), panel=rgb("#dde3ef"),
                foreground=rgb("#1e2433"), primary=rgb("#0369d4"), secondary=rgb("#7c3aed"), accent=rgb("#d97706"),
                success=rgb("#15803d"), warning=rgb("#d97706"), error=rgb("#be123c"), bg_top=rgb("#fbfcff"),
                bg_bottom=rgb("#e3e8f2"))


def rect(text: str) -> tuple[int, int, int, int]:
    x, y, w, h = (int(v) for v in text.split(","))
    return x, y, w, h


def size(text: str) -> tuple[int, int]:
    a, b = text.lower().split("x")
    return int(a), int(b)


def border_ok(frame: np.ndarray, edges: tuple[bool, bool, bool, bool]) -> bool:
    top, right, bottom, left = edges
    a = frame[..., 3]
    sides = [a[0] if not top else None, a[:, -1] if not right else None,
             a[-1] if not bottom else None, a[:, 0] if not left else None]
    return all(side is None or int(side.max()) == 0 for side in sides)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="kinds to draw (default: all)")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--screen", type=size, default=(90, 28), help="window in cells (90x28)")
    parser.add_argument("--cell", type=size, default=(9, 19), help="cell in pixels (9x19)")
    parser.add_argument("--at", type=rect, default=(4, 6, 38, 10), help="anchor x,y,w,h in cells")
    parser.add_argument("--to", type=rect, default=(50, 15, 36, 10), help="target x,y,w,h in cells (comets)")
    parser.add_argument("--light", action="store_true", help="a light theme")
    parser.add_argument("--crop", action="store_true", help="show only the stage, at full size")
    parser.add_argument("--tiles", type=int, default=12, help="frames on the sheet (12)")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    pal = LIGHT if args.light else NAVY
    cols, rows = args.screen
    cw, ch = args.cell
    args.out.mkdir(parents=True, exist_ok=True)
    names = args.names or list(FLOURISHES)
    screen_bg = backdrop.draw(cols * cw, rows * ch, pal)
    for name in names:
        kind = FLOURISHES[name]
        color, accent = getattr(pal, kind.color), getattr(pal, kind.accent)
        (sx, sy, sw, sh), stage = make_stage(kind, args.at, args.to, (cols, rows), (cw, ch), color, accent,
                                             dark=pal.dark, seed=args.seed)
        t0 = time.perf_counter()
        frames, gaps = kind.make(stage)
        ms = (time.perf_counter() - t0) * 1000
        arrays = [np.frombuffer(f, np.uint8).reshape(stage.height, stage.width, 4) for f in frames]
        upload = sum(len(zlib.compress(f, 6)) for f in frames)
        ok = all(border_ok(a, stage.window_edges) for a in arrays)
        print(f"{name:14} {len(frames):3} frames {sum(gaps):5} ms  render {ms:6.0f} ms  "
              f"upload {upload / 1024:7.0f} KiB  stage {sw}x{sh} cells {stage.width}x{stage.height} px  "
              f"border {'ok' if ok else 'LIGHT AT THE EDGE'}")

        # the window: backdrop, outlines, then each frame stretched over the stage's cells
        base = Image.fromarray((np.clip(screen_bg, 0, 1) * 255).astype(np.uint8)).convert("RGBA")
        draw = ImageDraw.Draw(base)
        outline = (*pal.foreground, 90)
        for r in (args.at, args.to if name == "comet" else None):
            if r is not None and not kind.screen:
                draw.rounded_rectangle([r[0] * cw, r[1] * ch, (r[0] + r[2]) * cw - 1, (r[1] + r[3]) * ch - 1],
                                       radius=8, outline=outline, width=2)
        box = (sx * cw, sy * ch, (sx + sw) * cw, (sy + sh) * ch)
        shots = []
        for a in arrays:
            layer = Image.fromarray(a, "RGBA").resize((box[2] - box[0], box[3] - box[1]), Image.BILINEAR)
            shot = base.copy()
            shot.alpha_composite(layer, (box[0], box[1]))
            shots.append(shot.crop(box) if args.crop else shot)

        picks = sorted({round(i * (len(shots) - 1) / max(1, args.tiles - 1)) for i in range(args.tiles)})
        tile_w = shots[0].width if args.crop else shots[0].width * 3 // 5
        tile_h = shots[0].height * tile_w // shots[0].width
        per_row = 3 if not args.crop else max(1, min(4, 1600 // tile_w))
        sheet = Image.new("RGB", (per_row * tile_w, -(-len(picks) // per_row) * (tile_h + 18)), (0, 0, 0))
        label = ImageDraw.Draw(sheet)
        elapsed = [sum(gaps[:i]) for i in range(len(gaps))]
        for n, i in enumerate(picks):
            x, y = (n % per_row) * tile_w, (n // per_row) * (tile_h + 18)
            sheet.paste(shots[i].convert("RGB").resize((tile_w, tile_h), Image.LANCZOS), (x, y + 18))
            label.text((x + 4, y + 3), f"{name}  frame {i + 1}/{len(shots)}  {elapsed[i]} ms", fill=(200, 200, 200))
        sheet.save(args.out / f"{name}.png")
        shots[0].save(args.out / f"{name}.gif", save_all=True, append_images=shots[1:], duration=gaps, loop=0,
                      disposal=2)
    print(f"sheets and GIFs in {args.out}")


if __name__ == "__main__":
    main()
