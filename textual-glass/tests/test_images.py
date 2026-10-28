"""Image files: loading (PNG, JPEG, GIF...), smooth resampling, and GlassImage
uploads and playback.

The synthetic tests always run. The art tests use real pictures from a folder
of your own, and are skipped unless you point at one::

    GLASS_TEST_ART=~/Pictures uv run pytest tests/test_images.py
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from textual.app import App, ComposeResult

from textual_glass import Glass, GlassImage, load_image
from textual_glass.images import DEFAULT_GAP_MS, INSTALL_HINT
from textual_glass.testing.pilot_helpers import Output, idle, start
from textual_glass.widgets.image import MAX_ANIMATION_PIXELS, fit, resample

SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
ART_DIR = Path(os.environ["GLASS_TEST_ART"]).expanduser() if os.environ.get("GLASS_TEST_ART") else None
ART_FILES = sorted(p for p in ART_DIR.iterdir() if p.suffix.lower() in SUFFIXES) if ART_DIR and ART_DIR.is_dir() else []
needs_art = pytest.mark.skipif(not ART_FILES, reason="set GLASS_TEST_ART to a folder of images")


def gif_bytes(frames: list[Image.Image], **params: object) -> bytes:
    buffer = io.BytesIO()
    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:], **params)
    return buffer.getvalue()


def solid(color: tuple[int, int, int, int], size: tuple[int, int] = (8, 8)) -> Image.Image:
    return Image.new("RGBA", size, color)


# -- loading ------------------------------------------------------------------------


def test_gif_frames_come_out_composited_with_their_timing() -> None:
    red = solid((255, 0, 0, 255))
    square = solid((0, 0, 0, 0))
    square.paste((0, 0, 255, 255), (3, 3, 5, 5))  # a partial frame drawn over the red one
    image = load_image(gif_bytes([red, square], duration=[50, 0], loop=0, disposal=1))
    assert image.animated and (image.width, image.height) == (8, 8)
    assert image.gaps_ms == [50, DEFAULT_GAP_MS]  # 0 ms means "default speed", as in browsers
    assert image.plays is None
    assert image.frames[1][0, 0].tolist() == [255, 0, 0, 255]
    assert image.frames[1][4, 4].tolist() == [0, 0, 255, 255]


@pytest.mark.parametrize("loop, plays", [(0, None), (None, 1), (2, 3)])
def test_gif_loop_count_becomes_plays(loop: int | None, plays: int | None) -> None:
    params = {} if loop is None else {"loop": loop}
    frames = [solid((255, 0, 0, 255)), solid((0, 255, 0, 255))]
    assert load_image(gif_bytes(frames, duration=80, **params)).plays == plays


def test_png_keeps_transparency() -> None:
    picture = solid((0, 0, 0, 0), (4, 4))
    picture.putpixel((1, 1), (10, 200, 30, 255))
    buffer = io.BytesIO()
    picture.save(buffer, "PNG")
    image = load_image(io.BytesIO(buffer.getvalue()))  # a file object works too
    assert not image.animated and image.plays == 1
    assert image.frames[0][0, 0, 3] == 0 and image.frames[0][1, 1].tolist() == [10, 200, 30, 255]


def test_jpeg_exif_rotation_is_applied(tmp_path: Path) -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # "rotate 90° clockwise to view", as phones write it
    path = tmp_path / "phone.jpg"
    Image.new("RGB", (40, 20), (200, 100, 50)).save(path, exif=exif)
    image = load_image(path)
    assert (image.width, image.height) == (20, 40)
    assert image.frames[0][..., 3].min() == 255


def test_size_and_frame_limits() -> None:
    frames = [solid((i * 20, 0, 0, 255), (300, 150)) for i in range(10)]
    image = load_image(gif_bytes(frames, duration=40, loop=0), max_size=(100, 100), max_frames=4)
    assert len(image.frames) == 4
    assert (image.width, image.height) == (100, 50)


def test_bad_input() -> None:
    with pytest.raises(OSError):
        load_image(b"definitely not an image")
    with pytest.raises(OSError):
        load_image("/no/such/file.png")


def test_missing_pillow_says_how_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(ImportError, match="textual-glass\\[images\\]") as raised:
        load_image(b"")
    assert str(raised.value) == INSTALL_HINT


# -- resampling ---------------------------------------------------------------------


def test_shrinking_averages_instead_of_dropping_pixels() -> None:
    checker = np.zeros((8, 8, 4), np.uint8)
    checker[..., 3] = 255
    checker[::2, ::2, :3] = checker[1::2, 1::2, :3] = 255
    out = resample(checker, 2, 2)
    assert np.all(np.abs(out[..., :3].astype(int) - 128) <= 1)
    nearest = fit(checker, 2, 2, pixelated=True)  # pixel art keeps hard pixels
    assert set(np.unique(nearest[..., :3])) <= {0, 255}


def test_transparent_edges_dont_darken() -> None:
    edge = np.array([[[0, 0, 0, 0], [0, 0, 255, 255]]], np.uint8)  # transparent black | opaque blue
    out = resample(edge, 1, 1)[0, 0]
    assert out[2] == 255 and out[:2].max() == 0 and 120 <= out[3] <= 135


def test_growing_is_smooth() -> None:
    ramp = np.zeros((1, 2, 4), np.uint8)
    ramp[..., 3] = 255
    ramp[0, 1, :3] = 255
    row = resample(ramp, 8, 1)[0, :, 0].astype(int)
    assert row[0] == 0 and row[-1] == 255 and np.all(np.diff(row) >= 0) and len(set(row.tolist())) > 3


# -- the widget ---------------------------------------------------------------------


def test_from_image_picks_scaling_and_natural_size() -> None:
    sprite = GlassImage.from_image(load_image(gif_bytes([solid((1, 2, 3, 255), (48, 48))] * 2, duration=60)))
    photo = GlassImage.from_image(load_image(gif_bytes([solid((1, 2, 3, 255), (512, 512))], duration=60)))
    assert sprite.pixelated and not photo.pixelated
    assert sprite._natural_cells() == (48, 24)
    assert photo._natural_cells() == (64, 32)  # capped; CSS can size it bigger


async def test_half_blocks_follow_frame_timing_and_play_count() -> None:
    frames = [np.full((4, 4, 4), (v, 0, 0, 255), np.uint8) for v in (50, 150, 250)]

    class Plain(App):
        def compose(self) -> ComposeResult:
            yield GlassImage(frames[0], 4, 4, frames=frames[1:], gap_ms=30, plays=1, id="once")
            yield GlassImage(frames[0], 4, 4, frames=frames[1:], gap_ms=30, id="forever")

    app = Plain()
    async with app.run_test(size=(40, 10)) as pilot:
        await asyncio.sleep(0.4)
        await pilot.pause()
        once, forever = app.query_one("#once", GlassImage), app.query_one("#forever", GlassImage)
        assert once._frame == 2 and once._timer is None  # played once, stopped on the last frame
        assert forever._timer is not None
        assert "▀" in "".join(segment.text for segment in once.render_line(0))


async def test_play_count_and_frame_gaps_reach_the_terminal(output: Output) -> None:
    frames = [np.full((16, 16, 4), (v, 80, 160, 255), np.uint8) for v in (0, 100, 200)]

    class Animated(Glass, App):
        def compose(self) -> ComposeResult:
            yield GlassImage(frames[0], 16, 16, frames=frames[1:], gap_ms=[70, 140, 210], plays=2)

    app = Animated()
    async with app.run_test(size=(60, 20)) as pilot:
        await start(app, pilot)
        await idle(app, pilot)
        image = app.query_one(GlassImage)
        model = output.model()
        assert not model.errors, model.errors
        uploaded = model.images[image._image_id]
        assert uploaded["gaps"][:3] == [70, 140, 210]
        assert uploaded["gaps"][3] < 0, "the dimmed copy (for under a popup) is gapless"
        assert uploaded["state"] == 3 and uploaded["loops"] == 3  # v = plays + 1


async def test_long_animations_stay_within_the_memory_budget(output: Output) -> None:
    frames = [np.full((64, 64, 4), (i, 50, 90, 255), np.uint8) for i in range(200)]

    class Long(Glass, App):
        CSS = "GlassImage { width: 1fr; height: 1fr; }"

        def compose(self) -> ComposeResult:
            yield GlassImage(frames[0], 64, 64, frames=frames[1:], gap_ms=40)

    app = Long()
    async with app.run_test(size=(120, 40)) as pilot:
        await start(app, pilot)
        await idle(app, pilot)
        model = output.model()
        assert not model.errors, model.errors
        uploaded = model.images[app.query_one(GlassImage)._image_id]
        assert len(uploaded["frames"]) == 201  # and the dimmed copy
        assert uploaded["w"] * uploaded["h"] * 201 <= MAX_ANIMATION_PIXELS * 1.01


# -- your own pictures (GLASS_TEST_ART) ---------------------------------------------------


def art_gif(tmp_path: Path) -> tuple[Path, list[int]]:
    """An animated GIF made from every art picture, each shown for a different time."""
    frames = [Image.open(p).convert("RGB").resize((160, 160), Image.Resampling.LANCZOS) for p in ART_FILES]
    durations = [120 + 80 * i for i in range(len(frames))]
    path = tmp_path / "art.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations, loop=0)
    return path, durations


@needs_art
@pytest.mark.parametrize("path", ART_FILES, ids=lambda p: p.name)
def test_art_loads(path: Path) -> None:
    from PIL import ImageOps

    with Image.open(path) as original:
        width, height = ImageOps.exif_transpose(original).size
    image = load_image(path)
    frame = image.frames[0]
    assert frame.dtype == np.uint8 and frame.ndim == 3 and frame.shape[2] == 4
    assert max(image.width, image.height) == min(1024, max(width, height))
    assert abs(image.width / image.height - width / height) < 0.01
    assert frame[..., :3].std() > 5  # an actual picture, not a blank


@needs_art
def test_art_survives_a_gif_round_trip(tmp_path: Path) -> None:
    path, durations = art_gif(tmp_path)
    image = load_image(path)
    assert len(image.frames) == len(ART_FILES) and image.gaps_ms == durations and image.plays is None
    for frame, source in zip(image.frames, ART_FILES, strict=True):
        expected = np.array(Image.open(source).convert("RGB").resize((160, 160), Image.Resampling.LANCZOS))
        assert np.abs(frame[..., :3].astype(int) - expected).mean() < 12  # GIF palettes cost a little


@needs_art
async def test_art_in_a_glass_app(output: Output, tmp_path: Path) -> None:
    gif, durations = art_gif(tmp_path)

    class Viewer(Glass, App):
        CSS = "GlassImage { width: 1fr; height: 1fr; }"

        def compose(self) -> ComposeResult:
            yield GlassImage.from_file(gif, id="animated")
            yield GlassImage.from_file(ART_FILES[0], id="still")

    app = Viewer()
    async with app.run_test(size=(120, 40)) as pilot:
        await start(app, pilot)
        await idle(app, pilot)
        model = output.model()
        assert not model.errors, model.errors
        animated, still = app.query_one("#animated", GlassImage), app.query_one("#still", GlassImage)
        uploaded = model.images[animated._image_id]
        assert len(uploaded["frames"]) == len(ART_FILES) and uploaded["gaps"] == durations
        assert uploaded["state"] == 3 and uploaded["loops"] == 1
        assert len(model.images[still._image_id]["frames"]) == 1
        for widget in (animated, still):
            placement = [p for (i, _), p in model.placements.items() if i == widget._image_id and p["virtual"]]
            assert placement and (placement[0]["c"], placement[0]["r"]) == tuple(widget.content_size)
            pixels = model.images[widget._image_id]["frames"][0]
            assert pixels[..., 3].max() == 255 and pixels[..., :3].std() > 5


@needs_art
async def test_art_as_half_blocks_without_glass() -> None:
    class Plain(App):
        def compose(self) -> ComposeResult:
            for path in ART_FILES:
                yield GlassImage.from_file(path)

    app = Plain()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        for widget in app.query(GlassImage):
            text = "".join(segment.text for y in range(widget.content_size.height) for segment in widget.render_line(y))
            assert text.count("▀") + text.count("▄") > widget.content_size.width
