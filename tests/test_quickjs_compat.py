"""Keep fresh installations on the QuickJS contract used by Nova's guarded bridge."""

from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.requirements import Requirement


@pytest.mark.parametrize("package", ["langchain-quickjs", "quickjs-rs"])
def test_quickjs_requirement_matches_the_tested_private_runtime(package: str) -> None:
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text("utf-8"))
    requirement = next(
        Requirement(value)
        for value in project["project"]["dependencies"]
        if Requirement(value).name == package
    )
    assert str(requirement.specifier) == f"=={version(package)}"
