"""Learning reviews happen when a task ends, and know how it turned out.

The original trigger reviewed every N tool calls. Run across twelve unrelated
Terminal-Bench tasks, that reviewed one 200-step task about twenty times, wrote
98 bullets about it, and recorded approaches that went on to fail — a review
mid-task cannot know the outcome. Each was also an extra model call carrying the
whole conversation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.hermes import memory_tiers as mt
from novacode_cli.hermes.review import ReviewRunner, outcome_text, session_outcome
from novacode_cli.hermes.skill_manager import SkillManager
from novacode_cli.hermes.tracker import ToolUsageTracker


class _Store:
    """The minimum of a store the tracker needs."""

    def __init__(self) -> None:
        self.data: dict = {}

    async def aput(self, ns, key, value):  # noqa: ANN001, ANN201
        self.data[(ns, key)] = value

    async def aget(self, ns, key):  # noqa: ANN001, ANN201
        value = self.data.get((ns, key))
        return SimpleNamespace(value=value) if value is not None else None


async def _runner(entries: list[dict], mode: str | None = None) -> ReviewRunner:
    store = _Store()
    await store.aput(("nova", "tool_history"), "history", {"entries": entries})
    await store.aput(("nova", "tool_counter"), "counter", {"count": len(entries)})
    return ReviewRunner(
        store,  # type: ignore[arg-type]
        ToolUsageTracker(store, enabled=True),  # type: ignore[arg-type]
        SkillManager(store, enabled=True),  # type: ignore[arg-type]
        review_threshold=4,
        enabled=True,
        mode=mode,
    )


def _calls(*pattern: bool) -> list[dict]:
    return [{"tool": "execute", "success": ok} for ok in pattern]


# ── when a review fires ────────────────────────────────────────────────────


def test_a_long_run_of_work_does_not_trigger_a_review_mid_task() -> None:
    async def go() -> bool:
        return await (await _runner(_calls(*[True] * 30))).should_review()

    assert asyncio.run(go()) is False, "the default mode must not review on tool-call count"


def test_working_through_an_error_does_trigger_one() -> None:
    async def go() -> bool:
        return await (await _runner(_calls(False, False, False, True))).should_review()

    assert asyncio.run(go()) is True


def test_failures_that_are_not_yet_resolved_do_not() -> None:
    """Mid-failure there is nothing to learn yet: the fix is not known."""

    async def go() -> bool:
        return await (await _runner(_calls(True, False, False, False))).should_review()

    assert asyncio.run(go()) is False


def test_periodic_mode_still_reviews_on_count() -> None:
    async def go() -> bool:
        return await (await _runner(_calls(*[True] * 4), mode="periodic")).should_review()

    assert asyncio.run(go()) is True


# ── the outcome ────────────────────────────────────────────────────────────


def _session(*shell_outputs: str) -> list:
    messages: list = [HumanMessage(content="make the tests pass")]
    for i, text in enumerate(shell_outputs):
        messages.append(AIMessage(content="", tool_calls=[{"id": f"c{i}", "name": "bash", "args": {}}]))
        messages.append(ToolMessage(content=text, tool_call_id=f"c{i}", name="bash"))
    return messages


def test_the_outcome_is_the_last_check_not_the_last_command() -> None:
    status, evidence = session_outcome(
        _session("3 failed, 1 passed in 0.2s", "4 passed in 0.2s", "total 8\n-rw-r--r-- a.py")
    )
    assert status == "passed" and "4 passed" in evidence


def test_a_failing_last_check_is_a_failed_outcome() -> None:
    status, evidence = session_outcome(_session("4 passed in 0.1s", "2 failed, 2 passed in 0.3s"))
    assert status == "failed" and "2 failed" in evidence


def test_no_check_at_all_is_unknown() -> None:
    assert session_outcome(_session("total 8\n-rw-r--r-- a.py"))[0] == "unknown"
    assert session_outcome([HumanMessage(content="hi")])[0] == "unknown"


def test_the_review_is_told_the_outcome() -> None:
    from novacode_cli.prompts import render_template

    def prompt(status: str) -> str:
        return render_template(
            "nova_review.jinja",
            tool_call_count=10,
            prior_lessons="",
            recovered_from_error=False,
            clean_win=False,
            existing_topics="",
            outcome=outcome_text(status, "2 failed, 2 passed"),
        )

    assert "FAILED" in prompt("failed") and "do not propose a skill" in prompt("failed")
    assert "passed" in prompt("passed")
    assert "nothing in it is" in prompt("unknown")


def test_failed_work_shares_nothing_however_it_is_labelled(tmp_path: Path) -> None:
    fact = "- `apt-get install` needs `apt-get update` first in a fresh container"
    mt.update_from_review(tmp_path, "", [{"topic": "apt", "bullets": fact, "scope": "general"}], project="task-a")
    mt.update_from_review(
        tmp_path, "", [{"topic": "apt", "bullets": fact, "scope": "general"}], project="task-b", shareable=False
    )
    shared = [p for p in mt.lesson_dir(tmp_path).glob("*.md") if p.name != "INDEX.md"]
    assert shared == [], "a session whose last check failed must not promote anything"
    assert (mt.lesson_dir(tmp_path, "task-b") / "apt.md").exists(), "it is still kept for the project"


def test_a_failed_outcome_saves_no_skill() -> None:
    async def go() -> AsyncMock:
        runner = await _runner([])
        runner._skill_manager.maybe_create_from_review = AsyncMock()  # type: ignore[method-assign]
        runner._outcome = ("failed", "2 failed")
        await runner._apply_review_content("<skill><name>x</name></skill>")
        return runner._skill_manager.maybe_create_from_review  # type: ignore[return-value]

    asyncio.run(go()).assert_not_awaited()


# ── an outside verdict ─────────────────────────────────────────────────────


def _shared(tmp_path: Path) -> list[str]:
    return [
        b
        for f in mt.lesson_dir(tmp_path).glob("*.md")
        if f.name != "INDEX.md"
        for b in mt._bullets(f.read_text(encoding="utf-8"))
    ]


def test_a_project_judged_a_failure_takes_its_confirmations_back(tmp_path: Path) -> None:
    """An agent's own check printed ALL PASS while the real tests failed 4 of 13."""
    fact = "- `all_gather` is not autograd-aware so gradients do not flow through its output"
    for project in ("task-a", "task-b"):
        mt.update_from_review(tmp_path, "", [{"topic": "torch", "bullets": fact, "scope": "general"}], project=project)
    assert len(_shared(tmp_path)) == 1

    assert mt.mark_project_outcome(tmp_path, "task-b", passed=False) == 1
    assert _shared(tmp_path) == []
    assert not (mt.lesson_dir(tmp_path) / "torch.md").exists()
    assert "torch" not in (mt.lesson_dir(tmp_path) / "INDEX.md").read_text(encoding="utf-8")


def test_a_fact_two_other_projects_still_back_survives(tmp_path: Path) -> None:
    fact = "- `apt-get install` needs `apt-get update` first in a fresh container"
    for project in ("task-a", "task-b"):
        mt.update_from_review(tmp_path, "", [{"topic": "apt", "bullets": fact, "scope": "general"}], project=project)
    mt.mark_project_outcome(tmp_path, "task-a", passed=True)
    assert len(_shared(tmp_path)) == 1, "a passing verdict retracts nothing"


def test_a_failed_project_cannot_confirm_anything_later(tmp_path: Path) -> None:
    fact = "- `apt-get install` needs `apt-get update` first in a fresh container"
    mt.update_from_review(tmp_path, "", [{"topic": "apt", "bullets": fact, "scope": "general"}], project="task-a")
    mt.mark_project_outcome(tmp_path, "task-a", passed=False)
    mt.update_from_review(tmp_path, "", [{"topic": "apt", "bullets": fact, "scope": "general"}], project="task-b")
    assert _shared(tmp_path) == []


# ── pitfalls ───────────────────────────────────────────────────────────────


def test_a_pitfall_becomes_one_symptom_first_bullet() -> None:
    text = (
        '<pitfall topic="bare-container-python" scope="general">'
        "<symptom>bash: python3: command not found</symptom>"
        "<cause>The image ships without Python.</cause>"
        "<fix>apt-get update && apt-get install -y python3</fix></pitfall>"
    )
    (lesson,) = mt.parse_review_response(text)["lessons"]
    assert lesson["topic"] == "bare-container-python" and lesson["scope"] == "general"
    assert lesson["bullets"].startswith("- **Symptom:** bash: python3: command not found")
    assert "**Fix:** apt-get update && apt-get install -y python3" in lesson["bullets"]
    assert "\n" not in lesson["bullets"], "one bullet, so it is recalled as a unit"


def test_a_pitfall_without_a_symptom_is_dropped() -> None:
    text = '<pitfall topic="x"><fix>do the thing that works</fix></pitfall>'
    assert mt.parse_review_response(text)["lessons"] == []
