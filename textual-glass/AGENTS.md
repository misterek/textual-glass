# Working on textual-glass

Read [`README.md`](README.md) first: it says what textual-glass does and documents its public API. The rules that keep it working in Ghostty, the layer table and the test harness are in [`../AGENTS.md`](../AGENTS.md); read them before changing anything here. This file covers where things live, how to add support for a stock widget, a pixel widget, an item, a sprite or a flourish, the theme variables glass reads, and the tests. Everything here was learned against Ghostty 1.4 and Textual 8.2.8.

## Where things live

Modules in `src/textual_glass/`:

| Module | What it does |
| --- | --- |
| `__init__.py` | The public names: `Glass`, `GlassEngine`, `PixelMixin`, `PixelContext`, the widgets, `GlassImage`, `load_image`, `terminal_write`, `on_hard_exit`, `detect`, `MOTIONS`, `FLOURISHES`. |
| `glass.py` | The `Glass` mixin (`class MyApp(Glass, App)`): the `GLASS_*` options, `f9`/`shift+f9`, the palette's "Glass: ..." commands, detection before the driver starts, the suspend and resume subscriptions, `GLASS_CSS` (transparent modals), `terminal_write`. |
| `engine.py` | `GlassEngine` (`app.glass`): owns the terminal images. Layers keyed `(level, "main" or "float")`, the ident-aware diff, in-place edits and worker renders, one cached backdrop per page (half resolution inside the frame for a new page), sprites (freed once no widget shows them, rule 21), dimming placeholder images under popups, the image cache, `background_job` and the one path every worker callback takes (`_from_worker`, rule 22), the error budget and its retries, suspend and resume, glass's hard-exit sequence. `flourish()`: an anchor's screen and cells, the stage, the limit (`MAX_FLOURISHES`). `play_scene()` and the `player` it makes on first use, and the few calls into it (each frame, a resize, a theme change, the motion level, glass turning off, `stats()`). The z constants (`Z0`, `LEVEL_STEP`, `Z_SCRIM`, `Z_SCENE`, `Z_FLOURISH`...). |
| `hard_exit.py` | `on_hard_exit(key, data)`: escape sequences written to the terminal on SIGTERM, SIGHUP or an unclean interpreter exit (rule 24). No Textual imports; works with glass on or off. |
| `scene.py` | Reads the widget tree and computed styles and builds the items for each visible screen: CSS chrome (panels, sheets, table chrome, pills, footer badges, bars, scrollbars) and every `PixelMixin` widget's `pixel_items(ctx)` in the same walk. `PixelMixin` (whose `render_lines` reports the sprites a widget's lines show), `PixelContext`, `Tweens`, and `rules_for` (which colours the filter owns). |
| `render.py` | The drawing model, no Textual imports: `Grid` (cells to layer pixels), `Canvas`, the `Item` base class (`bbox`, `draw`, `window`, `diff_boxes`, `dynamic`, `edge`), `paint`, `to_u8`, colour and shape helpers. |
| `items.py` | Every item, no Textual imports. Stock chrome: `Panel`, `Surface`, `HeaderStrip`, `Rows`, `Pill`, `Badge`, `Progress`, `Thumb`. Instruments: `HeaderBar`, `Rule`, `Ring`, `Signal`, `Meter`, `Area`, `Bars`, `Lane`, `Dot`, `Spine`. |
| `sprites.py` | Frame makers, no Textual imports: scrims (`solid`, `solid_frames`), `beacon_frames`, `pulse_frames`, the `full` motion effects (`halo_frames`, `glint_frames`, `shimmer_frames`), `radial_mask`, `edge_fade`, `dim_frame`. |
| `backdrop.py` | The backdrop painters, no Textual imports: `gradient` and `nebula`; `draw` picks one by `palette.backdrop`. |
| `palette.py` | `Palette.from_app`: the theme's colours and glass's theme variables as RGB; colour helpers (`mix`, `lighten`, `hue_shift`, `border_pair`, `hex_of`, `rgb`). |
| `filter.py` | `GlassFilter` and `Rules`: the per-widget line filter that turns structural backgrounds into the terminal default, blanks pixel-drawn scrollbars and bars, and dims the screen under a modal (leaving placeholder colours alone). |
| `effects.py` | Popup scrims (fades, recolouring), the `full` motion ambient effects, and flourish playback: rendered in the worker, uploaded while stopped, stepped with `a=a,c=N`, deleted after the last frame, dropped on a resize, a theme change, `minimal` motion or their screen closing. |
| `flourishes/` | The flourish kinds, no Textual imports. `__init__.py`: `Flourish`, `Stage`, `make_stage`, `stage_cells`, `FLOURISHES` and the limits (`MAX_PX`, `MAX_FRAMES`, `MAX_MS`). The makers, by family: `streaks.py` (kinds that travel), `bursts.py`, `shapes.py`. `_draw.py`: what the makers share: `Light` (the canvas and the frame encoder), `window`, `room`, the border fade, colours for the theme, glows, easing and timelines. |
| `scenes/` | The scene contract, no Textual imports: `Stage`, `Route`, `Actor`, `Cue`, `Scene`, `SceneMaker`, the palette `ROLES`, the limits (`MAX_ACTORS`, `MAX_FRAMES`, `MAX_PIXELS`, `MAX_SECONDS`) and `check`. The app writes the makers. |
| `player.py` | Scene playback (`app.glass.play_scene`): `Player`, owned by the engine and made on first use, and the `Showing` handle. The maker and the uploads' encoding in the worker (two jobs: ids are handed out on the app thread in between); each tick the actors on stage placed again only when their rounded position or crop changes, native ones at a cell plus `X`/`Y` offsets with a source rectangle, stretched ones on whole cells; one-shots stepped with `a=a,c=N`; cues; the hold; the anchor checked every frame; the end reasons, every image deleted and every id freed; `minimal` motion; `on_cue` and `on_end` run on the app thread once glass is done (`loop.call_soon`), their errors counted. `MAX_SCENES`, `REASONS`. |
| `kitty.py` | The protocol as pure functions: commands, PNG, transmit, frames, `animation` (and `play`, to start one uploaded stopped), edits, placements (`place` over cells, `place_px` at an image's own pixels with offsets and a source rectangle), placeholders, deletes (one id, delete-all, an id range), OSC 11. |
| `detection.py` | Whether to use glass. Runs before Textual's driver reads input. |
| `images.py` | `load_image` and `LoadedImage` (Pillow, the `images` extra). |
| `_textual_hooks.py` | **Every** use of Textual's private API, each failing soft (rule 7). Nothing else may touch private Textual attributes. |
| `widgets/base.py` | `PixelWidget`, `SelectList`, `Tween`, and the text helpers `strip`, `fit`, `placeholder_parts`, `text_meter`, `tone`, `ellipsize`, `hex_colour`. |
| `widgets/panel.py` | `GlassPanel`. |
| `widgets/instruments.py` | `RingGauge`, `Telemetry`, `StatValue`, `StatTile`, `ProgressArc`, `SpriteView`, `InfoCard`. |
| `widgets/image.py` | `GlassImage`: placeholders with glass, half blocks without; `cache_key`; the dimmed gapless frame. |
| `testing/harness.py` | The pty runner and replay (`run_app`, `Step`, `replay_steps`, `Replay`, `graphics_in`). |
| `testing/kitty_emu.py` | `KittyModel`, a model of Ghostty 1.4's kitty graphics rules (given the cell size, it records an `X` or `Y` offset at or beyond it as a mistake; also a source rectangle off the image), and `composite`, which draws the approximate PNGs (placements at their own pixels and source rectangles included). |
| `testing/pilot_helpers.py` | `Output` (its `model(cell=None)` places each image at the cursor written before it), `start`, `settle`, `idle` for pilot tests. |

Elsewhere in `textual-glass/`:

| Path | What it is |
| --- | --- |
| `tests/sample_app.py` | A small stock app with a `RingGauge`, a `SpriteView`, a `GlassImage`, a modal and two "pages". The pilot tests use it; run it with `uv run python tests/sample_app.py`. |
| `tests/test_*.py`, `tests/conftest.py` | See [Tests](#tests). `conftest.py` has the `output` fixture. |
| `tests/screenshots.py` | Writes `docs/screenshots/` from the showcase. |
| `tests/test_flourishes.py` | Every flourish kind by itself: the rule 17 border check, the limits, determinism, `about` and palette roles, no Textual imports, the stage's geometry; and `_draw.Light`'s windows and packing. |
| `tests/test_flourish_pilot.py` | `flourish()` in a running app, with test kinds, and the demo playing every real kind. |
| `tests/flourish_sheet.py` | Contact sheets and GIFs of the kinds, with their frame counts, render times and upload sizes. |
| `tests/test_scenes.py` | The scene contract by itself: routes, and the limits `check` holds a maker to. |
| `tests/test_scene_pilot.py` | `play_scene()` in a running app, with tiny test actors and a fake clock (`engine.player.clock`), and the demo playing to its hold. |
| `examples/showcase/` | `app.py` (the app, keys, `--plain`, `--image`), `views.py` (view 1 stock widgets, view 2 pixel widgets, with a `SelectList` subclass), `dialogs.py` (the modals), `art.py` (the rocket drawn in numpy), `world.py` (made-up data and colours), `showcase.tcss`. |
| `examples/flourishes/` | `app.py`: every flourish kind over a small made-up dashboard, for trying them in Ghostty (keys, `--plain`); `flourishes.tcss`. |
| `examples/scenes/` | `app.py`: a small scene over a bordered panel in placeholder art (dots flying in from beyond its edge, a stretched disc, one-shot flashes, a caption on cues, the hold), and `make_show`, its maker; `scenes.tcss`. |

## Adding support for a stock widget

1. Reproduce the problem in `tests/sample_app.py` (or a small app of your own). Compare it plain against glass with `TEXTUAL_GLASS=off` or `shift+f9`.
2. Decide which part is wrong:
   - which backgrounds become transparent: `scene.rules_for` and `filter.py`;
   - which chrome is drawn: `scene.py` (`_base_chrome`, `_widget_chrome`) describes the items and `items.py` draws them;
   - motion: `effects.py`, or the `Tweens` in `scene.py`.
3. Match widgets by class name with `scene.is_a(widget, "DataTable")`, so the scene imports no widget modules and subclasses count.
4. If a widget deliberately paints a neutral background that must stay opaque, either tighten the filter rule or tell users about `GLASS_SKIP`.
5. If you need a private attribute, add an accessor in `_textual_hooks.py` with a safe fallback, and add it to `test_every_hook_fails_soft`.
6. Add a pilot test in `tests/test_pilot.py` that asserts on the scene items and on `output.model().errors == []`. Add a step to `tests/test_pty.py` if the change affects the byte stream.

## Adding a pixel widget

For a widget in an app, follow "Write your own pixel widget" in the README. To add one to the library:

1. Subclass `PixelWidget` (or `SelectList`) in `widgets/instruments.py` or a new module under `widgets/`. Colours are constructor arguments that default to the theme (`ctx.palette` in `pixel_items`, `self.theme_palette` in `render_line`); never a module-level palette. The setters take them too, so an update can change a colour (a grade turning amber); `None` keeps the current one.
2. Draw a text fallback with the same layout (`self.glass_on` says which mode is current). Give every item a stable `ident` and `clip=self.visible_clip()`.
3. Don't add attributes Textual already has (rule 14): `test_widgets_add_no_attribute_textual_has` lists the widget classes; add yours.
4. Export it from `widgets/__init__.py` and `textual_glass/__init__.py` (both `__all__` lists).
5. In `tests/test_widgets.py`: add it to the `Board` app, assert on its text in both modes and on its scene item in `test_every_widget_renders_in_both_modes_with_identical_layout`, and add its ident prefix to the regex in `test_widget_items_draw_nothing_at_the_edge_of_their_box` (rule 16 for the items it yields).
6. Add it to the README's widget table, and to the showcase's view 2 if it shows something new.

## Adding an item

An item is a frozen dataclass in `items.py` that subclasses `render.Item`:

- Fields in cell units: the geometry and colours first (`Colour` is `"#rrggbb"` or an RGB tuple), then `ident: str = ""`, `depth: int = 0` and `clip: FRect | None = None` last, like every other item.
- `dynamic: ClassVar[bool] = True` if it changes often (values, positions): it is then composed over the cached base when a rectangle is edited.
- `bbox(g)` returns its pixel box (`g.box(rect, pad_x, pad_y)`). Leave room around the light it draws.
- `draw(c, g)` starts with `win = self.window(c, g)` (the canvas pixels it may touch; this also arms the edge fade), then draws with `c.over(view, colour, alpha)` and `c.glow(view, colour, amount)`. `render.sd_rrect`, `smoothstep` and `x_gradient` help.
- **Rule 16:** every glow needs a falloff that reaches zero inside the box (multiply an `exp()` glow by a `smoothstep` that ends inside it). Don't rely on the box fade: `edge` (rows of safety fade, default 0.22; 0 for none) is only a safety net, and the test runs both with and without it.
- `diff_boxes(old, g)` is optional: return the pixel boxes that differ from `old` (an item with the same `ident`) when a change touches only part of it (see `Panel`), or None for "both bounding boxes".
- No Textual imports in `render.py`, `items.py`, `sprites.py` or `backdrop.py` (`test_render_layer_has_no_textual_imports`).

Then add a sample of it to `EDGE_SAMPLES` in `tests/test_units.py`, in the variant that draws the most light. `test_every_item_class_is_checked_at_its_edges` fails until you do, and `test_items_draw_nothing_at_the_edge_of_their_box` checks it at two cell sizes, with and without the safety fade.

An item defined in an app (outside `items.py`) is not checked by those tests; copy the check into the app's tests, or paint it on a canvas and look at the outermost ring of its box the same way.

## Adding a sprite maker

A maker lives in `sprites.py`, is named `*_frames`, and returns straight-alpha RGBA frames as bytes plus a gap in ms (`(frames, gap_ms)`). Multiply every frame by `radial_mask` (round sprites) or `edge_fade` (sprites that fill their box) so it is transparent at its border (rule 17). Add cases for it to `_sprite_cases()` in `tests/test_units.py`: `test_every_sprite_maker_is_checked_at_several_sizes` wants at least three, and `test_sprite_frames_are_transparent_at_their_border` checks every frame of each.

## Adding a flourish

A kind is a `Flourish` in one of the family modules, listed in that module's `KINDS` dict: `flourishes/streaks.py` (kinds that travel), `bursts.py` or `shapes.py`. `FLOURISHES` collects them. No Textual imports.

1. Write the maker, `make(stage) -> (frames, gaps)`. It runs in glass's worker thread. The `Stage` gives the image size in pixels (square pixels; the terminal stretches the image over the stage's cells), `scale` (multiply stroke widths and radii by it), one cell in image pixels, the anchor `at` and the target `to` (or None) in image pixels, the two colours, `dark` (on a light theme, draw in colour, not white), the sides on the window's edge, and a `seed` for any randomness.
2. Return straight-alpha RGBA frames as bytes (`width * height * 4` each) and one duration per frame in ms: 2 to `MAX_FRAMES` (48) frames, each at least 16 ms, and at most `MAX_MS` (2600 ms) in all. The last frame must be (nearly) empty, so the end never pops. The same seed gives the same frames. Draw with `_draw.Light`: `over` for paint, `add` for light that adds up, `shine` for light that reads on either theme (added on a dark one, painted on a light one), each into the whole frame or a `_draw.window` of it; `frame()` gives the bytes, packed to upload small.
3. **Rule 17:** every frame is fully transparent at the stage's border, except on sides on the window's edge (`Stage.window_edges`). `Light.frame()` fades the border for you (`_draw.border`). If the light needs more room, raise `grow` instead of drawing up to the edge.
4. Add it to `KINDS`: `Flourish(make, grow=(cols, rows), color="primary", accent="secondary", screen=False, about="...")`. `grow` is how many cells the stage reaches past the anchor (and the target) on each side. `screen=True` makes the stage the whole window. `color` and `accent` are `Palette` field names. `about` says in one line what it could mean on a dashboard.
5. Test it: `tests/test_flourishes.py` checks every kind in `FLOURISHES`, so a new one is covered as soon as it is listed. `test_flourish_frames_are_transparent_at_the_border_and_within_the_limits` is the rule 17 test: every frame at five window and cell sizes, a light theme included. `test_the_demo_plays_every_kind` in `tests/test_flourish_pilot.py` plays it through the engine.
6. Look at it: `uv run python textual-glass/tests/flourish_sheet.py NAME --out /tmp/fl` (from the repository root) writes a contact sheet (every few frames over a nebula backdrop, the anchor and target outlined) and a GIF at the real timing. It prints the frame count, the duration, the render time, the upload size and the border check. `--light`, `--screen 70x22` and `--crop` change the setting. Then ask the user to try it in the demo in Ghostty (`uv run python textual-glass/examples/flourishes/app.py`).
7. Add it to the table of kinds in the README's [Flourishes](README.md#flourishes) section.

## Theme variables glass reads

Glass takes every colour from the app's Textual theme (`Palette.from_app`): `background`, `surface`, `panel`, `foreground`, `primary`, `secondary`, `accent`, `success`, `warning`, `error`, and `block-cursor-background` for cursor pills. A theme can also set these variables (`Theme.variables`, all strings):

| Variable | Default | What it does |
| --- | --- | --- |
| `glass-backdrop` | `gradient` | The backdrop painter: `gradient` (from the theme's colours and `GLASS_BACKGROUND`) or `nebula` (deep space, nebulae and stars). Anything else means `gradient`. |
| `glass-scrim-alpha` | `0.5` on dark themes, `0.3` on light ones | How dark a popup's scrim gets (0 to 1). Sprites and images below a popup dim to `1 - alpha` of their brightness. |
| `glass-space-top`, `glass-space-bottom` | `#0b1433`, `#03050d` | `nebula`: the space gradient, top to bottom. |
| `glass-nebula-1`, `glass-nebula-2`, `glass-nebula-3` | `#5b2a9e`, `#0e6a78`, `#1b3d8f` | `nebula`: the top-right nebula, the bottom-left one, and the faint scattered one. |
| `glass-nebula-strength` | `1.0` | `nebula`: 1 is the default (dim) nebulae, 0 leaves only the space gradient and its stars; higher is brighter. |

```python
from textual.app import App, ComposeResult
from textual.theme import Theme
from textual.widgets import Static

from textual_glass import Glass

DUSK = Theme(name="dusk", primary="#7aa2f7", secondary="#bb9af7", background="#16161e", dark=True,
             variables={"glass-backdrop": "nebula", "glass-scrim-alpha": "0.6", "glass-nebula-strength": "0.5"})


class MyApp(Glass, App):
    CSS = "Static { border: round $primary; height: 5; }"

    def compose(self) -> ComposeResult:
        yield Static("over a nebula")

    def on_mount(self) -> None:
        self.register_theme(DUSK)
        self.theme = "dusk"


if __name__ == "__main__":
    MyApp().run()
```

Any other theme works too; without the variables the backdrop is the gradient. textual-orbital's `ORBITAL` and `ORBITAL_NAVY` themes are examples that set all of them.

## Tests

From the repository root:

```sh
uv run pytest textual-glass/tests                                  # about 1.5 minutes
uv run pytest textual-glass/tests/test_units.py textual-glass/tests/test_widgets.py   # seconds
uv run ruff check . && uv run mypy
uv run python textual-glass/tests/test_pty.py --png /tmp/glass-steps   # a composite per pty step
uv run python textual-glass/tests/screenshots.py                    # regenerate docs/screenshots
uv run python textual-glass/tests/flourish_sheet.py --out /tmp/fl   # contact sheets and GIFs of every flourish
GLASS_TEST_ART=~/Pictures uv run pytest textual-glass/tests/test_images.py   # with real pictures, if the user has some
```

The README's [Tests](README.md#tests) section says what each file checks. Rules 1 to 19 and 21 to 24 (glass's own; 25 to 30 are orbital's) in `../AGENTS.md` each have a guarding test; when you change the code a rule protects, run that test first.

## Known gaps

- Untested in kitty, tmux and screen. Under tmux glass is off unless forced.
- An app with no bordered or backgrounded widgets gets no backdrop image: a layer is only created when it has items. The terminal's default background (OSC 11) still matches the theme.
- Colour-based ownership is a heuristic (see the README's Limitations).
- Items defined outside `items.py` are not covered by the rule 16 test.
- A sprite used by a widget that isn't a `PixelMixin` is freed only when that widget is removed (rule 21).
- A flourish stays where it started: if its widget moves or is hidden while it plays (a scroll, a page switch), it finishes where the widget was. Only its screen going away stops it early. A scene, by contrast, ends as soon as its anchor moves or is hidden, and the app plays it again if it wants its end back.
- Scene actors are placed at terminal pixels from `TIOCGWINSZ`. Where the cell size it reports isn't whole, offsets are rounded so they stay below it, and an actor can be up to a pixel off.
