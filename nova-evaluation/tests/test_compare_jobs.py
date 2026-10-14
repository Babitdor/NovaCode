"""Paired benchmark comparison refuses mismatched trial settings."""

import json

import pytest

from scripts.compare_jobs import compare_jobs


def _trial(
    job,
    name,
    reward,
    *,
    effort="off",
    model="ollama/model",
    ref="task-rev",
    source="terminal-bench/tb2",
    environment="modal",
):
    trial = job / f"{name}__run"
    (trial / "verifier").mkdir(parents=True)
    (trial / "config.json").write_text(
        json.dumps(
            {
                "task": {
                    "name": f"terminal-bench/{name}",
                    "source": source,
                    "ref": ref,
                },
                "agent": {
                    "name": "NovaCodeWrapper",
                    "model_name": model,
                    "kwargs": {"reasoning_effort": effort} if effort is not None else {},
                },
                "environment": {"type": environment},
            }
        )
    )
    (trial / "verifier" / "reward.txt").write_text(str(reward))
    return trial


def test_compare_reports_gain_and_regression_on_matching_tasks(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    _trial(before, "fixed", 0)
    _trial(after, "fixed", 1)
    _trial(before, "regressed", 1)
    _trial(after, "regressed", 0)
    _trial(before, "baseline-only", 1)
    assert compare_jobs(before, after) == [
        ("terminal-bench/fixed", False, True),
        ("terminal-bench/regressed", True, False),
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"effort": None},
        {"model": "ollama/other"},
        {"ref": "changed-rev"},
        {"source": "terminal-bench/other"},
        {"environment": "docker"},
    ],
)
def test_compare_rejects_mismatched_settings(tmp_path, change):
    before, after = tmp_path / "before", tmp_path / "after"
    _trial(before, "task", 0)
    _trial(after, "task", 1, **change)
    with pytest.raises(ValueError, match="mismatch"):
        compare_jobs(before, after)


def test_compare_rejects_unknown_task_revision(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    _trial(before, "task", 0, ref=None)
    _trial(after, "task", 1, ref=None)
    with pytest.raises(ValueError, match="missing.*task_ref"):
        compare_jobs(before, after)


def test_compare_rejects_duplicate_and_missing_task_ids(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    _trial(before, "task", 0)
    _trial(after, "other", 1)
    with pytest.raises(ValueError, match="no shared tasks"):
        compare_jobs(before, after)
    duplicate = _trial(after, "task", 1)
    duplicate.rename(after / "task__retry")
    _trial(after, "task", 1)
    with pytest.raises(ValueError, match="duplicate"):
        compare_jobs(before, after)
