"""Memory wiki maintenance preserves content while repairing discoverability."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from novacode_cli.memory.wiki import maintain_wiki


def test_maintenance_indexes_links_and_preserves_sources(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    memories.mkdir()
    originals = {
        "nova-config.md": "# Nova configuration\n\nKeep exact flag `--x` and correction A.\n",
        "nova-runtime.md": "# Nova runtime\n\nDifferent constraints, same project.\n",
        "amy-design.md": "# Amy design\n\nOnly applies to Amy.\n",
    }
    for name, content in originals.items():
        (memories / name).write_text(content, encoding="utf-8")
    (tmp_path / "agent.md").write_text("# User\nNever lose my preference.\n", encoding="utf-8")
    result = maintain_wiki(tmp_path)
    home = (memories / "INDEX.md").read_text(encoding="utf-8")
    assert len(home) <= 2000
    assert "wiki-nova.md" in home
    assert "wiki-web.md" in home
    for name, content in originals.items():
        page = (memories / name).read_text(encoding="utf-8")
        assert page.startswith(content)
        assert "INDEX.md" in page
        assert (Path(result["backup"]) / "memories" / name).read_text(encoding="utf-8") == content
    assert "nova-runtime.md" in (memories / "nova-config.md").read_text(encoding="utf-8")
    assert (tmp_path / "agent.md").read_text(
        encoding="utf-8"
    ) == "# User\nNever lose my preference.\n"
    assert result["broken_links"] == []


def test_second_pass_is_idempotent(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "topic.md").write_text("# Topic\nUnique content.\n", encoding="utf-8")
    maintain_wiki(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = maintain_wiki(tmp_path)
    assert result["changed"] == 0
    assert result["backup"] is None
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_habit_reviews_keep_the_stable_checklist(tmp_path: Path) -> None:
    from novacode_cli.hermes.memory_tiers import record_habit

    checklist = "# Habits\n<!-- nova-memory-wiki:checklist -->\n## Final Review\nKeep evidence.\n"
    (tmp_path / "HABITS.md").write_text(checklist, encoding="utf-8")
    record_habit(tmp_path, "- Measure the rendered pixels before changing visual geometry.")
    assert (tmp_path / "HABITS.md").read_text(encoding="utf-8") == checklist
    lesson = tmp_path / "memories" / "habits-lessons.md"
    assert "rendered pixels" in lesson.read_text(encoding="utf-8")


def test_new_lessons_survive_navigation_refresh(tmp_path: Path) -> None:
    from novacode_cli.hermes.memory_tiers import record_lesson

    memories = tmp_path / "memories"
    memories.mkdir()
    page = memories / "parser.md"
    page.write_text("# Parser\nOriginal requirement.\n", encoding="utf-8")
    maintain_wiki(tmp_path)
    record_lesson(tmp_path, "parser", "- Preserve the newly approved Unicode parser constraint.")
    assert "newly approved Unicode" in page.read_text(encoding="utf-8")
    maintain_wiki(tmp_path)
    assert "newly approved Unicode" in page.read_text(encoding="utf-8")


def test_compaction_keeps_an_exact_recoverable_original(tmp_path: Path) -> None:
    from novacode_cli.hermes.memory_tiers import compact_memory_file

    original = "# Long memory\n" + "Unique historical constraint.\n" * 700
    path = tmp_path / "long.md"
    path.write_text(original, encoding="utf-8")
    assert compact_memory_file(path)
    backups = list((tmp_path / ".history").glob("*.txt"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert backups[0].name in path.read_text(encoding="utf-8")


def test_existing_managed_name_is_not_overwritten(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    memories.mkdir()
    page = memories / "wiki-nova.md"
    page.write_text("# My handwritten map\nKeep this.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="user-authored"):
        maintain_wiki(tmp_path)
    assert page.read_text(encoding="utf-8") == "# My handwritten map\nKeep this.\n"


def test_broken_links_reported_and_scoped_project_pages_reachable(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    scoped = memories / "projects" / "project-a"
    scoped.mkdir(parents=True)
    page = scoped / "parser.md"
    page.write_text("# Parser\n\n[Missing](missing.md)\n", encoding="utf-8")
    result = maintain_wiki(tmp_path)
    assert any("missing.md" in item for item in result["broken_links"])
    assert "projects/project-a/parser.md" in (memories / "wiki-projects.md").read_text(
        encoding="utf-8"
    )
    assert "../../INDEX.md" in page.read_text(encoding="utf-8")


async def test_dream_handles_habits_and_uses_wiki_index(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from novacode_cli.commands.dream_handler import handle_dream_command

    (tmp_path / "HABITS.md").write_text("# Habits\nKeep evidence.\n", encoding="utf-8")
    result = await handle_dream_command(SimpleNamespace(), agent_dir=tmp_path, emit=lambda _: None)
    assert isinstance(result, str)
    assert "/memories/HABITS.md" in result
    assert "/memories/memories/INDEX.md" in result
    assert "/memories/MEMORY.md" not in result
    assert "memory_wiki" in result
    assert "nova-memory-wiki:checklist" in result
    assert "unique" in result
    assert "backup" in result.lower()


async def test_dream_snapshots_an_already_organized_wiki(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from novacode_cli.commands.dream_handler import handle_dream_command

    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "topic.md").write_text("# Topic\nExact constraint.\n", encoding="utf-8")
    maintain_wiki(tmp_path)
    existing = set((tmp_path / "history" / "wiki-backups").iterdir())
    result = await handle_dream_command(SimpleNamespace(), agent_dir=tmp_path, emit=lambda _: None)
    assert isinstance(result, str)
    created = set((tmp_path / "history" / "wiki-backups").iterdir()) - existing
    assert len(created) == 1
    backup = created.pop()
    assert (backup / "memories/topic.md").read_bytes() == (memories / "topic.md").read_bytes()


async def test_dream_finds_project_only_memory_and_preserves_scope(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from novacode_cli.commands.dream_handler import handle_dream_command

    project = tmp_path / "memories/projects/check-out-a"
    project.mkdir(parents=True)
    (project / "parser.md").write_text("# Parser\nOnly for project A.\n", encoding="utf-8")
    result = await handle_dream_command(SimpleNamespace(), agent_dir=tmp_path, emit=lambda _: None)
    assert isinstance(result, str)
    assert "projects/check-out-a/parser.md" in (tmp_path / "memories/wiki-projects.md").read_text(
        encoding="utf-8"
    )


async def test_dream_does_not_write_while_planning(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from novacode_cli.commands.dream_handler import handle_dream_command

    messages: list[str] = []
    result = await handle_dream_command(
        SimpleNamespace(plan_mode_enabled=True),
        agent_dir=tmp_path,
        emit=messages.append,
    )
    assert result is True
    assert any("Exit plan mode" in message for message in messages)
    assert list(tmp_path.iterdir()) == []  # noqa: ASYNC240 - inspect a tiny temporary fixture


def test_stale_read_set_stops_before_writing(tmp_path: Path) -> None:
    from novacode_cli.memory.wiki import _commit

    page = tmp_path / "agent.md"
    page.write_bytes(b"A newer user preference")
    with pytest.raises(RuntimeError, match="changed during maintenance"):
        _commit(tmp_path, {page: b"Older preference"}, {page: "Proposed rewrite"})
    assert page.read_bytes() == b"A newer user preference"
    assert not (tmp_path / "history").exists()


def test_concurrent_write_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novacode_cli.memory import wiki

    memories = tmp_path / "memories"
    memories.mkdir()
    page = memories / "topic.md"
    page.write_text("# Original\n", encoding="utf-8")
    real_backup = wiki._backup

    def race(root: Path, originals: dict[Path, bytes | None]) -> Path:
        backup = real_backup(root, originals)
        page.write_text("# Concurrent correction\n", encoding="utf-8")
        return backup

    monkeypatch.setattr(wiki, "_backup", race)
    with pytest.raises(RuntimeError, match="changed during maintenance"):
        maintain_wiki(tmp_path)
    assert page.read_text(encoding="utf-8") == "# Concurrent correction\n"
    backups = list((tmp_path / "history" / "wiki-backups").glob("*/memories/topic.md"))
    assert backups[0].read_text(encoding="utf-8") == "# Original\n"


def test_atomic_failure_retains_source_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    memories = tmp_path / "memories"
    memories.mkdir()
    page = memories / "topic.md"
    page.write_text("# Original\n", encoding="utf-8")

    def fail_replace(*_args: object) -> None:
        message = "injected replace failure"
        raise OSError(message)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        maintain_wiki(tmp_path)
    assert page.read_text(encoding="utf-8") == "# Original\n"
    assert list((tmp_path / "history" / "wiki-backups").glob("*/memories/topic.md"))
    assert not list(memories.glob(".wiki-*"))


def test_wiki_tool_is_bound_to_its_agent(tmp_path: Path) -> None:
    import json

    from novacode_cli.tools.memory_wiki_tools import create_memory_wiki_tool

    tool = create_memory_wiki_tool(tmp_path / "agent-a")
    result = json.loads(tool.invoke({"action": "maintain"}))
    assert result["broken_links"] == []
    assert (tmp_path / "agent-a" / "memories" / "INDEX.md").is_file()
    assert not (tmp_path / "agent-b").exists()
    from novacode_cli.config.plan_mode import BLOCKED_TOOLS

    assert tool.name in BLOCKED_TOOLS


def test_maps_cover_research_other_projects_and_encoded_paths(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    memories.mkdir()
    for name in ("thesis-chapter.md", "oracle-project.md", "notes with spaces.md"):
        (memories / name).write_text(f"# {name}\nImportant requirements.\n", encoding="utf-8")
    result = maintain_wiki(tmp_path)
    assert result["broken_links"] == []
    assert "thesis-chapter.md" in (memories / "wiki-research.md").read_text(encoding="utf-8")
    assert "oracle-project.md" in (memories / "wiki-projects.md").read_text(encoding="utf-8")
    assert "notes%20with%20spaces.md" in (memories / "wiki-operations.md").read_text(
        encoding="utf-8"
    )


def test_generated_maps_do_not_compete_with_factual_memories(tmp_path: Path) -> None:
    from tests.test_memory_search import _mw

    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "nova-config.md").write_text(
        "# Config\nKeep parser mode strict.\n", encoding="utf-8"
    )
    (memories / "wiki-handwritten.md").write_text(
        "# My facts\nAn actual lesson.\n", encoding="utf-8"
    )
    maintain_wiki(tmp_path)
    corpus = _mw(tmp_path)._load_memory_corpus()
    assert "nova-config" in corpus
    assert "wiki-handwritten" in corpus
    assert "wiki-nova" not in corpus
    assert "wiki-operations" not in corpus


def test_check_ignores_example_links_in_code_and_remote_urls(tmp_path: Path) -> None:
    from novacode_cli.memory.wiki import validate_wiki

    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "topic.md").write_text(
        "# Examples\n`[Example](missing.md)`\n```md\n[X](absent.md)\n```\n"
        "[Remote](https://example.invalid/page.md)\n",
        encoding="utf-8",
    )
    assert validate_wiki(tmp_path) == []


@pytest.mark.parametrize(
    "block",
    [
        "<!-- nova-memory-wiki:start -->\nmissing end",
        "<!-- nova-memory-wiki:start -->\n## Review\nUnique correction\n"
        "<!-- nova-memory-wiki:end -->\n",
    ],
)
def test_malformed_or_contaminated_navigation_is_preserved(tmp_path: Path, block: str) -> None:
    memories = tmp_path / "memories"
    memories.mkdir()
    path = memories / "topic.md"
    path.write_text("# Topic\n" + block, encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(ValueError, match=r"markers|lesson"):
        maintain_wiki(tmp_path)
    assert path.read_bytes() == original


def test_commit_rejects_outside_destination(tmp_path: Path) -> None:
    from novacode_cli.memory.wiki import _commit

    root = tmp_path / "agent"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"Keep the unrelated file")
    with pytest.raises(ValueError, match="escapes"):
        _commit(root, {outside: outside.read_bytes()}, {outside: "Overwrite"})
    assert outside.read_bytes() == b"Keep the unrelated file"


def test_tool_reports_failed_maintenance_without_claiming_completion(tmp_path: Path) -> None:
    import json

    from novacode_cli.tools.memory_wiki_tools import create_memory_wiki_tool

    memories = tmp_path / "memories"
    memories.mkdir()
    page = memories / "wiki-nova.md"
    page.write_text("# My map\n", encoding="utf-8")
    result = json.loads(create_memory_wiki_tool(tmp_path).invoke({"action": "maintain"}))
    assert result["complete"] is False
    assert "user-authored" in result["error"]
    assert page.read_text(encoding="utf-8") == "# My map\n"


def test_interrupted_pass_can_resume_and_originals_can_be_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    memories = tmp_path / "memories"
    memories.mkdir()
    originals = {"a.md": b"# First\r\nKeep exact CRLF.\r\n", "b.md": b"# Second\nKeep B.\n"}
    for name, raw in originals.items():
        (memories / name).write_bytes(raw)
    real_replace = os.replace
    calls = 0

    def interrupt_second(source: str, target: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            message = "interrupted second write"
            raise OSError(message)
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", interrupt_second)
    with pytest.raises(OSError, match="interrupted"):
        maintain_wiki(tmp_path)
    initial_backup = next((tmp_path / "history" / "wiki-backups").iterdir())
    monkeypatch.setattr(os, "replace", real_replace)
    assert maintain_wiki(tmp_path)["broken_links"] == []
    assert maintain_wiki(tmp_path)["changed"] == 0
    for name, raw in originals.items():
        assert (memories / name).read_bytes().startswith(raw)
        saved = (initial_backup / "memories" / name).read_bytes()
        assert saved == raw
        (memories / name).write_bytes(saved)
        assert (memories / name).read_bytes() == raw


@settings(max_examples=20, deadline=None)
@given(st.text(alphabet="abc XYZ012_-\néΩ", min_size=1, max_size=250))
def test_arbitrary_topic_text_survives_and_repeated_pass_is_stable(body: str) -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as temp:
        root = Path(temp)
        memories = root / "memories"
        memories.mkdir()
        original = "# Source\n" + body + "\n"
        page = memories / "source.md"
        page.write_text(original, encoding="utf-8")
        maintain_wiki(root)
        assert page.read_text(encoding="utf-8").startswith(original)
        assert maintain_wiki(root)["changed"] == 0
