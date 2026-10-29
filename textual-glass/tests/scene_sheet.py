"""Contact sheets, mock GIFs and budgets for the week-in-review scene
(``textual_glass.scenes.space``), one set per look. Approximate, like every
composite here: the actors are composited offline over the nebula the way the
engine will place them (native actors at their own pixels, stretched ones
over whole cells), with no Ghostty and no text.

    uv run python textual-glass/tests/scene_sheet.py --out /tmp/looks            # every look
    uv run python textual-glass/tests/scene_sheet.py halo --out /tmp/looks --no-gif

For each look it writes:

* ``{look}_sheet.png``: the world's loop, every ship, the warp-in, the arrival,
  the lights coming on, the release, and the summary of a 25-item week;
* ``{look}_show.gif``: a 12-ship show on a 120x36-cell page at 9x19 px cells;
* ``{look}_story.png``: moments of that show, for a quick look without playing it;
* ``{look}_final.png`` and ``{look}_light.png``: the summary on the dark and light themes;

and prints the budgets of the 25-item show at 140x45 cells on a Retina screen
(18x38 px cells): actors, frames, pixels, compressed upload, render time, and
whether every frame of every actor is transparent at its border (rule 17).
"""

from __future__ import annotations

import argparse
import json
import math
import time
import zlib
from pathlib import Path

import numpy as np
from flourish_sheet import LIGHT, NAVY  # the dark and light palettes the flourish sheets use
from PIL import Image, ImageDraw

from textual_glass import backdrop
from textual_glass.palette import Palette
from textual_glass.scenes import ROLES, Actor, Scene, Stage, check, space
from textual_glass.scenes._effects import arrive, light_on, warp_in
from textual_glass.scenes._paint import Sprite
from textual_glass.scenes._ships import ship_sprite
from textual_glass.scenes._worlds import LOOP_GAP

CONCEPTS = {
    "harbour": "Ring harbour: haulers dock on the giant's rail, one berth per item, the rail cut into the "
               "seven days. A release is a new moon.",
    "nightfall": "Nightfall: landers come down on the night side and a town lights up where each lands. "
                 "Longitude is the day, latitude the kind. A release is a capital with a beam.",
    "halo": "Halo: probes park on an eclipsed planet's orbits, one orbit per kind, at the hour of the week "
            "(Monday at the top, clockwise), and unfold into lights. A release flares the corona.",
}


def stage_for(pal: Palette, cols: int, rows: int, cell: tuple[int, int], seed: int = 1, still: bool = False) -> Stage:
    colors = {role: getattr(pal, role) for role in ROLES}
    return Stage(cols * cell[0], rows * cell[1], cols, rows, (float(cell[0]), float(cell[1])), colors, pal.dark,
                 still, seed)


# ============================================================ compositing ==


def backdrop_image(stage: Stage, pal: Palette) -> Image.Image:
    bg = backdrop.draw(stage.width, stage.height, pal)
    return Image.fromarray((np.clip(bg, 0, 1) * 255).astype(np.uint8)).convert("RGBA")


def frame_index(actor: Actor, t: float) -> int:
    """The frame showing at ``t``: a loop the terminal plays from ``enter``, or a one-shot glass steps
    once and holds."""
    n = len(actor.frames)
    gaps = list(actor.gaps) if len(actor.gaps) == n else [actor.gaps[0]] * n
    elapsed = (t - actor.enter) * 1000
    if actor.loop:
        elapsed %= sum(gaps)
    acc = 0.0
    for k, g in enumerate(gaps):
        acc += g
        if elapsed < acc:
            return k
    return n - 1


class Compositor:
    """Draws a scene at any moment the way the engine places its actors: native ones at their own pixels
    (centred on their route, at whole pixels, cropped at the stage's edge), stretched ones over whole cells."""

    def __init__(self, stage: Stage, scene: Scene, base: Image.Image) -> None:
        self.stage, self.scene, self.base = stage, scene, base
        self.cache: dict[tuple[int, int], Image.Image] = {}
        self.order = sorted(scene.actors, key=lambda a: a.z)

    def image(self, actor: Actor, k: int) -> Image.Image:
        key = (id(actor), k)
        if key not in self.cache:
            img = Image.frombytes("RGBA", (actor.width, actor.height), bytes(actor.frames[k]))
            if actor.cells is not None:
                cw, ch = self.stage.cell
                img = img.resize((round(actor.cells[0] * cw), round(actor.cells[1] * ch)), Image.BILINEAR)
            self.cache[key] = img
        return self.cache[key]

    def at(self, t: float) -> Image.Image:
        out = self.base.copy()
        cw, ch = self.stage.cell
        for actor in self.order:
            if not actor.enter <= t < actor.leave:
                continue
            img = self.image(actor, frame_index(actor, t))
            cx, cy = actor.route.at(t)
            if actor.cells is not None:
                x = int(round((cx - actor.cells[0] * cw / 2) / cw) * cw)
                y = int(round((cy - actor.cells[1] * ch / 2) / ch) * ch)
            else:
                x, y = round(cx - img.width / 2), round(cy - img.height / 2)
            x0, y0 = max(0, -x), max(0, -y)
            x1, y1 = min(img.width, self.stage.width - x), min(img.height, self.stage.height - y)
            if x1 > x0 and y1 > y0:
                out.alpha_composite(img.crop((x0, y0, x1, y1)), (x + x0, y + y0))
        return out


def save_gif(frames: list[Image.Image], path: Path, gap: int) -> None:
    """One palette for every frame (from a few of them), so the still parts never flicker."""
    sample = [frames[i] for i in sorted({0, len(frames) // 3, 2 * len(frames) // 3, len(frames) - 1})]
    strip = Image.new("RGB", (sample[0].width, sample[0].height * len(sample)))
    for k, f in enumerate(sample):
        strip.paste(f.convert("RGB"), (0, k * f.height))
    palette = strip.quantize(255, method=Image.Quantize.FASTOCTREE)  # keeps small areas of colour
    quant = [f.convert("RGB").quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG) for f in frames]
    durations = [gap] * (len(quant) - 1) + [3000]
    quant[0].save(path, save_all=True, append_images=quant[1:], duration=durations, loop=0, optimize=False)


# ================================================================ budgets ==


def border_clear(actor: Actor) -> bool:
    """Every frame's outermost pixel ring is fully transparent (rule 17)."""
    for f in actor.frames:
        a = np.frombuffer(bytes(f), np.uint8).reshape(actor.height, actor.width, 4)[..., 3]
        if a[0].any() or a[-1].any() or a[:, 0].any() or a[:, -1].any():
            return False
    return True


def budget(scene: Scene, ms: float) -> dict[str, object]:
    loops = [a for a in scene.actors if a.loop and len(a.frames) > 1 and a.cells is not None]
    world = max(loops, key=lambda a: a.width * a.height, default=None)
    return {
        "actors": len(scene.actors),
        "frames": sum(len(a.frames) for a in scene.actors),
        "pixels": sum(a.width * a.height * len(a.frames) for a in scene.actors),
        "upload_bytes": sum(len(zlib.compress(bytes(f), 6)) for a in scene.actors for f in a.frames),
        "hold_bytes": sum(len(zlib.compress(bytes(f), 6)) for a in scene.actors for f in a.frames
                          if a.leave == math.inf),
        "loop_frames": len(world.frames) if world else 0,
        "loop_px": world.width * world.height if world else 0,
        "duration_s": round(scene.duration, 1),
        "render_ms": round(ms),
        "border_ok": all(border_clear(a) for a in scene.actors),
    }


# ================================================================= sheets ==


def tile(img: Image.Image, w: int, h: int) -> Image.Image:
    return img.resize((w, h), Image.LANCZOS)


def strip(sprite: Sprite, bg: Image.Image, pad: int = 4, frames: list[int] | None = None) -> Image.Image:
    """A sprite's frames side by side over a patch of the backdrop."""
    picks = frames if frames is not None else list(range(len(sprite.frames)))
    patch = bg.crop((60, 60, 60 + sprite.width, 60 + sprite.height))
    out = Image.new("RGBA", ((sprite.width + pad) * len(picks), sprite.height), (0, 0, 0, 255))
    for n, k in enumerate(picks):
        cell = patch.copy()
        cell.alpha_composite(Image.frombytes("RGBA", (sprite.width, sprite.height), sprite.frames[k]))
        out.paste(cell, (n * (sprite.width + pad), 0))
    return out


def world_strip(scene: Scene, stage: Stage, bg: Image.Image, count: int = 6) -> Image.Image:
    """The world's loop: every few of its frames, with the stills, cropped round the world."""
    world = [a for a in scene.actors if a.z < 10]  # the world's actors sit under everything else
    comp = Compositor(stage, Scene(world, 1.0), bg)
    loop = max((a for a in world if a.loop), key=lambda a: len(a.frames))
    n = len(loop.frames)
    gap = LOOP_GAP / 1000
    picks = [round(i * n / count) for i in range(count)]
    shots = [comp.at(k * gap + 1e-4) for k in picks]
    xs = [a.route.at(0)[0] for a in world]
    cx, cy = sum(xs) / len(xs), world[0].route.at(0)[1]
    half_w, half_h = min(stage.width, 2.2 * stage.height) / 2, stage.height / 2
    box = (int(max(0, cx - half_w)), int(max(0, cy - half_h)), int(min(stage.width, cx + half_w)),
           int(min(stage.height, cy + half_h)))
    crops = [s.crop(box) for s in shots]
    w = crops[0].width // 3
    h = crops[0].height // 3
    out = Image.new("RGBA", ((w + 4) * count, h + 16), (0, 0, 0, 255))
    draw = ImageDraw.Draw(out)
    for i, (c, k) in enumerate(zip(crops, picks, strict=True)):
        out.paste(tile(c, w, h), (i * (w + 4), 16))
        draw.text((i * (w + 4) + 4, 2), f"loop frame {k + 1}/{n}", fill=(190, 190, 200))
    return out


def contact_sheet(look: str, out: Path, seed: int) -> None:
    """Everything a look is made of, on one page."""
    small = stage_for(NAVY, 120, 36, (9, 19), seed)
    retina = stage_for(NAVY, 120, 36, (18, 38), seed)
    bg_small, bg_retina = backdrop_image(small, NAVY), backdrop_image(retina, NAVY)
    style = space.STYLES[look]
    inks = space.colours(retina)
    week = space.sample_week(25, seed=seed)
    scene = space.make(small, week, look)
    rows: list[tuple[str, Image.Image]] = []
    rows.append(("The world, looping (the terminal plays it; nothing on it moves)",
                 world_strip(scene, small, bg_small)))

    # every ship, at a few headings, at Retina size
    ships = []
    for kind in space.KINDS:
        for heading in (0.0, -0.7, 2.4, 1.2):
            ships.append(strip(ship_sprite(retina, style.family, kind, heading, inks[kind], style.sun), bg_retina,
                               frames=[0]))
    side = max(max(s.width, s.height) for s in ships) + 6
    grid = Image.new("RGBA", (side * 4, side * 5), (0, 0, 0, 255))
    for i, s in enumerate(ships):
        grid.paste(s, ((i % 4) * side + (side - s.width) // 2, (i // 4) * side + (side - s.height) // 2))
    labels = ImageDraw.Draw(grid)
    for k, kind in enumerate(space.KINDS):
        labels.text((4, k * side + 4), kind, fill=(200, 200, 210))
    rows.append((f"Ships ({style.family}): merged, review, deploy, ticket, release; at Retina size", grid))

    heading = -0.5
    rows.append((f"Warp-in ({style.warp})", strip(warp_in(retina, heading, inks["merged"], style.warp), bg_retina)))
    for kind in ("deploy", "release"):
        sprite, _ = arrive(retina, style.family, kind, heading, inks[kind], style.arrive, style.sun, (0.6, 0.4))
        rows.append((f"Arrival ({style.arrive}), {kind}", strip(sprite, bg_retina)))
    lights = [strip(light_on(retina, kind, inks[kind], style.light, seed=3), bg_retina) for kind in space.KINDS]
    lw = max(s.width for s in lights)
    block = Image.new("RGBA", (lw, sum(s.height + 4 for s in lights)), (0, 0, 0, 255))
    y = 0
    for s in lights:
        block.paste(s, (0, y))
        y += s.height + 4
    rows.append((f"Lights coming on ({style.light}); the last frame stays: merged, review, deploy, ticket, release",
                 block))

    # the release, on the page
    comp = Compositor(small, scene, bg_small)
    t_rel = next(c.t for c in scene.cues if c.data[2] == "release")
    moments = [comp.at(t_rel + dt) for dt in (-0.9, -0.3, 0.0, 0.15, 0.35, 0.6, 0.9, 1.4)]
    mw, mh = moments[0].width // 4, moments[0].height // 4
    rel = Image.new("RGBA", (mw * 4 + 12, mh * 2 + 4), (0, 0, 0, 255))
    for i, m in enumerate(moments):
        rel.paste(tile(m, mw, mh), ((i % 4) * (mw + 4), (i // 4) * (mh + 4)))
    rows.append(("The release moment", rel))
    final = comp.at(scene.duration + 5.0)
    rows.append(("The summary that stays: a 25-item week", final))

    width = max(img.width for _, img in rows) + 24
    height = 40 + sum(img.height + 34 for _, img in rows)
    sheet = Image.new("RGB", (width, height), (6, 8, 16))
    draw = ImageDraw.Draw(sheet)
    draw.text((12, 10), f"{look}: {CONCEPTS[look]}", fill=(230, 230, 240))
    y = 40
    for title, img in rows:
        draw.text((12, y), title, fill=(170, 180, 200))
        sheet.paste(img.convert("RGB"), (12, y + 16))
        y += img.height + 34
    sheet.save(out / f"{look}_sheet.png")
    final.save(out / f"{look}_final.png")


def light_frame(look: str, out: Path, seed: int) -> None:
    stage = stage_for(LIGHT, 120, 36, (9, 19), seed)
    scene = space.make(stage, space.sample_week(25, seed=seed), look)
    Compositor(stage, scene, backdrop_image(stage, LIGHT)).at(scene.duration + 5.0).save(out / f"{look}_light.png")


def mock_show(look: str, out: Path, seed: int, gif: bool) -> None:
    """The 12-ship show on a 120x36-cell page, at 10 frames a second, and a storyboard of it."""
    stage = stage_for(NAVY, 120, 36, (9, 19), seed)
    scene = space.make(stage, space.mini_week(seed + 1), look, length=11.0)
    check(scene, stage)
    comp = Compositor(stage, scene, backdrop_image(stage, NAVY))
    gap = 100
    if gif:
        frames = [comp.at(k * gap / 1000) for k in range(int((scene.duration + 1.5) * 1000 / gap))]
        save_gif(frames, out / f"{look}_show.gif", gap)
    cue_t = [c.t for c in scene.cues]
    moments = [0.5, cue_t[0] - 1.0, cue_t[0] + 0.2, cue_t[3], cue_t[6], cue_t[-1] - 0.3, cue_t[-1] + 0.3,
               scene.duration + 2.0]
    shots = [comp.at(t) for t in moments]
    w, h = shots[0].width // 2, shots[0].height // 2
    board = Image.new("RGB", (w * 4 + 12, h * 2 + 4), (0, 0, 0))
    draw = ImageDraw.Draw(board)
    for i, (s, t) in enumerate(zip(shots, moments, strict=True)):
        x, y = (i % 4) * (w + 4), (i // 4) * (h + 4)
        board.paste(tile(s, w, h).convert("RGB"), (x, y))
        draw.text((x + 6, y + 4), f"{t:4.1f} s", fill=(220, 220, 230))
    board.save(out / f"{look}_story.png")


def measure(look: str, seed: int) -> dict[str, object]:
    """The 25-item show at 140x45 cells on a Retina screen, and the same page under minimal motion."""
    stage = stage_for(NAVY, 140, 45, (18, 38), seed)
    week = space.sample_week(25, seed=seed)
    t0 = time.perf_counter()
    scene = space.make(stage, week, look)
    ms = (time.perf_counter() - t0) * 1000
    check(scene, stage)
    numbers = budget(scene, ms)
    still = stage_for(NAVY, 140, 45, (18, 38), seed, still=True)
    t0 = time.perf_counter()
    quiet = space.make(still, week, look)
    numbers["still_bytes"] = sum(len(zlib.compress(bytes(f), 6)) for a in quiet.actors for f in a.frames)
    numbers["still_ms"] = round((time.perf_counter() - t0) * 1000)
    numbers["still_border_ok"] = all(border_clear(a) for a in quiet.actors)
    small = stage_for(NAVY, 120, 36, (9, 19), seed)
    t0 = time.perf_counter()
    page = space.make(small, week, look)
    numbers["page_ms"] = round((time.perf_counter() - t0) * 1000)
    numbers["page_bytes"] = sum(len(zlib.compress(bytes(f), 6)) for a in page.actors for f in a.frames)
    numbers["page_border_ok"] = all(border_clear(a) for a in page.actors)
    return numbers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("looks", nargs="*", help=f"looks to draw (default: all of {', '.join(space.LOOKS)})")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--no-gif", action="store_true", help="skip the GIFs (the slow part)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    table = {}
    for look in args.looks or space.LOOKS:
        numbers = measure(look, args.seed)
        table[look] = numbers
        clear = numbers["border_ok"] and numbers["still_border_ok"] and numbers["page_border_ok"]
        print(f"{look:10} actors {numbers['actors']:3}  frames {numbers['frames']:4}  "
              f"px {numbers['pixels'] / 1e6:5.1f}M  upload {numbers['upload_bytes'] / 1e6:4.2f} MB "
              f"(held {numbers['hold_bytes'] / 1e3:4.0f} KB, minimal motion {numbers['still_bytes'] / 1e3:4.0f} KB)  "
              f"loop {numbers['loop_frames']}x{numbers['loop_px']} px  render {numbers['render_ms']} ms  "
              f"{numbers['duration_s']} s  border {'ok' if clear else 'LIGHT AT THE EDGE'}")
        contact_sheet(look, args.out, args.seed)
        light_frame(look, args.out, args.seed)
        mock_show(look, args.out, args.seed, not args.no_gif)
    (args.out / "numbers.json").write_text(json.dumps(table, indent=2))
    print(f"sheets, GIFs and numbers in {args.out}")


if __name__ == "__main__":
    main()
