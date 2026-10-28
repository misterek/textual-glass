import re
from pathlib import Path

import textual_orbital


def test_version_matches_pyproject() -> None:
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    found = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    assert found and textual_orbital.__version__ == found.group(1)
