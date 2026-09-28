import re
from pathlib import Path

from telemetry_radio import __version__

ROOT = Path(__file__).parents[1]


def test_package_versions_are_synchronized_and_semantic():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project_version = re.search(r'^version = "([^"]+)"$', project, re.MULTILINE)

    assert project_version is not None
    assert project_version.group(1) == __version__
    assert re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", __version__)
