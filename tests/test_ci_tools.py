"""CI partition coverage, subprocess failures, and release authorization checks."""

import io
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

from scripts import check_release_ci, ci_regression


def test_real_manifest_partitions_without_lost_or_duplicate_selectors():
    weights = json.loads(ci_regression.MANIFEST.read_text(encoding="utf-8"))
    groups = ci_regression.partition(weights, 4)
    flattened = [selector for group in groups for selector in group]
    assert len(flattened) == len(set(flattened)) == len(weights)
    assert set(flattened) == set(weights)
    root = ci_regression.MANIFEST.parent.parent
    assert all((root / selector.split("::")[0]).exists() for selector in flattened)
    assert groups == ci_regression.partition(weights, 4)
    totals = [sum(weights[selector] for selector in group) for group in groups]
    assert max(totals) - min(totals) <= max(weights.values())


@pytest.mark.parametrize("count", [0, -1, 3])
def test_invalid_shard_count(count):
    with pytest.raises(ValueError):
        ci_regression.partition({"a": 2, "b": 1}, count)


def test_runner_preserves_failure_exit_code_and_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"tests/example.py": 1}', encoding="utf-8")
    monkeypatch.setattr(ci_regression, "MANIFEST", manifest)
    monkeypatch.setattr(sys, "argv", ["ci_regression.py", "--shard", "1", "--shards", "1"])
    real_popen = subprocess.Popen
    commands = []

    def failing_child(command, **kwargs):
        commands.append(command)
        return real_popen(
            [sys.executable, "-c", "print('test failure'); raise SystemExit(7)"], **kwargs
        )

    monkeypatch.setattr(subprocess, "Popen", failing_child)
    assert ci_regression.main() == 7
    assert commands[0][-1] == "tests/example.py"
    assert "test failure" in Path("test-results/regression-1.log").read_text()


def run(**changes):
    return {
        "head_sha": "release",
        "head_branch": "main",
        "event": "push",
        "run_number": 1,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://example.test/run",
        **changes,
    }


def test_release_requires_success_for_exact_commit():
    successful = run()
    assert check_release_ci.require_success([successful], "release") is successful


@pytest.mark.parametrize(
    "changes",
    [
        {"head_sha": "other"},
        {"head_branch": "feature"},
        {"event": "pull_request"},
        {"status": "in_progress"},
        {"conclusion": "failure"},
        {"conclusion": "cancelled"},
        {"conclusion": "skipped"},
        {"conclusion": None},
    ],
)
def test_release_rejects_wrong_or_unsuccessful_runs(changes):
    with pytest.raises(ValueError):
        check_release_ci.require_success([run(**changes)], "release")


def test_release_does_not_reuse_old_success_after_failed_rerun():
    with pytest.raises(ValueError):
        check_release_ci.require_success(
            [run(), run(run_attempt=2, conclusion="failure")], "release"
        )


def test_release_rejects_missing_ci():
    with pytest.raises(ValueError):
        check_release_ci.require_success([], "release")


def test_release_gate_queries_checkout_commit_and_ci_workflow(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "release\n")
    requests = []

    def response(request, timeout):
        requests.append(request)
        assert timeout == 30
        return io.BytesIO(json.dumps({"workflow_runs": [run()]}).encode())

    monkeypatch.setattr(check_release_ci.urllib.request, "urlopen", response)
    check_release_ci.main()
    url = urllib.parse.urlparse(requests[0].full_url)
    assert url.path == "/repos/owner/repo/actions/workflows/ci.yml/runs"
    assert urllib.parse.parse_qs(url.query) == {
        "head_sha": ["release"],
        "branch": ["main"],
        "event": ["push"],
        "per_page": ["100"],
    }
    assert "test-token" not in capsys.readouterr().out
