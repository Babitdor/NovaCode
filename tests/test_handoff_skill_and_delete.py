"""Actual skill resolution and deletion boundaries, using isolated project libraries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from novacode_cli import handoff
from novacode_cli import ui_events as ev
from novacode_cli.agents.handoff import HandoffMiddleware
from novacode_cli.commands.handoff_handler import handle_handoff_command
from novacode_cli.commands.skill_invoke import _resolve_skill_invocation
from novacode_cli.core.agent_loop import iterate_agent_events
from novacode_cli.handoff import HandoffMemory, HandoffRun, document, prepare
from novacode_cli.handoff import load_handoff_skill as actual_loader
from tests.test_handoff import state
from tests.test_prime import Model

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from langchain.agents.middleware.types import ModelRequest


def isolate_skill_libraries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import novacode_cli.commands.skill_invoke as invocation

    class Settings:
        project_root = tmp_path

        @staticmethod
        def from_environment() -> Settings:
            return Settings()

        @staticmethod
        def ensure_user_skills_dir(_assistant_id: str) -> Path:
            return tmp_path / "user"

        @staticmethod
        def get_project_skills_dir() -> Path:
            return tmp_path / ".nova/skills"

        @staticmethod
        def get_project_skills_dirs() -> list[Path]:
            return [tmp_path / ".agents/skills", tmp_path / ".nova/skills"]

        @staticmethod
        def get_global_claude_skills_dir() -> Path:
            return tmp_path / "claude"

        @staticmethod
        def get_shared_skills_dir() -> Path:
            return tmp_path / "shared"

    monkeypatch.setattr(invocation, "Settings", Settings)
    monkeypatch.setattr("novacode_cli.skills.skills_prefs.effective_disabled", set)
    monkeypatch.setattr(handoff, "load_handoff_skill", actual_loader)


def test_clean_install_uses_the_actual_bundled_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    isolate_skill_libraries(tmp_path, monkeypatch)
    invocation = _resolve_skill_invocation("handoff", None, state(tmp_path), "nova-agent")
    assert invocation is not None
    assert invocation.source == "bundled"
    assert invocation.pinned_prompt is None
    assert 'Include a "suggested skills" section' in invocation.prompt
    assert "temporary directory of the user's OS" in invocation.prompt
    assert "Do not duplicate content" in invocation.prompt


def test_project_skill_overrides_user_and_bundled_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    isolate_skill_libraries(tmp_path, monkeypatch)
    for directory, sentence in (
        (tmp_path / "user/handoff", "User handoff instructions."),
        (tmp_path / ".agents/skills/handoff", "Use the project's special handoff format."),
    ):
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            "---\nname: handoff\ndescription: Handoff instructions.\n---\n" + sentence,
            encoding="utf-8",
        )
    run = HandoffRun(state(tmp_path), "Continue portfolio")
    assert run.skill.source == "project"
    assert "project's special handoff format" in run.skill.prompt
    assert "User handoff instructions" not in run.skill.prompt


async def test_actual_skill_prompt_drives_document_saved_by_real_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    isolate_skill_libraries(tmp_path, monkeypatch)
    recorded = []
    original = HandoffMiddleware.prepare

    def capture(request: ModelRequest) -> ModelRequest:
        prepared = original(request)
        recorded.append(str(prepared.system_message.content))
        return prepared

    monkeypatch.setattr(HandoffMiddleware, "prepare", staticmethod(capture))
    content = (
        "# Portfolio handoff\n\n## Suggested skills\nfrontend-design\n\n" + "Useful context. " * 400
    )
    graph = create_agent(
        Model(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "handoff_save",
                            "id": "handoff",
                            "args": {"title": "Portfolio", "content": content},
                        }
                    ],
                ),
                AIMessage("Saved the handoff."),
            ]
        ),
        middleware=[HandoffMiddleware()],
        checkpointer=InMemorySaver(),
    )
    session = state(tmp_path)
    events = [
        e async for e in iterate_agent_events("Build portfolio", graph, "nova-agent", session)
    ]
    assert not [e for e in events if isinstance(e, ev.Error)]
    assert "--- SKILL: handoff ---" in recorded[0]
    assert "temporary directory of the user's OS" in recorded[0]
    assert "Nova's storage adaptation" in recorded[0]
    saved = HandoffMemory(str(tmp_path)).get(session.handoff_latest["id"])
    assert saved["content"] == content  # No old 4,000-character structured-field truncation.
    assert content in document(saved)
    assert saved["skill_source"] == "bundled"
    assert not list(tmp_path.glob("*.md")), "Handoffs must stay outside the project files"  # noqa: ASYNC240 - Local test fixture.


async def test_disabled_skill_stays_disabled_without_using_the_bundled_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    isolate_skill_libraries(tmp_path, monkeypatch)
    monkeypatch.setattr("novacode_cli.skills.skills_prefs.effective_disabled", lambda: {"handoff"})
    assert _resolve_skill_invocation("handoff", None, state(tmp_path), "nova-agent") is None
    graph = create_agent(
        Model(responses=[AIMessage("Work response")]),
        middleware=[HandoffMiddleware()],
        checkpointer=InMemorySaver(),
    )
    session = state(tmp_path)
    events = [e async for e in iterate_agent_events("Task", graph, "nova-agent", session)]
    assert session.handoff_latest["summary_kind"].startswith("snapshot")
    assert any(
        isinstance(e, ev.ContextMessage) and "skill unavailable or disabled" in e.message
        for e in events
    )


def test_delete_requires_project_local_confirmation_and_clears_selection(tmp_path: Path):
    session = state(tmp_path)
    run = HandoffRun(session, "Portfolio")
    prepare(session, run.record["id"])
    memory = HandoffMemory(str(tmp_path))
    prompt = handle_handoff_command(session, f"delete {run.record['id']} confirm")
    assert "Confirm deletion" in prompt
    assert memory.get(run.record["id"]) is not None
    assert "Handoff deleted" in handle_handoff_command(
        session, f"delete {run.record['id']} confirm"
    )
    assert memory.get(run.record["id"]) is None
    assert memory.store.get(memory.namespace, run.record["id"]) is None
    assert memory.store.get(memory.deleted_namespace, run.record["id"]).value == {"deleted": True}
    assert session.handoff_pending is None
    assert session.handoff_latest is None
    run.finish("complete")
    assert memory.records() == []
    assert "deleted" in run.error


def test_wrong_project_cannot_delete_and_repeated_deletion_is_safe(tmp_path: Path):
    original = tmp_path / "original"
    other = tmp_path / "other"
    original.mkdir()
    other.mkdir()
    session = state(original)
    run = HandoffRun(session, "Portfolio")
    assert "not found" in handle_handoff_command(state(other), f"delete {run.record['id']}")
    memory = HandoffMemory(str(original))
    assert memory.get(run.record["id"])
    assert memory.delete(run.record["id"])
    assert not memory.delete(run.record["id"])


def test_deletion_between_save_checks_cannot_resurrect_a_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run = HandoffRun(state(tmp_path), "Portfolio")
    memory = run.memory
    original_put = memory.store.put

    def interleave(
        _self: object, namespace: tuple[str, ...], key: str, value: dict[str, object]
    ) -> None:
        if namespace == memory.namespace:
            original_put(memory.deleted_namespace, key, {"deleted": True})
        return original_put(namespace, key, value)

    monkeypatch.setattr(type(memory.store), "put", interleave)
    assert not memory.save(run.record)
    assert memory.get(run.record["id"]) is None
    assert memory.store.get(memory.namespace, run.record["id"]) is None
    assert memory.records() == []


def test_failed_delete_reports_storage_error_without_touching_project_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run = HandoffRun(state(tmp_path), "Portfolio")
    sentinel = tmp_path / "project.txt"
    sentinel.write_text("keep")
    session = run.session
    handle_handoff_command(session, f"delete {run.record['id']}")

    def fail_delete(*_args: object) -> None:
        message = "private storage exception detail"
        raise OSError(message)

    monkeypatch.setattr(type(run.memory.store), "delete", fail_delete)
    response = handle_handoff_command(session, f"delete {run.record['id']} confirm")
    assert "storage unavailable" in response
    assert "private storage" not in response
    assert sentinel.read_text() == "keep"


def test_deleted_pending_selection_is_not_recreated_in_a_new_run(tmp_path: Path):
    session = state(tmp_path)
    run = HandoffRun(session, "Old handoff")
    prepare(session, run.record["id"])
    HandoffMemory(str(tmp_path)).delete(run.record["id"])
    new = HandoffRun(session, "New request")
    assert new.source is None
    assert new.record["parent_id"] is None
