"""The main agent's base instructions always ship with the package."""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from novacode_cli import prompts
from novacode_cli.hermes.activation import active_artifacts


def test_main_templates_ignore_global_overrides_and_activated_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(prompts, "PROMPT_HISTORY_DIR", tmp_path)
    monkeypatch.setattr(
        prompts,
        "_override_env",
        Environment(
            loader=FileSystemLoader(tmp_path),
            autoescape=select_autoescape(default=False),
        ),
    )
    for name in (
        "core_agent_system.jinja",
        "System_Prompt_Nova.jinja",
        "nova_standing_instructions.jinja",
    ):
        folder = tmp_path / name.removesuffix(".jinja")
        folder.mkdir()
        (folder / "active.jinja").write_text("OVERRIDE MAIN IDENTITY", encoding="utf-8")
        assert prompts.render_template(name) == prompts._env.get_template(name).render()
        token = active_artifacts.set([("prompt", name, "ACTIVATED MAIN IDENTITY")])
        try:
            rendered = prompts.render_template(name)
            assert "OVERRIDE MAIN IDENTITY" not in rendered
            assert "ACTIVATED MAIN IDENTITY" not in rendered
            assert rendered == prompts._env.get_template(name).render()
        finally:
            active_artifacts.reset(token)


def test_non_main_template_keeps_explicit_activation():
    token = active_artifacts.set(
        [("prompt", "longterm_memory.jinja", "custom memory instructions")]
    )
    try:
        assert prompts.render_template("longterm_memory.jinja") == "custom memory instructions"
    finally:
        active_artifacts.reset(token)


def test_main_and_continuation_share_packaged_standing_rules():
    standing = prompts.render_template("nova_standing_instructions.jinja")
    for template in ("core_agent_system.jinja", "System_Prompt_Nova.jinja"):
        rendered = prompts.render_template(template)
        assert rendered.count("<standing_instructions>") == 1
        assert standing in rendered
        for rule in (
            "write_memory",
            "append=True",
            "recent_plans",
            "3\u20137 concrete outcomes",
            "/memories/HABITS.md",
            "memory_wiki",
            "Never replace a document with a small note",
            "existing authorization",
            "the checks that actually passed",
        ):
            assert rule in rendered
