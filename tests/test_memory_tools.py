"""``read_memory`` / ``write_memory`` must not hard-fail on a guessed type.

The model reads Nova's memory guide, sees topic files under
``/memories/memories/<topic>.md``, and calls::

    read_memory(memory_type="topic", path="/memories/memories/foo.md")

``memory_type`` was a ``Literal["user", "project"]``, so Pydantic rejected
"topic" before the tool body ran — no error dict, no recovery, just a raw
ValidationError. But when a virtual ``path`` is given it alone determines the
file, so the type is irrelevant; the schema still advertises the enum while
validation tolerates the guess. Without a path the type is validated and a
helpful ``success: False`` explains what to pass instead.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def memory_home(tmp_path, monkeypatch):
    """Point agent memory at a temp dir so tests never touch ~/.nova."""
    from novacode_cli.config.config import settings

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    monkeypatch.setattr(settings, "get_agent_dir", lambda _aid: agent_dir)
    monkeypatch.setattr(settings, "project_root", tmp_path)
    return agent_dir


def test_schema_still_advertises_the_enum():
    """The model must keep seeing the allowed values in the tool schema."""
    from novacode_cli.tools.memory_tools import read_memory

    prop = read_memory.args_schema.model_json_schema()["properties"]["memory_type"]
    assert prop["enum"] == ["user", "project"]


def test_topic_type_with_a_virtual_path_is_tolerated(memory_home):
    """The exact failing call: memory_type="topic" + a topic file path."""
    from novacode_cli.tools.memory_tools import read_memory

    topic_dir = memory_home / "memories"
    topic_dir.mkdir()
    (topic_dir / "linked-worktree-python-env.md").write_text(
        "# Linked Worktree Python Env\n\nVENV_MARKER\n", encoding="utf-8"
    )

    result = read_memory.invoke(
        {
            "memory_type": "topic",
            "path": "/memories/memories/linked-worktree-python-env.md",
        }
    )

    assert result["success"] is True
    assert result["exists"] is True
    assert "VENV_MARKER" in result["content"]
    assert result["path"] == "/memories/memories/linked-worktree-python-env.md"


def test_write_with_a_virtual_path_tolerates_a_guessed_type(memory_home):  # noqa: ARG001
    from novacode_cli.tools.memory_tools import read_memory, write_memory

    result = write_memory.invoke(
        {
            "content": "MARKER_CONTENT",
            "memory_type": "topic",
            "path": "/memories/memories/notes.md",
        }
    )
    assert result["success"] is True
    read_back = read_memory.invoke(
        {"memory_type": "topic", "path": "/memories/memories/notes.md"}
    )
    assert read_back["success"] is True
    assert "MARKER_CONTENT" in read_back["content"]


def test_unknown_type_without_a_path_returns_a_helpful_error():
    """No path ⇒ the type must be exact, but the failure is recoverable."""
    from novacode_cli.tools.memory_tools import read_memory

    result = read_memory.invoke({"memory_type": "topic"})

    assert result["success"] is False
    assert "user" in result["error"]
    assert "project" in result["error"]
    # Points the model at the correct way to name a topic file.
    assert "/memories/memories/" in result["error"]


def test_default_user_memory_still_resolves(memory_home):
    from novacode_cli.tools.memory_tools import read_memory

    (memory_home / "agent.md").write_text("USER_PREFS\n", encoding="utf-8")
    result = read_memory.invoke({})
    assert result["success"] is True
    assert result["path"] == "/memories/agent.md"
    assert "USER_PREFS" in result["content"]


def test_project_memory_resolves_from_the_workspace(memory_home):
    from novacode_cli.tools.memory_tools import read_memory

    nova_dir = memory_home.parent / ".nova"
    nova_dir.mkdir()
    (nova_dir / "NOVA.md").write_text("PROJECT_RULES\n", encoding="utf-8")
    result = read_memory.invoke({"memory_type": "project"})
    assert result["success"] is True
    assert result["path"] == "/project-memory/NOVA.md"
    assert "PROJECT_RULES" in result["content"]


def test_tool_schema_is_json_serializable():
    """Guard against an annotation that breaks tool-schema generation."""
    from novacode_cli.tools.memory_tools import read_memory, write_memory

    for tool in (read_memory, write_memory):
        json.dumps(tool.args_schema.model_json_schema())
