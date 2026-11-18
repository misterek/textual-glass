"""The boards demo: a folder of panels on one orbital page, with made-up data.

In Ghostty, from this folder (or with this file's path from anywhere)::

    uv run python app.py --demo     # two seeded boards of fake data, in a temporary folder
    uv run python app.py            # the boards in $BOARDS_DIR, or ~/.cache/boards
    uv run python app.py --help     # everything else

README.md has the keys, the folder's format, and how an agent adds panels.
"""

from boards.app import main

if __name__ == "__main__":
    main()
