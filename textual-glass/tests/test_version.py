import re
from pathlib import Path

import textual_glass


def test_version_matches_pyproject() -> None:
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    found = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    assert found and textual_glass.__version__ == found.group(1)
