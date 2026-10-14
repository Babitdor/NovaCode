"""Negative controls for the custom changed-line coverage gate."""

import json
from types import SimpleNamespace

import pytest

from scripts import verify_context_learning as verify


def test_coverage_gate_rejects_unexecuted_new_module_and_accepts_executed(tmp_path, monkeypatch):
    source = tmp_path / "novacode_cli" / "new.py"
    source.parent.mkdir()
    source.write_text("def risky():\n    return 42\n")
    monkeypatch.setattr(verify, "ROOT", tmp_path)

    def git(command, **kwargs):
        output = "" if "diff" in command else "novacode_cli/new.py\n"
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(verify.subprocess, "run", git)
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"files": {}}))
    result = verify.changed_line_coverage(report)
    assert result["passed"] is False
    assert result["covered"] == 0 and result["total"] == 2
    report.write_text(
        json.dumps(
            {"files": {"novacode_cli/new.py": {"executed_lines": [1, 2], "missing_lines": []}}}
        )
    )
    assert verify.changed_line_coverage(report)["passed"] is True


def test_coverage_gate_empty_report_is_error_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    monkeypatch.setattr(
        verify.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="")
    )
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"files": {}}))
    with pytest.raises(RuntimeError, match="no executable lines"):
        verify.changed_line_coverage(report)


def test_coverage_gate_counts_staged_and_unstaged_lines(tmp_path, monkeypatch):
    source = tmp_path / "novacode_cli" / "new.py"
    source.parent.mkdir()
    source.write_text("def good():\n    return 2\ndef bad():\n    return 999\n")
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    patch = "+++ b/novacode_cli/new.py\n@@ -1,4 +1,4 @@\n+1\n@@ -4,1 +4,1 @@\n+1\n"

    def git(command, **kwargs):
        if "diff" in command:
            assert "HEAD" in command, "staged changes are otherwise invisible"
            return SimpleNamespace(stdout=patch)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(verify.subprocess, "run", git)
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {"files": {"novacode_cli/new.py": {"executed_lines": [2], "missing_lines": [4]}}}
        )
    )
    result = verify.changed_line_coverage(report)
    assert result["covered"] == 1 and result["total"] == 2
    assert result["passed"] is False


def test_default_verdict_rejects_partial_or_failed_acceptance_evidence():
    results = {"partial_checks_passed": True, "source_unchanged": True}
    assert verify.acceptance_verdict(results, complete_mode=True) is False
    assert len(results["missing_acceptance_gates"]) == len(verify.REQUIRED_GATES) + 1
    results.update(
        {
            "tests": {"exit_code": 0},
            "lint_new_modules": {"exit_code": 0},
            "changed_line_coverage": {"passed": True},
            "types_new_modules": {"exit_code": 0},
            "mutations": [{"killed": True} for _ in range(8)],
            "shuffled_affected": {"exit_code": 0},
            "full_suite_runs": [{"exit_code": 0}, {"exit_code": 0}],
        }
    )
    assert verify.acceptance_verdict(results, complete_mode=False) is False
    assert verify.acceptance_verdict(results, complete_mode=True) is True
    results["full_suite_runs"][1]["exit_code"] = 1
    assert verify.acceptance_verdict(results, complete_mode=True) is False
    results["full_suite_runs"][1]["exit_code"] = 0
    results["mutations"].pop()
    assert verify.acceptance_verdict(results, complete_mode=True) is False


def test_source_hash_includes_runtime_templates_and_packaged_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    for name in (
        "pyproject.toml",
        "uv.lock",
        "docs/context-learning/CONTEXT_LEARNING_SPEC.md",
        "docs/context-learning/CONTEXT_LEARNING_CONTRACT.md",
    ):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(name, encoding="utf-8")
    template = tmp_path / "novacode_cli" / "prompts" / "system.jinja"
    template.parent.mkdir(parents=True)
    template.write_text("first", encoding="utf-8")
    asset = tmp_path / "novacode_cli" / "assets" / "icon.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image-one")

    def files(command, **kwargs):
        assert "--cached" in command and "--others" in command
        return SimpleNamespace(
            stdout=b"novacode_cli/prompts/system.jinja\0novacode_cli/assets/icon.png\0"
        )

    monkeypatch.setattr(verify.subprocess, "run", files)
    first = verify.source_hash()
    template.write_text("second", encoding="utf-8")
    second = verify.source_hash()
    assert second != first
    asset.write_bytes(b"image-two")
    assert verify.source_hash() != second
