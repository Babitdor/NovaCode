"""Authored skills must remain discoverable through Nova's real skill loader."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from deepagents.middleware.skills import _parse_skill_metadata

from novacode_cli.skills.schema import prepare_skill_content


def _metadata(content: str) -> dict:
    return yaml.safe_load(content.split("---", 2)[1])


@pytest.mark.parametrize(
    "content",
    [
        "# Factory\n\nRun the workflow.",
        "---\nname: wrong-name\n---\n\nRun the workflow.",
        "---\ndescription: Use when: deploying to B:\\Summer Project\n---\n\nRun the workflow.",
        "\ufeff# Factory\n\nRun the workflow.",
    ],
)
def test_repairs_metadata_and_real_loader_accepts_it(content: str):
    output = prepare_skill_content(content, "factory")
    metadata = _metadata(output)
    assert metadata["name"] == "factory"
    assert isinstance(metadata["description"], str)
    assert metadata["description"]
    assert metadata["version"] == "1.0.0"
    assert metadata["tags"] == []
    assert "Run the workflow." in output
    assert _parse_skill_metadata(output, "/factory/SKILL.md", "factory") is not None
    assert prepare_skill_content(output, "factory") == output


def test_preserves_multiline_and_nested_metadata_and_body():
    content = """---
name: factory
description: |
  First line.
  Second line.
metadata:
  category: orchestration
allowed-tools: [read_file]
tags: [review, build]
---

# Instructions
1. Review all four outputs.
"""
    output = prepare_skill_content(content, "factory")
    metadata = _metadata(output)
    assert metadata["description"] == "First line.\nSecond line.\n"
    assert metadata["metadata"] == {"category": "orchestration"}
    assert metadata["allowed-tools"] == ["read_file"]
    assert metadata["tags"] == ["review", "build"]
    assert output.endswith("# Instructions\n1. Review all four outputs.\n")


@pytest.mark.parametrize("name", ["Factory", "my_skill", "../factory", "a" * 65])
def test_rejects_invalid_directory_names(name: str):
    with pytest.raises(ValueError, match="kebab-case"):
        prepare_skill_content("# Body", name)


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("", "markdown"),
        ("---\nname: factory\ndescription: d\n---\n", "markdown"),
        ("---\nmetadata: [broken\n---\nbody", "invalid YAML"),
        ("---\n[a, b]\n---\nbody", "mapping"),
        ("---\ntags: not-a-list\n---\nbody", "tags"),
        ("---\ndescription: " + "x" * 1025 + "\n---\nbody", "1024"),
    ],
)
def test_invalid_content_is_rejected(content: str, error: str):
    with pytest.raises(ValueError, match=error):
        prepare_skill_content(content, "factory")


@pytest.mark.asyncio
@pytest.mark.parametrize("inline", [True, False])
async def test_generator_repairs_file_and_inline_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    inline: bool,
):
    from deepagents import graph

    from novacode_cli.skills import skill_creation

    raw = "---\nname: wrong-name\n---\n\n# Factory\nRun all reviews."
    target = tmp_path / "factory" / "SKILL.md"

    class Agent:
        async def ainvoke(self, _messages: object) -> dict:
            if not inline:
                target.parent.mkdir()
                target.write_text(raw, encoding="utf-8")
            return {"messages": [SimpleNamespace(content=raw if inline else "Written.")]}

    monkeypatch.setattr(graph, "create_deep_agent", lambda **_kwargs: Agent())
    monkeypatch.setattr(skill_creation, "create_model", object)
    output = await skill_creation._generate_skill("factory", tmp_path)
    assert output == target.read_text(encoding="utf-8")
    assert _metadata(output)["name"] == "factory"
    assert _parse_skill_metadata(output, str(target), "factory") is not None


@pytest.fixture
def api(tmp_path: Path):
    from novacode_cli.commands import create_server

    settings = SimpleNamespace(
        get_global_skills_dir=lambda: tmp_path / "global",
        get_project_skills_dir=lambda: tmp_path / "project",
    )
    handler = SimpleNamespace(settings=settings, _send_json=Mock(), _send_error=Mock())
    owner = next(
        v
        for v in vars(create_server).values()
        if isinstance(v, type) and hasattr(v, "_create_skill")
    )
    for method in ("_create_skill", "_update_skill"):
        setattr(handler, method, getattr(owner, method).__get__(handler))
    return handler


@pytest.mark.parametrize("scope", ["global", "project"])
def test_web_create_and_edit_save_loadable_skills(api: SimpleNamespace, scope: str):
    api._create_skill({"name": "factory", "content": "# Run reviews", "scope": scope})
    assert api._send_json.call_args.args[1] == 201
    root = getattr(api.settings, f"get_{scope}_skills_dir")()
    target = root / "factory/SKILL.md"
    assert _parse_skill_metadata(target.read_text(encoding="utf-8"), str(target), "factory")
    api._update_skill("factory", {"content": "---\nname: wrong\n---\nUpdated steps"})
    assert _metadata(target.read_text(encoding="utf-8"))["name"] == "factory"
    assert target.read_text(encoding="utf-8").endswith("Updated steps\n")


def test_web_invalid_edit_preserves_existing_skill(api: SimpleNamespace):
    api._create_skill({"name": "factory", "content": "# Steps"})
    target = api.settings.get_global_skills_dir() / "factory/SKILL.md"
    original = target.read_bytes()
    api._update_skill("factory", {"content": "---\nmetadata: [broken\n---\nbody"})
    assert api._send_error.call_args.args[1] == 400
    assert target.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [".nova", ".agents", ".claude"])
async def test_file_tool_write_guards_skill_metadata(tmp_path: Path, library: str):
    from novacode_cli.backends import OptimizedFilesystemBackend

    backend = OptimizedFilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    path = f"/{library}/skills/factory/SKILL.md"
    result = await backend.awrite(path, "# Factory\nRun reviews.")
    assert not result.error
    target = tmp_path / library / "skills/factory/SKILL.md"
    original = target.read_bytes()
    assert _parse_skill_metadata(original.decode(), str(target), "factory")
    result = await backend.awrite(path, "---\nmetadata: [broken\n---\nbody")
    assert result.error
    assert target.read_bytes() == original
    raw = "# Standalone source document"
    assert not backend.write("/SKILL.md", raw).error
    assert (tmp_path / "SKILL.md").read_text(encoding="utf-8") == raw


def test_proposals_validate_before_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config.config import Settings
    from novacode_cli.hermes import skill_proposals

    ledger = SimpleNamespace(propose=Mock(return_value=SimpleNamespace(reference="test-ref")))
    monkeypatch.setattr(
        Settings,
        "from_environment",
        lambda: SimpleNamespace(
            ensure_user_skills_dir=lambda: tmp_path,
        ),
    )
    monkeypatch.setattr(skill_proposals, "main_agent_learning_ledger", lambda: ledger)
    assert skill_proposals.propose_skill("factory", "# Review steps") == "test-ref"
    staged = ledger.propose.call_args.kwargs["content"]
    assert _parse_skill_metadata(staged, "/factory/SKILL.md", "factory")
    ledger.propose.reset_mock()
    with pytest.raises(ValueError, match="invalid YAML"):
        skill_proposals.propose_skill("factory", "---\nmetadata: [broken\n---\nbody")
    ledger.propose.assert_not_called()


def test_cli_fallback_writes_a_loadable_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.skills import skill_creation

    async def fail_generation(*_args: object) -> None:
        return None

    monkeypatch.setattr(
        skill_creation,
        "Settings",
        SimpleNamespace(
            from_environment=lambda: SimpleNamespace(
                ensure_user_skills_dir=lambda _agent: tmp_path
            ),
        ),
    )
    monkeypatch.setattr(skill_creation, "_generate_skill", fail_generation)
    skill_creation._create("factory", "nova-agent", global_scope=True, ask=False)
    target = tmp_path / "factory/SKILL.md"
    assert _parse_skill_metadata(target.read_text(encoding="utf-8"), str(target), "factory")
