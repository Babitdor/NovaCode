"""The offline summary must distinguish grader failures from harness errors."""

import json

import pytest

from scripts.analyze import TrialStatus, analyze_trial, print_summary


def make_trial(tmp_path, name, *, reward=None, exception=None, stopped=False, truncated=False):
    trial = tmp_path / name
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    if reward is not None:
        (trial / "verifier" / "reward.txt").write_text(str(reward))
    if exception:
        (trial / "exception.txt").write_text(exception)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "agent_result": {
                    "n_input_tokens": 100,
                    "n_cache_tokens": 80,
                    "n_output_tokens": 10,
                },
                "exception_info": {"type": "RuntimeError"} if exception else None,
            }
        )
    )
    (trial / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "agent": {
                    "extra": {
                        "time_limit_min_told_to_agent": 7.5,
                        "timing": {
                            "wall_seconds": 12.0,
                            "model_seconds": 7.0,
                            "tool_seconds": 5.0,
                            "tools": {"bash": {"calls": 1, "seconds": 5.0}},
                        },
                    }
                },
                "steps": [
                    {
                        "source": "agent",
                        "message": (
                            "I stopped: I was repeating the same tool call" if stopped else "Done"
                        ),
                        "extra": {"finish_reason": "length" if truncated else "stop"},
                    }
                ],
            }
        )
    )
    return trial


@pytest.mark.asyncio
async def test_summary_separates_task_failure_and_execution_error(tmp_path, capsys):
    failed = await analyze_trial(make_trial(tmp_path, "failed", reward=0, stopped=True))
    truncated = await analyze_trial(make_trial(tmp_path, "truncated", reward=0, truncated=True))
    errored = await analyze_trial(
        make_trial(tmp_path, "errored", reward=0, exception="RuntimeError")
    )
    passed = await analyze_trial(make_trial(tmp_path, "passed", reward=1))

    assert failed.status == TrialStatus.FAILED
    assert failed.failure_kind == "task failure"
    assert failed.loop_stopped
    assert truncated.truncated_final
    assert errored.failure_kind == "execution error"
    assert passed.status == TrialStatus.COMPLETED

    print_summary([failed, truncated, errored, passed])
    output = capsys.readouterr().out
    assert "Task failures: 2" in output
    assert "Execution errors: 1" in output
    assert "Loop stops: 1" in output
    assert "Truncated endings: 1" in output
    assert "Final model response: truncated without a tool call" in output
    assert "Tokens: input=400, cached=320, uncached=80, output=40" in output
    assert "uncached=20" in output
    assert "Time: wall=12.0s, model=7.0s, tools=5.0s" in output
    assert "Slowest tool: bash (5.0s)" in output
    assert "Agent time budget told: 7.5 min" in output


@pytest.mark.asyncio
async def test_missing_result_keeps_usage_unknown(tmp_path):
    trial_dir = tmp_path / "interrupted"
    trial_dir.mkdir()
    trial = await analyze_trial(trial_dir)
    assert trial.status == TrialStatus.PENDING
    assert trial.input_tokens is None
    assert trial.failure_kind is None


@pytest.mark.asyncio
async def test_result_exception_is_error_without_exception_file(tmp_path):
    trial_dir = make_trial(tmp_path, "error-in-result", reward=0)
    result_path = trial_dir / "result.json"
    result = json.loads(result_path.read_text())
    result["exception_info"] = {"type": "RuntimeError"}
    result_path.write_text(json.dumps(result))

    trial = await analyze_trial(trial_dir)
    assert trial.status == TrialStatus.FAILED
    assert trial.failure_kind == "execution error"


@pytest.mark.asyncio
async def test_graph_step_limit_is_reported_as_harness_cutoff(tmp_path, capsys):
    trial_dir = make_trial(tmp_path, "cutoff", reward=0)
    (trial_dir / "trial.log").write_text(
        "Selected strategy: _ModalDirect\nagent hit the step limit; grading what it left\n"
    )

    trial = await analyze_trial(trial_dir)
    assert trial.status == TrialStatus.FAILED
    assert trial.failure_kind == "harness cutoff"
    assert trial.graph_step_limit_hit

    print_summary([trial])
    output = capsys.readouterr().out
    assert "Harness cutoffs: 1" in output
    assert "Graph step limit: hit" in output
