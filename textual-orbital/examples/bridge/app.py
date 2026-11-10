"""The bridge: Claude Code and Codex sessions as little crew members at their consoles on a starship bridge.

In Ghostty, from this folder (or with this file's path from anywhere)::

    uv run python app.py                        # the scripted fake bridge (time runs 20x)
    uv run python app.py --live                 # your sessions, from $BRIDGE_STATE or ~/.cache/bridge/sessions
    uv run python app.py --print-hooks claude   # the hook registration for Claude Code (or codex)
    uv run python app.py --help                 # everything else

README.md has the keys, how to hook up Claude Code and Codex, and how to replace the crew.
"""

from bridge.app import main

if __name__ == "__main__":
    main()
