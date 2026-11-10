"""Export the smooth crew as pictures: a starter picture pack to paint over, or to replace picture by picture.

    uv run python tools/export_pictures.py --out DIR                               # every member, pose and frame
    uv run python tools/export_pictures.py --out DIR --members ash,dee --stills    # one still per pose
    uv run python tools/export_pictures.py --out DIR --pack ~/my-smooth-crew --poses seated,hand_up --scale 2

Writes to DIR (never inside the repo unless you say so):

- ``pack.json``: the pack, ready to run (``uv run python app.py --pack DIR``);
- ``guide.png``: the canvas, 464 x 400 at ``--scale 1``: the station as the renderer draws it round your
  pictures, with the console line, the seated head and the reach of a raised hand marked;
- ``<member>/<pose>_<n>.png``: every frame of every pose of each member, drawn on that canvas, the character
  alone (the renderer adds the console, the chair, the lamp's light, the ensigns and the effects). With
  ``--stills``, the first frame only, as ``<pose>.png``.

``PACKS.md`` (the picture style) says what each pose should show and how the fallbacks work: a pack needs only
``seated`` and ``hand_up``. Compare a pack with the live crew: ``tools/crew_sheet.py --pack DIR --against smooth``.
Never opens a window.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.crew import _figure as fig  # noqa: E402
from bridge.crew import _picture as picture  # noqa: E402
from bridge.crew import _scene as scene  # noqa: E402
from bridge.crew import load  # noqa: E402
from bridge.crew._paint import Canvas, Light, rgb  # noqa: E402
from bridge.crew._smooth import DEFAULT_COLORS, SmoothData, member_picture  # noqa: E402

NAVY = (11, 18, 42)
INK = (200, 210, 235)
MARK = (255, 178, 56)


def guide(data: SmoothData, width: int, height: int) -> Image.Image:
    """The canvas with the station drawn round it the way the renderer does (the chair behind, the console in
    front, an amber lamp), on dark navy, with the lines that matter marked."""
    cv = Canvas(width, height, fig.DESIGN, fit="exact")
    lamp = rgb(MARK)
    rig = fig.pose_rigs("seated")[0]
    light = Light(lamp, (fig.CX, fig.DESK + 1.0, 15.0), lamp_power=0.95)
    scene.draw_chair_back(cv, light, data.colors)
    scene.draw_console(cv, light, data.colors, lamp, rig)
    art = Image.fromarray(cv.image(edge=False, pack=False), "RGBA")
    im = Image.new("RGBA", (width, height), (*NAVY, 255))
    im.alpha_composite(art)
    d = ImageDraw.Draw(im)
    k = width / fig.DESIGN[0]  # pixels per design unit

    def y(v: float) -> int:
        return round(v * k)

    def dashed_h(yy: int, color: tuple[int, int, int]) -> None:
        for x in range(0, width, 8):
            d.line((x, yy, x + 4, yy), fill=color, width=1)

    dashed_h(y(fig.DESK), MARK)
    d.text((4, y(fig.DESK) - 11), f"y={y(fig.DESK)}: the console's top. Hands rest on the plate below it", fill=INK)
    dashed_h(y(scene.PLATE_BOTTOM), (255, 90, 90))
    d.text((4, y(scene.PLATE_BOTTOM) + 2), f"y={y(scene.PLATE_BOTTOM)}: below this line the console hides your picture",
           fill=(255, 150, 150))
    assert rig is not None
    hx, hy = rig.head
    r = rig.head_r
    d.ellipse((y(hx - r), y(hy - r), y(hx + r), y(hy + r)), outline=(120, 200, 255), width=1)
    d.text((4, y(hy) - 5), f"a seated head: centre ({y(hx)}, {y(hy)}), radius {y(r)}", fill=(120, 200, 255))
    reach = min(a.hand[1] for a in fig.pose_rigs("hand_up")[0].arms)  # type: ignore[union-attr]
    dashed_h(y(reach), (180, 180, 255))
    d.text((4, y(reach) - 11), f"y={y(reach)}: a raised hand reaches about here", fill=(180, 180, 255))
    d.text((4, 2), f"the picture canvas, {width} x {height}: transparent PNG, the character alone", fill=INK)
    return im


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="the folder to write the pack to (created)")
    ap.add_argument("--pack", default=None, help="a smooth pack to export (default: the built-in crew)")
    ap.add_argument("--members", default="", help="comma-separated member ids (default: all)")
    ap.add_argument("--poses", default="", help="comma-separated poses (default: all but empty)")
    ap.add_argument("--stills", action="store_true", help="one picture per pose (its first frame), <pose>.png")
    ap.add_argument("--scale", type=int, default=1, help="draw the canvas this many times bigger (1: 464 x 400)")
    args = ap.parse_args(argv)
    pack = load(args.pack)
    data = pack._data
    if not isinstance(data, SmoothData):
        ap.exit(2, f"export_pictures: {args.pack} is a {pack.style} pack; only a smooth pack can be exported\n")
    if args.scale < 1:
        ap.exit(2, "export_pictures: --scale is 1 or more\n")
    ids = [m for m in args.members.split(",") if m] or [m.id for m in pack.members]
    poses = [p for p in args.poses.split(",") if p] or list(picture.MEMBER_POSES)
    for mid in ids:
        if mid not in {m.id for m in pack.members}:
            ap.exit(2, f"export_pictures: no member {mid!r} in {pack.name} (members: "
                       f"{', '.join(m.id for m in pack.members)})\n")
    for pose in poses:
        if pose not in picture.MEMBER_POSES:
            ap.exit(2, f"export_pictures: {pose!r} is not a pose a pack draws (poses: "
                       f"{', '.join(picture.MEMBER_POSES)})\n")
    width, height = picture.CANVAS[0] * args.scale, picture.CANVAS[1] * args.scale
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    members = {sm.member.id: sm for sm in data.members}
    for mid in ids:
        sm = members[mid]
        folder = out / mid
        folder.mkdir(exist_ok=True)
        for pose in poses:
            count = 1 if args.stills else len(fig.pose_rigs(pose))
            for frame in range(count):
                name = f"{pose}.png" if count == 1 else f"{pose}_{frame + 1}.png"
                Image.fromarray(member_picture(data, sm, pose, frame, width, height), "RGBA").save(folder / name)
                written.append(folder / name)
    manifest: dict[str, object] = {
        "name": f"{pack.name}-pictures",
        "style": "picture",
        "about": "Exported by tools/export_pictures.py from the smooth crew: paint over these pictures or replace "
                 "them one by one (PACKS.md, the picture style). Only seated and hand_up are required.",
    }
    colors = {k: v for k, v in data.palette.items() if DEFAULT_COLORS.get(k) != v}
    if colors:
        manifest["colors"] = colors
    manifest["ensigns"] = [{"colors": c, "hair_style": style} for c, style in data.ensigns]
    manifest["members"] = [{"id": sm.member.id, "name": sm.member.name,
                            "uniform": "#{:02x}{:02x}{:02x}".format(*sm.member.uniform)}
                           for sm in (members[mid] for mid in ids)]
    (out / "pack.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    written.append(out / "pack.json")
    guide(data, width, height).save(out / "guide.png")
    written.append(out / "guide.png")
    for p in written:
        print(p)
    print(f"{len(written) - 2} pictures of {len(ids)} member(s) in {len(poses)} pose(s); run the bridge with "
          f"--pack {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
