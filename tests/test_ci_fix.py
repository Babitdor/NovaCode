"""/ci-fix fetches the failed run with gh and builds the agent prompt."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from novacode_cli.commands import ci_fix
from novacode_cli.commands.ci_fix import CIFixError, build_ci_fix_prompt

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

RUN = {
    "databaseId": 42,
    "displayTitle": "fix parser",
    "workflowName": "CI",
    "headBranch": "main",
    "headSha": "abc123def4567890",
    "url": "https://github.com/o/r/actions/runs/42",
    "conclusion": "failure",
}


def _fake_run(
    calls: list[list[str]], *, runs: list[dict], head: str = "abc123def4567890"
) -> Callable[..., subprocess.CompletedProcess]:
    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        out = {
            ("git", "rev-parse", "--abbrev-ref"): "main\n",
            ("git", "rev-parse", "HEAD"): head + "\n",
            ("gh", "run", "list"): json.dumps(runs),
            ("gh", "run", "view"): (
                "test\tpytest\t\x1b[31mFAILED tests/test_x.py::test_y\x1b[0m\n"
                if "--log-failed" in argv
                else json.dumps(RUN)
            ),
        }[tuple(argv[:3])]
        return subprocess.CompletedProcess(argv, 0, out, "")

    return run


def test_latest_failed_run_on_branch_becomes_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[list[str]] = []
    monkeypatch.setattr(ci_fix.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(ci_fix.subprocess, "run", _fake_run(calls, runs=[RUN]))
    prompt = build_ci_fix_prompt(tmp_path)
    assert calls[1][-2:] == ["--branch", "main"]
    assert "FAILED tests/test_x.py::test_y" in prompt
    assert "\x1b" not in prompt
    assert 'url="https://github.com/o/r/actions/runs/42"' in prompt
    assert "untrusted CI output" in prompt
    assert "local HEAD" not in prompt


def test_explicit_run_and_head_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(ci_fix.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(
        ci_fix.subprocess, "run", _fake_run(calls, runs=[], head="ffff000011112222")
    )
    prompt = build_ci_fix_prompt(tmp_path, "42")
    assert calls[0][:4] == ["gh", "run", "view", "42"]
    assert "The run tested abc123def456; local HEAD is ffff00001111." in prompt


def test_clear_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ci_fix.shutil, "which", lambda _name: None)
    with pytest.raises(CIFixError, match=r"cli\.github\.com"):
        build_ci_fix_prompt(tmp_path)
    monkeypatch.setattr(ci_fix.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(ci_fix.subprocess, "run", _fake_run([], runs=[]))
    with pytest.raises(CIFixError, match="No failed GitHub Actions runs found for branch main"):
        build_ci_fix_prompt(tmp_path)
    with pytest.raises(CIFixError, match="Usage"):
        build_ci_fix_prompt(tmp_path, "latest")


def test_long_log_keeps_the_output_before_the_error_not_the_cleanup():
    stamp = "build\tUNKNOWN STEP\t2026-10-24T20:32:32.0186548Z "
    log = "\n".join(
        [stamp + f"install line {i}" for i in range(2000)]
        + [
            stamp + "FAILED tests/test_x.py::test_y",
            stamp + "##[error]Process completed with exit code 1.",
        ]
        + [stamp + f"post-job cleanup {i}" for i in range(2000)]
    )
    text = ci_fix.excerpt(log)
    assert len(text) <= ci_fix.MAX_LOG_CHARS + 100
    assert "FAILED tests/test_x.py::test_y\n##[error]Process completed" in text
    assert "== build ==" in text
    assert "post-job cleanup" not in text
    assert "2026-10-24T" not in text
