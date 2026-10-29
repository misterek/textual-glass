# textual-glass and textual-orbital

Two Python libraries for [Textual](https://textual.textualize.io) apps, drawn in pixels through the kitty graphics protocol and tuned for Ghostty 1.4. In any other terminal they are plain Textual, with the same layout.

| Package | What it is |
| --- | --- |
| [**textual-glass**](textual-glass/README.md) | Gives a stock Textual app pixel "glass" chrome with one mixin: `class MyApp(Glass, App)`. Glass panels with gradient borders, a sliding selection pill, glowing progress bars, footer key badges and a dimming scrim under modals, drawn under Textual's own text. It is also a small pixel engine: your widgets can add pixel items (rings, meters, charts, glowing dots) and terminal-played animations, and it ships ready-made pixel widgets and `GlassImage` for pictures and GIFs. |
| [**textual-orbital**](textual-orbital/README.md) | A library for always-on dashboards, built on glass. You write one small `Source` per system you watch; orbital polls them, merges their records (with a `data` field for your own typed records) into one snapshot, runs your alert rules, pops up alerts, sends desktop notifications, and draws it all with dashboard widgets: status beacons, a priority queue, a stage track, task boards, timelines. |

Orbital depends on glass; glass knows nothing about orbital. Use glass alone to dress up any Textual app, or both to build a dashboard.

| | |
| --- | --- |
| ![textual-glass: the showcase's pixel widgets at 90x28](textual-glass/docs/screenshots/90x28_pixels.png) | ![textual-orbital: Mission Console at 90x28](textual-orbital/docs/screenshots/90x28_console.png) |
| textual-glass's showcase (`textual-glass/examples/showcase`) | textual-orbital's Mission Console (`textual-orbital/examples/console`) |

*These are **approximate composites** drawn by the test harness from each app's real byte stream in a pty (pixels from a model of Ghostty's kitty rules, text in Menlo at 9x19 px cells), not screenshots of Ghostty: no animation, no dim text, and the font and cell size differ from your terminal.*

## Install

Both install from this GitHub repository; each package lives in its own subdirectory.

textual-glass on its own (the `images` extra adds Pillow, for loading PNG, JPEG, GIF and WebP files):

```sh
uv add "textual-glass[images] @ git+https://github.com/misterek/textual-glass#subdirectory=textual-glass"
```

Both, for a dashboard. Name both in one command: pip can't resolve orbital's dependency on textual-glass by itself.

```sh
uv add "textual-glass[images] @ git+https://github.com/misterek/textual-glass#subdirectory=textual-glass" \
       "textual-orbital @ git+https://github.com/misterek/textual-glass#subdirectory=textual-orbital"
```

With pip:

```sh
pip install "textual-glass[images] @ git+https://github.com/misterek/textual-glass#subdirectory=textual-glass"
pip install "textual-glass[images] @ git+https://github.com/misterek/textual-glass#subdirectory=textual-glass" \
            "textual-orbital @ git+https://github.com/misterek/textual-glass#subdirectory=textual-orbital"
```

They need Python 3.10 or later, `textual>=8.2,<9` and `numpy`. Add the `testing` extra (`textual-glass[images,testing]`) for the pty test harness that lets you check an app without seeing the terminal.

From a local checkout (here a clone at `../textual-glass`, next to your project), install both editable:

```sh
uv add --editable "../textual-glass/textual-glass[images,testing]" ../textual-glass/textual-orbital
pip install -e "../textual-glass/textual-glass[images,testing]" -e ../textual-glass/textual-orbital
```

Name textual-glass even if you only use orbital. uv would find it on its own, through orbital's `[tool.uv.sources]` in the checkout's workspace, but pip doesn't read that table and looks for textual-glass on PyPI, where there is none. Python writes `__pycache__/` directories into an editable checkout's source trees. Git ignores them; to keep them out altogether, set `PYTHONPYCACHEPREFIX` (for example `export PYTHONPYCACHEPREFIX=~/.cache/pycache`).

## Where to start

| You want to | Read |
| --- | --- |
| Give an existing Textual app the glass look | [`textual-glass/README.md`](textual-glass/README.md) |
| Write a widget with pixels of its own | [textual-glass: Write your own pixel widget](textual-glass/README.md#write-your-own-pixel-widget) |
| Build a dashboard (a CI monitor, a homelab board...) | [`textual-orbital/AGENTS.md`](textual-orbital/AGENTS.md#build-a-dashboard): a walkthrough from an empty project to a tested app |
| Know what orbital's records, sources, rules and widgets are | [`textual-orbital/README.md`](textual-orbital/README.md) |
| Change either library, or test an app headlessly | [`AGENTS.md`](AGENTS.md): the rules that keep it working in Ghostty, the layers, the test harness |
| See what changed in a release | [`CHANGELOG.md`](CHANGELOG.md) |

## Developing

This directory is a [uv](https://docs.astral.sh/uv/) workspace with both packages as members. From here:

```sh
uv sync --all-packages     # both packages (editable) and the dev tools
uv run pytest              # both test suites: units, pilot and real-pty tests (a few minutes)
uv run ruff check .
uv run mypy
```

Try the examples (in Ghostty):

```sh
uv run python textual-glass/examples/showcase/app.py
uv run python textual-glass/examples/flourishes/app.py
uv run python textual-orbital/examples/console/app.py
```

Read [`AGENTS.md`](AGENTS.md) before changing anything.

## Licence

[MIT-0](LICENSE) (MIT No Attribution): use, copy, modify and distribute it freely, without keeping a copyright notice.
