# Working on textual-glass and textual-orbital

This file is for anyone, person or agent, who changes these libraries or builds an app on them. It holds what applies to both packages: how they fit together, the rules that keep them working in Ghostty, the layer table, and how to check your work when you can't see the terminal. Everything here was learned against Ghostty 1.4 and Textual 8.2.8.

Read these next, for the package you are working on:

| File | For |
| --- | --- |
| [`textual-glass/README.md`](textual-glass/README.md) | What glass does, its options, widgets and pixel API. |
| [`textual-glass/AGENTS.md`](textual-glass/AGENTS.md) | Changing glass: its modules, adding a widget or an item, the theme variables, its tests. |
| [`textual-orbital/README.md`](textual-orbital/README.md) | What orbital does: the data model, sources, rules, notifications, widgets. |
| [`textual-orbital/AGENTS.md`](textual-orbital/AGENTS.md) | **Build a dashboard** (a walkthrough from an empty project), wiring real sources, adding a widget or a rule. |

Both packages were built for an always-on dashboard in a corner window of about 90x28 cells. In order of priority, what they make must be:

1. useful at a glance;
2. cheap while idle;
3. good-looking.

Every visual must encode information. Don't add decoration that takes space.

## How the two packages fit together

| Package | Import | Depends on | Gives you |
| --- | --- | --- | --- |
| `textual-glass` | `textual_glass` | `textual>=8.2,<9`, `numpy` (Pillow with `[images]`; pyte and Pillow with `[testing]`) | The `Glass` mixin, which gives any Textual app pixel chrome through the kitty graphics protocol. Also the pixel engine (`PixelMixin`, scene items, sprites, backdrops), generic pixel widgets, `GlassImage`, and the test harness `textual_glass.testing`. |
| `textual-orbital` | `textual_orbital` | `textual-glass` | A generic always-on dashboard: typed records, the source seam, a store, alert rules, notifications, `OrbitalApp(Glass, App)` and dashboard widgets built on glass's `PixelWidget` and `SelectList`. |

textual-glass knows nothing about orbital. Orbital uses only glass's public names, apart from its tests, which patch `textual_glass._textual_hooks.driver_write` to capture output (the same `output` fixture glass's tests use).

In an orbital app the data flows like this:

1. Each `Source.fetch()` runs in a daemon thread of its own, with a deadline (`Source.timeout`). The next fetch is scheduled an interval after it ends, and later while the source fails (rule 21).
2. The result comes back to the app thread (`OrbitalApp.call_from_worker`), and `Store.apply` (or `Store.fail`) merges it. `Store.apply` fails the source instead when its data has a naive datetime or a key another source uses (rule 23).
3. Every fetch re-stamps `app.snapshot` with its time. If the merged content changed, `OrbitalApp.snapshot_changed(snapshot)` runs. By default it calls `update_snapshot(snapshot)` on every widget that has one (each `Page`). Health changes go to `set_statuses`, and the minute tick goes to `minute_tick`.
4. After every fetch, whether it changed anything, failed or timed out, the alert rules run on that snapshot (not on the minute tick), and new alerts are queued and shown.
5. The pages call setters on the widgets (`set_items`, `set_entities`...).
6. Textual renders the text. At the end of that frame, glass's engine collects the pixel items, compares them with what the terminal holds, and sends only the changes.

Each step on the app thread is guarded on its own: what raises goes to `OrbitalApp.report_error` and the other steps still run (rule 22).

## Where things live

| Path | What it is |
| --- | --- |
| `pyproject.toml` | The uv workspace root (not a package): the members, the `dev` dependency group, and the pytest, ruff and mypy settings for both packages. |
| `uv.lock` | One lock file for the workspace. |
| `LICENSE` | MIT-0 (MIT No Attribution). Each package carries a copy. |
| `textual-glass/` | `src/textual_glass/` (the package), `tests/`, `examples/showcase/` (a two-view demo app), `docs/screenshots/`. |
| `textual-orbital/` | `src/textual_orbital/` (the package), `tests/`, `examples/console/` (Mission Console, the demo dashboard), `docs/screenshots/`. |

In the workspace, orbital's dependency on textual-glass resolves to the sibling directory (`[tool.uv.sources] textual-glass = { workspace = true }` in `textual-orbital/pyproject.toml`).

## Commands

From the repository root (this directory):

```sh
uv sync --all-packages        # both packages, editable, plus the dev tools
uv run pytest                 # both test suites: units, pilot and pty tests (about 4 minutes)
uv run ruff check .
uv run mypy                   # both src/ trees
```

Narrower runs:

```sh
uv run pytest textual-glass/tests/test_units.py textual-glass/tests/test_widgets.py   # seconds
uv run pytest textual-orbital/tests/test_pilot.py
uv run python textual-glass/tests/screenshots.py      # regenerate textual-glass/docs/screenshots
uv run python textual-orbital/tests/screenshots.py    # regenerate textual-orbital/docs/screenshots
uv run python textual-glass/tests/test_pty.py --png /tmp/glass-steps      # a composite of every pty step
uv run python textual-orbital/tests/test_pty.py --png /tmp/orbital-steps
```

pytest runs with `--import-mode=importlib`, because both packages have `tests/test_units.py`.

## Rules that must not be "simplified" away

Rules 1 to 19 and 21 to 25 each have at least one test that guards them; rule 20 is enforced by the harness and the test fixtures. The first eight were learned building glass, the rest building the dashboard that became orbital.

1. **Put `q=2` on every graphics command, and never query the terminal once Textual is running.** Textual's input parser doesn't understand kitty graphics replies, so they arrive as key presses. An `ENOENT ...` reply containing `j` or `k` moves the cursor. Detection queries only before the driver starts: `Glass.run_async` calls `app.glass.detect()` first.
2. **Upload every frame of an animation, then start it** (`kitty.animation`). Ghostty 1.4 rejects a chunked `a=f` sent to an image whose animation is playing; it replies `ENOENT` and silently drops the frame, so every later frame shifts by one.
3. **Send each chunked transfer in one write, with nothing in between.** While a chunked load is open, Ghostty treats *any* graphics command as part of it. `kitty.command()` builds the whole transfer and the writer sends it in one piece. Never write graphics from another thread: use `app.glass.background_job(work, done)`; `done` runs on the app thread. For other escape sequences (a notification, the window title, the bell) use `textual_glass.terminal_write(app, data)`, which goes through Textual's writer in order with the frames.
4. **Keep the backdrop, scrims and dialog layers below z = -1073741824**, so they draw under cells with explicit backgrounds (the kitty spec, and `imageLayerFromZ` in Ghostty 1.4). The engine uses Z0 = -2e9, plus 100M per modal level, minus 50M for that level's scrim. See the layer table below.
5. **Retransmitting an image id deletes its placements and frames.** For changes, use partial in-place edits (`a=f,r=1,x,y,X=1`). Re-upload with a new id only when the size changes, and delete the old one after the new one is placed.
6. **Leave the foreground colour of placeholder cells alone.** The image id lives in the 24-bit foreground colour of `U+10EEEE` cells. When glass's line filter dims the screen under a modal, it skips those cells' colours and only resets their background. Textual's modal tint or any colour filter would break the images. Ghostty draws placeholder images at z=-1 whatever z you ask for, so they sit *above* a popup's scrim (see rule 18 for how they are dimmed).
7. **Keep private Textual API in `textual_glass/_textual_hooks.py`, and make every hook fail soft.** The package pins `textual>=8.2,<9`. Run the whole suite before raising the pin. A broken hook means a plainer app, not a crash; after more than five errors the engine turns glass off and the app carries on, plain.
8. **`is_mounted` is still False while `on_mount` runs.** Code reached from `on_mount` must use `is_attached`.
9. **Stay silent while idle.** Only data or layout changes redraw. Ages are computed from timestamps and shown at minute resolution through one formatter, `textual_orbital.fmt_age` (it says `<1m`, never seconds, so every age on screen agrees). The clock has minute resolution. Trail lengths are quantized, so time passing doesn't change pixels. Ambient motion is always an animation the terminal plays by itself. The tests assert zero bytes in idle windows.
10. **Cell backgrounds under glass must be the terminal default, or the pixels don't show.** Glass's line filter (`filter.py`, with rules from `scene.rules_for`) runs last in each widget's filter chain and rewrites *structural* colours to the default: the widget's own resolved background if it is neutral, and neutral or cursor colours of its components (table header, zebra rows, cursor, footer). Colourful backgrounds (badges, buttons) stay opaque on top of the glass. So never paint an opaque neutral background on purpose; if a widget must, add its class name to `GLASS_SKIP`. Pixel widgets draw text with no background (`PixelWidget` sets `background: transparent`). Modal screens are made transparent by `GLASS_CSS` (`!important`, scoped to the App's `-textual-glass` class), and under a modal the filter also resets the modal's own background colours, so text below it stays transparent. Glass never sets `App.ansi_color`.
11. **Textual scopes `DEFAULT_CSS` to the widget.** `App.-textual-glass MyWidget {...}` inside `MyWidget.DEFAULT_CSS` silently becomes `MyWidget App.-textual-glass MyWidget` and never matches. A widget (or an App subclass, like `OrbitalApp`) whose default CSS starts at an ancestor sets `SCOPED_CSS = False`.
12. **`render_line` strips are used as-is.** CSS `text-style` (dim, bold) is not applied to them. That is why glass dims the screen under a modal in its line filter: a widget needs no code for it.
13. **Callbacks from threads must run in the app's context.** `loop.call_soon_threadsafe` runs callbacks in the worker's context, where Textual's `active_app` is unset; pushing a screen from there raised `NoActiveAppError` and hung `run_test`. `GlassEngine.background_job` and `OrbitalApp.call_from_worker` pass `context=` with a copy of the app's context. Use one of them.
14. **Don't shadow Textual attributes.** `App._context` is a method (overwriting it crashed `run_async`), and `Widget.colors` is a read-only property. Check `hasattr(App, name)` (or `Widget`, `Screen`) before adding attributes to an app, screen or widget.
15. **Placeholder rows are 1 cell per `U+10EEEE` plus two zero-width diacritics.** `textual_glass.widgets.strip()` measures them by counting placeholders. Never crop a placeholder run in the middle.
16. **An item's light must end inside its own bounding box.** Ghostty showed faint lighter squares around steady track dots, and a lighter band behind a column of them. The cause was not the in-place edits; the tests prove the edited image is pixel-identical to a full render, and Ghostty's edit rule (`graphics_pixel.zig`: `X=1` overwrites, otherwise source-over) matches the model. The cause was each `Dot`'s glow: `exp(-d/k)` never reaches zero, so it was cut off at the dot's bounding box at about 3% brightness, which shows on dark navy. Every glow now has its own falloff that reaches zero inside the box and follows the item's shape: radial for dots, rounded for pills and bars. `Item.window` also arms a box fade as a safety net. `test_items_draw_nothing_at_the_edge_of_their_box` renders every item alone, with and without that fade, and requires its outermost pixel ring to be black. New items must pass it.
17. **Sprite frames must be fully transparent at their border.** A sprite fills whole cells. The stuck-item pulse's halo was bigger than its 2x1-cell canvas in the middle of the pulse, so Ghostty drew it as a tinted square. Round sprites are multiplied by `sprites.radial_mask`, sprites that fill their box by `sprites.edge_fade`. `test_sprite_frames_are_transparent_at_their_border` checks every frame of every maker at several sizes; check new sprites the same way, mid-animation included.
18. **Dim sprites under popups with a gapless frame, never by re-uploading.** Every sprite and `GlassImage` is uploaded with one extra frame: a copy of frame 1 at scrim brightness (`1 - glass-scrim-alpha`), added with `z=-1`, which Ghostty makes gapless so playback skips it. It goes up with the other frames, before `s=3` (rule 2). While a `ModalScreen` is up, the engine sends `a=a,c=<dim>,s=1` for each image used below it; when the popup closes it sends `a=a,c=1,s=3`, or just `c=1` for stills. `c=` may select a gapless frame (`graphics_exec.zig`).
19. **Page switches swap backdrops in the same frame as the text.** Each page's backdrop is a separate terminal image, keyed by the page's panels (`Layer.page_key`), and up to four are kept. A page seen before is placed in about 1 ms, and its base is rebuilt in the worker for later edits. A first visit is rendered at half resolution inside the frame and replaced by the full-resolution one from the worker. Background stars are placed in relative coordinates, so the two renders match. Rendering the new page only in the worker left the old page's panels under the new page's text for about 150 ms.
20. **Tests must never open the user's real browser, or any GUI app.** A test that pressed `o` once opened made-up URLs in the user's browser. Three guards, all required:
    - `textual_glass.testing.harness.run_app` gives every child process `BROWSER=true`, so Python's `webbrowser` runs the no-op `true` command.
    - Test suites replace `webbrowser.open`, `open_new` and `open_new_tab` with a recorder in an autouse fixture (`no_browser` in `textual-orbital/tests/conftest.py`; copy it into any suite that drives an app with links).
    - `OrbitalApp(open_urls=False)` is the default, so `o` only shows the link. A real dashboard passes `open_urls=True` in its `main()`, never in tests. The one exception is the test of the opening path itself (`test_open_link_runs_off_the_ui_thread_and_honours_open_hosts`), which replaces `webbrowser.open` with a recorder in the test before it passes `open_urls=True`.

    The same goes for anything else that leaves the terminal: no `open`, `osascript`, new terminal windows or screenshots from tests or from an agent.
21. **A fetch never stops its source, the other sources or quitting.** Each fetch runs in a daemon thread of its own, with a deadline: `Source.timeout`, by default `max(2 * interval, 10 s)` (`fetch_timeout`). Past it, the source turns red ("timed out after 20 s"), the fetch's token is dropped so its late result is ignored, and the source is fetched again with backoff. A thread can't be stopped, so a source with `OrbitalApp.HUNG_LIMIT` (2) hung fetches pauses until one returns, and says so in its error. With the old pool of four workers, one fetch with no timeout (a socket that died while the laptop slept) kept its source amber for good, four of them stopped every source, and Python joined the pool's threads at exit, so `q` left a frozen process. `fetch()` must still set its own network and subprocess timeouts. Tests: `test_a_hung_fetch_times_out_and_never_stops_the_other_sources` and `test_at_the_hung_limit_a_source_waits_for_a_fetch_to_return` in `textual-orbital/tests/test_app.py`, `test_quitting_never_waits_for_a_hung_fetch` in its `test_pty.py`.
22. **Nothing raised on the app thread is silent, and one failing step never skips the others.** `loop.call_soon_threadsafe` hands a callback's exception to asyncio's handler, which logs it to stderr, and Textual captures stderr: a rule or a page that raised froze the dashboard without a word, every fetch. So `call_from_worker` guards its callback; `_publish` runs the snapshot hook, health, the alert rules and the badge as separate guarded steps; the default hooks guard each widget; the minute tick re-arms in a `finally`. All of it goes to `OrbitalApp.report_error(where, error)`: the `textual_orbital` logger with the traceback, `app.errors` (the health screen lists them with `SourcesScreen(..., errors=app.errors)`), and a toast at most every `ERROR_TOAST_SPACING` seconds. Tests: `test_a_failing_step_is_reported_and_the_others_still_run`, `test_errors_from_thread_callbacks_and_the_snapshot_are_reported` and `test_a_raising_minute_tick_still_arms_the_next_minute` in `test_app.py`; the console's pilot tests require `app.errors` to stay empty.
23. **One source's bad data fails that source, never the merge.** `Store.apply` rejects a fetch with a naive `datetime` in any record ("naive datetime in AttentionItem.since": the attention sort compares every source's `since`, and one naive value raised there for all of them) or with a key that another source's current data already has ("duplicate key '412' (also from GH): prefix keys with the source": snoozes, `item()` and jumps use bare keys). The source turns red and keeps its last good data. Don't coerce timestamps or namespace keys in the store: keys stay bare and must be unique across sources. Tests: `test_store_fails_a_source_with_a_naive_datetime` and `test_store_rejects_keys_another_source_already_uses` in `test_units.py`.
24. **Dismiss only the screen on top.** In Textual 8.2.8, `Screen.dismiss()` on a screen that is not on top runs that screen's callback and then pops whichever screen *is* on top. The alert popup used to be closed when its condition went away even with the source health screen open over it (that one closed instead), and a second publish before the dismiss callback had run closed it again. `OrbitalApp` forgets the popup the moment it closes it, and `AlertScreen.close_resolved()` dismisses it only while it is on top, or as soon as it is again. Tests: `test_a_resolved_alert_under_another_popup_closes_when_it_is_on_top_again` and `test_a_second_publish_never_closes_a_resolved_alert_twice` in `test_app.py`.
25. **A snooze file never stops startup, and is never half written.** `OrbitalApp(snooze_file=...)` reads it when the app mounts, before the first fetch can raise an alert. A missing file is empty. One that can't be read, isn't JSON or isn't the app's shape (`{"items": {key: until}, "alerts": {key: until}}`, every time with an offset) is reported through `report_error`, treated as empty, and left as it is until a snooze changes. Writes go to a temporary file next to it, then `os.replace`, so a crash or a full disk leaves the old file whole; a failed write is reported and tried again at the next change. Bare keys are safe to keep because they are unique across sources (rule 23). Tests: `test_a_broken_snooze_file_is_reported_and_never_stops_startup` and `test_snoozes_survive_a_restart_and_a_snoozed_wait_stays_quiet_at_launch` in `test_app.py`.

## Layers

Everything glass draws is a kitty image. The table lists the z of each, top to bottom as Ghostty draws them:

| Layer | z | Drawn by | Contents |
| --- | --- | --- | --- |
| Text | (cells) | Textual | All text, always on top. |
| Placeholder images | -1 (Ghostty forces it) | the terminal | `GlassImage` and sprites (beacons, pulses), shown through `U+10EEEE` cells, so Textual clips and covers them like text. Above the scrims: dimmed with a gapless frame (rule 18). |
| Cell backgrounds | (cells) | Textual | Only explicit, non-default cell colours: badges, buttons, swatches. |
| *threshold* | -1,073,741,824 | | Images below this draw under cell backgrounds (rule 4). |
| Dialog of modal level k | Z0 + k x 100M | glass | The modal screen's own glass and its widgets' `pixel_items`. |
| Scrim under modal level k | Z0 + k x 100M - 50M | glass | Dims everything below the modal; fades in and out at `subtle` motion. |
| Float layer | Z0 + 30M | glass | Toasts, tooltips and other overlay widgets. |
| Ambient | Z0 + 10M | glass | `full` motion only: shimmer, glints, the pulsing focus halo (terminal-played). |
| Backdrop | Z0 = -2,000,000,000 | glass | The bottom screen: the backdrop painter (gradient or nebula), panels, pills, header strips, badges, bars and every widget's `pixel_items`. One image per page (rule 19). |

Up to nine screen levels are drawn. Cells with the default background are transparent in Ghostty, so only they reveal the glass (rule 10).

## Checking your work without seeing the terminal

You can't see Ghostty, so work in this order:

1. Unit tests for pure logic (items, sprites, the model, sources: `timed_fetch(source)`).
2. Pilot tests: the app headless, glass forced on, its output captured and checked by a model of Ghostty's kitty rules.
3. Pty runs: the real app in a real pseudo-terminal, its byte stream replayed into a screen model, with approximate composite PNGs you can look at.
4. Ask the user to look at Ghostty.

### Pilot tests (`textual_glass.testing.pilot_helpers`)

Textual's `run_test` runs headless, so glass stays off unless a test turns it on. Capture what glass writes by replacing `textual_glass._textual_hooks.driver_write` (both packages' `tests/conftest.py` have this as the `output` fixture):

```python
# test_my_app.py: needs pytest and pytest-asyncio (asyncio_mode = "auto")
import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable

import textual_glass._textual_hooks as hooks
from textual_glass import Glass, RingGauge
from textual_glass.engine import MAIN
from textual_glass.items import Panel, Ring
from textual_glass.testing.pilot_helpers import Output, settle, start


class MyApp(Glass, App):
    CSS = "DataTable { border: round $primary; height: 1fr; }"

    def compose(self) -> ComposeResult:
        yield RingGauge("CPU", fraction=0.4, text="40%")
        yield DataTable(cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("host")
        table.add_rows([("web-1",), ("web-2",), ("db-1",)])
        table.focus()


@pytest.fixture
def output(monkeypatch: pytest.MonkeyPatch) -> Output:
    out = Output()
    monkeypatch.setattr(hooks, "driver_write", out)  # capture what glass would send to the terminal
    return out


async def test_my_app(output: Output) -> None:
    app = MyApp()
    async with app.run_test(size=(90, 28)) as pilot:
        await start(app, pilot)                                  # glass on, as in Ghostty
        layer = app.glass.layers[MAIN]                           # the bottom screen's scene
        assert any(isinstance(item, Ring) for item in layer.dynamic), "the gauge's ring"
        assert any(isinstance(item, Panel) for item in layer.static), "the table's border is a glass panel"
        mark = output.mark()
        await pilot.press("down")
        await settle(app, pilot)
        assert "a=f" in output.since(mark), "the pill moved by in-place edits, no re-upload"
    model = output.model()
    assert model.errors == [] and model.no_quiet == 0 and model.queries == 0
```

| Name | What it does |
| --- | --- |
| `Output()` | A callable that records every write. `.chunks`, `.text`, `.mark()` and `.since(mark)`, and `.model()`: a `KittyModel` fed with everything written so far. |
| `start(app, pilot)` | Marks the terminal as supported, calls `app.glass.activate(force=True)` and waits until everything is idle. |
| `settle(app, pilot, rounds=8)` | Lets Textual render and glass run its frame and ticks, and waits for worker renders. |
| `idle(app, pilot, timeout=20.0)` | Like `settle`, but also waits for slow worker jobs (big uploads). |

What to assert on:

- `app.glass.layers[(level, "main")]` (`MAIN` is `(0, "main")`): `.static` and `.dynamic` are the current scene items, with their rects in screen cells and their `ident`s.
- `app.glass.last_scene`, `app.glass.stats()` (bytes sent, images, layers, pages, sprites), `app.glass.sprites`, `app.glass.dimmed`.
- A widget's text: `widget.render_line(y).text`.
- `output.model()`: `errors` (protocol mistakes), `images` (frames, gaps, `state`, `loops`), `placements`, `counts` per action letter, `edits`, `no_quiet`, `queries`.

### Pty runs (`textual_glass.testing.harness`)

`run_app` starts any command in a real pty the way Ghostty would host it, plays a script of keys, and records every byte with timestamps. Install the `testing` extra (pyte and Pillow) to use it outside this workspace.

```python
import sys

from textual_glass.testing.harness import Step, graphics_in, replay_steps, run_app

steps = [Step(3.0, "start"), Step(0.0, "down", "down"), Step(1.0, "moved"), Step(0.0, "quit", "q")]
capture = run_app([sys.executable, "app.py"], steps, root=".", size=(90, 28))
assert capture.code == 0 and b"Traceback" not in capture.raw
for label, rp in replay_steps(capture, (90, 28), png_dir="/tmp/shots"):   # writes 00_start.png, ...
    if label == "end":
        assert rp.model.errors == [] and not rp.model.images, "clean protocol; every image deleted on exit"
idle = capture.between(capture.mark_time("moved") + 0.3, capture.mark_time("quit"))
assert graphics_in(idle) == 0
```

| Name | What it does |
| --- | --- |
| `run_app(argv, script, root, size=(160, 45), env=None, answer=False, timeout=30.0, term="xterm-ghostty", probe=None)` | Runs `argv` in a pty with working directory `root`, 9x19 px cells (`CW`, `CH`), `TERM=xterm-ghostty`, `TERM_PROGRAM=ghostty`, `COLORTERM=truecolor` and `BROWSER=true`. It removes `TEXTUAL_GLASS`, `TMUX`, `KITTY_WINDOW_ID` and similar variables from the environment first. `answer=True` replies to the graphics query like a kitty terminal; `probe(label, pid)` runs at every step (to sample CPU time). Returns a `Capture`. |
| `Step(delay, label, keys=None)` | Wait `delay` seconds, mark `label`, then send `keys`: bytes, a name from `KEYS` (`up`, `down`, `left`, `right`, `enter`, `escape`, `tab`, `f9`, `shift+f9`, `ctrl+p`, `pagedown`, `end`), plain text, or `(cols, rows)` to resize. Leave at least 0.3 s after `escape`: an escape followed at once by a key reads as alt+key. |
| `Capture` | `.code` (the exit code, or `"TIMEOUT"`), `.raw`, `.between(t0, t1)`, `.mark_time(label)`, `.chunks`, `.marks`, `.sizes`. |
| `replay_steps(capture, size, png_dir=None)` | Yields `(label, Replay)` just before each step's keys are sent, then `("end", replay)`. It is one live `Replay`, fed on between steps, so `dict(replay_steps(...))` maps every label to the last screen: keep a step with `rp.snapshot()`, as in `{label: rp.snapshot() for label, rp in replay_steps(...)}`. With `png_dir` it writes a composite PNG per step. |
| `replay_until(capture, t_end, size)` | One `Replay` of everything written before `t_end`. |
| `Replay` | `.model` (a `KittyModel`), `.all_text()`, `.text(y)`, `.cell_bgs()` (a list of rows, each a list of cell backgrounds; every one should be `"default"` while glass is on), `.placeholder_ids()`, `.composite(path)`, `.snapshot()` (a frozen copy that can't be fed). |
| `graphics_in(data)` | How many graphics commands are in some bytes. |

For screenshots of chosen steps, `textual_glass.testing.screenshots.shoot(argv, root, size, steps, labels, out, prefix="", timeout=40.0)` runs the app once and writes `{prefix}{cols}x{rows}_{label}.png` into `out` for each step named in `labels`. It returns the paths, and exits with an error when the app fails or the kitty model saw a protocol error. Both packages' `tests/screenshots.py` use it.

The kitty model (`textual_glass.testing.kitty_emu.KittyModel`) applies transmit, frame, animate, place and delete commands the way Ghostty 1.4 does, and records protocol mistakes in `errors`: an `ENOENT`, a command interleaved into a chunked transfer, a frame added while playing, a placement that moves the cursor.

### The PNGs are approximate composites

`Replay.composite` draws a picture from pyte's screen and the kitty model in Ghostty's pass order, with text in Menlo (Pillow's default font where Menlo is missing) at 9x19 px cells. These are approximations, not screenshots: no animation (sprites show their first frame), no dim text, and the font, cell size and colour handling differ from Ghostty's. They catch layout, overlap, truncation and layering mistakes. Look at them with your image viewer (or the Read tool) at 70x22, 90x28 and 140x45.

What the model can't see: faint artefacts of 1 to 3% brightness are invisible at its 9x19 px cells. Light at an item's or sprite's edge is caught by the property tests of rules 16 and 17, not by looking.

### The final check is a person looking at Ghostty

Ask the user to run the app in Ghostty and describe it or send a screenshot. Never open terminal windows, launch apps or take screenshots yourself.

## Known gaps

- Only tested headlessly. The tests check the byte stream against the protocol and against Ghostty's source, not real pixels.
- Untested in kitty, tmux and screen. Under tmux or screen glass is off unless forced with `TEXTUAL_GLASS=on`, and it would need passthrough even then. Orbital's notifications do go through tmux's passthrough (with `allow-passthrough on`); that is checked only in a recorded byte stream.
- Private Textual API (rule 7) pins Textual to 8.x.
