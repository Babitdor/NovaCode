"""Smart-loop signals for /ralph: checklist completion, stuck-break, verify gate.

Pins the pure decision policy and the parsing helpers — the loop integration
(both fg/bg loops) drives these, so locking them locks the behaviour.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from novacode_cli.commands import ralph_handler as rh
from novacode_cli.commands.ralph_handler import (
    _ChecklistState,
    _VerifyResult,
)
from novacode_cli.commands.ralph_handler import (
    _ralph_iteration_decision as decide,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# -- checklist parsing --------------------------------------------------------


def test_checklist_regex_parses_checked_and_unchecked() -> None:
    assert rh._CHECKLIST_RE.match("- [x] done").group(1).lower() == "x"
    assert rh._CHECKLIST_RE.match("- [X] done caps").group(1).lower() == "x"
    assert rh._CHECKLIST_RE.match("  * [ ] todo here").group(2) == "todo here"
    assert rh._CHECKLIST_RE.match("not a checklist line") is None


def test_checklist_state_progress() -> None:
    cl = _ChecklistState([(True, "a"), (False, "b"), (True, "c")])
    assert cl.total == 3
    assert cl.done == 2
    assert not cl.all_done
    assert cl.remaining() == ["b"]
    assert _ChecklistState([(True, "a")]).all_done
    assert not _ChecklistState([]).all_done  # empty checklist is never "done"


def test_read_checklist_from_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / ".nova" / "ralph"
    d.mkdir(parents=True)
    (d / "checklist.md").write_text("- [x] one\n- [ ] two\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cl = rh._read_checklist()
    assert cl.total == 2
    assert cl.done == 1


# -- decision policy ----------------------------------------------------------


def test_complete_only_when_all_done_and_verify_ok() -> None:
    all_done = _ChecklistState([(True, "a"), (True, "b")])
    assert decide(all_done, 0, None)[0] == "complete"
    assert decide(all_done, 0, _VerifyResult(passed=True, summary="ok"))[0] == "complete"
    # all checklist items done but verification failing -> keep iterating to fix
    assert decide(all_done, 0, _VerifyResult(passed=False, summary="2 failed"))[0] == "continue"


def test_stuck_only_when_no_progress_and_not_done() -> None:
    partial = _ChecklistState([(True, "a"), (False, "b")])
    assert decide(partial, rh._MAX_NO_CHANGE, None)[0] == "stuck"
    assert decide(partial, rh._MAX_NO_CHANGE - 1, None)[0] == "continue"
    # a finished checklist is never "stuck", even with no new file changes
    done = _ChecklistState([(True, "a")])
    assert decide(done, 5, None)[0] == "complete"


def test_no_checklist_runs_to_max_iterations_unless_stuck() -> None:
    none = _ChecklistState([])
    assert decide(none, 0, None)[0] == "continue"  # progressing, no checklist
    assert decide(none, rh._MAX_NO_CHANGE, None)[0] == "stuck"  # spinning, no checklist


# -- prompt extras carry state forward ---------------------------------------


def test_render_extras_expose_state() -> None:
    extras = rh._ralph_render_extras(
        no_change_streak=1, last_verify=_VerifyResult(passed=False, summary="boom")
    )
    assert extras["no_change_streak"] == 1
    assert extras["verify_passed"] is False
    assert extras["verify_summary"] == "boom"
    assert extras["checklist_path"] == rh.RALPH_CHECKLIST_PATH


def test_bookkeeping_writes_do_not_count_as_progress_in_an_unborn_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    code = tmp_path / "new.py"
    code.write_text("version = 1", encoding="utf-8")
    baseline = rh._git_change_signature(str(tmp_path))
    assert baseline
    progress = tmp_path / rh.RALPH_PROGRESS_PATH
    progress.parent.mkdir(parents=True)
    progress.write_text("Iteration notes", encoding="utf-8")
    assert rh._git_change_signature(str(tmp_path)) == baseline
    code.write_text("version = 2", encoding="utf-8")
    assert rh._git_change_signature(str(tmp_path)) != baseline
