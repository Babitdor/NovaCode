"""Tests for /dream memory consolidation.

Covers the bug where /dream looked in ~/.nova (root) instead of the real agent
memory dir, operating on the semantic surface (agent.md + memories/ topic
files), plus the legacy-UI → emit conversion.
"""

from __future__ import annotations

import types

from novacode_cli.commands.dream_handler import handle_dream_command


def _sink():
    lines: list[str] = []
    return lines, lines.append


async def test_no_memory_returns_true_and_emits(tmp_path, capsys):
    lines, emit = _sink()
    result = await handle_dream_command(
        types.SimpleNamespace(), "nova-agent", agent_dir=tmp_path, emit=emit
    )
    assert result is True
    assert any("No memory files found" in ln for ln in lines)
    # Output goes to the emit sink, not stdout (TUI-safe).
    assert capsys.readouterr().out == ""


async def test_with_tier_files_returns_virtual_path_prompt(tmp_path):
    (tmp_path / "agent.md").write_text("# Agent Memory\nLikes terse answers.\n", encoding="utf-8")
    lines, emit = _sink()

    result = await handle_dream_command(
        types.SimpleNamespace(), "nova-agent", agent_dir=tmp_path, emit=emit
    )
    assert isinstance(result, str)
    # The prompt targets the virtual /memories/ route (not a raw OS path) and
    # names the real tier file.
    assert "/memories/" in result
    assert "/memories/agent.md" in result
    assert any("Memory Consolidation" in ln for ln in lines)


async def test_index_uses_full_virtual_path(tmp_path):
    mem = tmp_path / "memories"
    mem.mkdir()
    (mem / "INDEX.md").write_text("- [A](a.md) — hook", encoding="utf-8")
    (tmp_path / "agent.md").write_text("u", encoding="utf-8")
    lines, emit = _sink()

    result = await handle_dream_command(
        types.SimpleNamespace(), "nova-agent", agent_dir=tmp_path, emit=emit
    )
    assert isinstance(result, str)
    # The consolidation references the index at its real virtual location.
    assert "/memories/memories/INDEX.md" in result


async def test_topic_files_are_listed(tmp_path):
    mem = tmp_path / "memories"
    mem.mkdir()
    (mem / "auth-design.md").write_text("notes", encoding="utf-8")
    lines, emit = _sink()

    result = await handle_dream_command(
        types.SimpleNamespace(), "nova-agent", agent_dir=tmp_path, emit=emit
    )
    assert isinstance(result, str)
    assert "/memories/memories/" in result
    assert "auth-design.md" in result


async def test_dream_command_runs_with_isolated_read_only_agent(tmp_path, monkeypatch):
    from novacode_cli.commands import CommandRegistry, dream_handler

    calls = {}
    active_model = object()
    main_agent = object()

    async def prompt(*_args, **_kwargs):
        return "Review memory proposals."

    async def execute(*args, **kwargs):
        calls["execute"] = (args, kwargs)

    def create(model, agent_dir):
        calls["create"] = (model, agent_dir)
        return "read-only dream agent"

    from novacode_cli.agents import dream_agent
    from novacode_cli.config.config import settings
    from novacode_cli.ui import execution

    monkeypatch.setattr(dream_handler, "handle_dream_command", prompt)
    monkeypatch.setattr(dream_agent, "create_dream_agent", create)
    monkeypatch.setattr(execution, "execute_task", execute)
    monkeypatch.setattr(settings, "get_agent_dir", lambda _assistant_id: tmp_path)

    registry = CommandRegistry()
    dream_handler.register_commands(registry)
    result = await registry.get("dream")(
        types.SimpleNamespace(
            session_state=types.SimpleNamespace(_model=active_model),
            assistant_id="nova",
            agent=main_agent,
            token_tracker=None,
        )
    )

    assert result is True
    assert calls["create"] == (active_model, tmp_path)
    args, kwargs = calls["execute"]
    assert args[0] == "Review memory proposals."
    assert args[1] == "read-only dream agent"
    assert args[2] == "dream-agent"
    assert kwargs["is_subagent"] is True
    assert args[1] is not main_agent
