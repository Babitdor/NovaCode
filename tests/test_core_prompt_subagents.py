"""The core system prompt tells the agent the task tool is synchronous."""

from __future__ import annotations

from novacode_cli.prompts import render_template


def _core_prompt() -> str:
    # Lenient Jinja Undefined => no kwargs needed for this static-block assertion.
    return render_template("core_agent_system.jinja")


def test_core_prompt_has_subagents_block():
    out = _core_prompt()
    assert "<subagents>" in out
    assert "</subagents>" in out


def test_core_prompt_states_task_is_synchronous_and_no_wait():
    out = _core_prompt()
    assert "synchronous" in out
    assert "Never end your turn" in out


def test_subagents_block_follows_todo_management():
    out = _core_prompt()
    assert "<todo_management>" in out
    assert "<subagents>" in out
    assert out.index("<subagents>") > out.index("</todo_management>")


def test_core_prompt_has_artifacts_block():
    out = _core_prompt()
    assert "<artifacts>" in out
    assert "</artifacts>" in out
    # Proactive framing + the tool names it must match (see CORE_TOOLS).
    assert "proactively" in out
    for name in ("create_artifact", "update_artifact", "list_artifacts"):
        assert name in out


def test_async_prompt_collects_required_results_before_completion():
    out = render_template("core_agent_system.jinja", has_async_agents=True)
    assert "wait_for_task(task_id)" in out
    assert "two waits per user message" in out and "end your turn" in out
    assert "claim its work is finished" in out
