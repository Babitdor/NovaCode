"""Tests for Nova's semantic memory tier — agent.md + memories/ topic files.

Covers:
- ``ensure_memory_tiers`` scaffolding (the memories/ dir)
- ``update_user_model`` — section merge/replace into agent.md
- ``record_lesson`` — topic files + INDEX pointer, newest-first, deduped
- ``compact_memory_file`` — keep-newest (head) compaction
- ``migrate_legacy_tiers`` — one-time USER.md/MEMORY.md import
- ``parse_review_response`` — the new <user_model> / <lesson> contract
"""

from pathlib import Path

import pytest

from novacode_cli.hermes.memory_tiers import (
    MAX_MEMORY_CHARS,
    compact_memory_file,
    ensure_memory_tiers,
    migrate_legacy_tiers,
    parse_review_response,
    record_lesson,
    update_user_model,
)


@pytest.fixture
def temp_agent_dir(tmp_path: Path) -> Path:
    """Create a temporary agent directory for testing."""
    agent_dir = tmp_path / ".nova" / "test_agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    return agent_dir


class TestEnsureMemoryTiers:
    """Semantic-tier scaffolding."""

    def test_creates_memories_dir(self, temp_agent_dir):
        """ensure_memory_tiers should create the memories/ directory."""
        ensure_memory_tiers(temp_agent_dir)
        assert (temp_agent_dir / "memories").is_dir()

    def test_does_not_create_legacy_files(self, temp_agent_dir):
        """It must NOT recreate the orphaned USER.md / MEMORY.md tiers."""
        ensure_memory_tiers(temp_agent_dir)
        assert not (temp_agent_dir / "USER.md").exists()
        assert not (temp_agent_dir / "MEMORY.md").exists()


class TestCompactMemoryFile:
    """Memory file compaction at size limits (keep-newest/head)."""

    def test_no_compaction_under_limit(self, temp_agent_dir):
        test_file = temp_agent_dir / "test.md"
        test_file.write_text("small content", encoding="utf-8")
        assert not compact_memory_file(test_file)
        assert test_file.read_text(encoding="utf-8") == "small content"

    def test_compaction_over_limit(self, temp_agent_dir):
        test_file = temp_agent_dir / "test.md"
        test_file.write_text("A" * (MAX_MEMORY_CHARS + 1000), encoding="utf-8")
        assert compact_memory_file(test_file)
        content = test_file.read_text(encoding="utf-8")
        assert len(content) < MAX_MEMORY_CHARS
        assert "truncated" in content

    def test_keeps_newest_head(self, temp_agent_dir):
        """Compaction keeps the head (newest), drops the tail (oldest)."""
        test_file = temp_agent_dir / "test.md"
        head = "# Title\n\n## NEWEST\n" + ("n" * (MAX_MEMORY_CHARS // 2))
        tail = "\n## OLDEST\n" + ("o" * MAX_MEMORY_CHARS)
        test_file.write_text(head + tail, encoding="utf-8")
        assert compact_memory_file(test_file)
        content = test_file.read_text(encoding="utf-8")
        assert "NEWEST" in content
        assert "OLDEST" not in content

    def test_non_existent_file(self, temp_agent_dir):
        assert not compact_memory_file(temp_agent_dir / "nonexistent.md")

    def test_exact_limit(self, temp_agent_dir):
        test_file = temp_agent_dir / "test.md"
        test_file.write_text("A" * MAX_MEMORY_CHARS, encoding="utf-8")
        assert not compact_memory_file(test_file)


class TestUpdateUserModel:
    """agent.md user-model updates."""

    def test_create_file_if_missing(self, temp_agent_dir):
        update_user_model(temp_agent_dir, "## Communication Style\n- Terse")
        agent_md = temp_agent_dir / "agent.md"
        assert agent_md.exists()
        assert "- Terse" in agent_md.read_text(encoding="utf-8")

    def test_replace_section(self, temp_agent_dir):
        (temp_agent_dir / "agent.md").write_text(
            "# Agent Memory\n\n## Communication Style\n- old\n", encoding="utf-8"
        )
        update_user_model(temp_agent_dir, "## Communication Style\n- Very formal")
        content = (temp_agent_dir / "agent.md").read_text(encoding="utf-8")
        assert "- Very formal" in content
        assert "- old" not in content

    def test_add_new_section(self, temp_agent_dir):
        (temp_agent_dir / "agent.md").write_text("# Agent Memory\n", encoding="utf-8")
        update_user_model(temp_agent_dir, "## Custom\n- value")
        content = (temp_agent_dir / "agent.md").read_text(encoding="utf-8")
        assert "## Custom" in content
        assert "- value" in content

    def test_plain_bullets_go_under_notes(self, temp_agent_dir):
        update_user_model(temp_agent_dir, "- A loose preference")
        content = (temp_agent_dir / "agent.md").read_text(encoding="utf-8")
        assert "## Notes" in content
        assert "- A loose preference" in content


class TestRecordLesson:
    """memories/<topic>.md + INDEX pointer."""

    def test_creates_topic_file_and_index(self, temp_agent_dir):
        record_lesson(temp_agent_dir, "testing", "- Use pytest -x for fast feedback")
        topic = temp_agent_dir / "memories" / "testing.md"
        index = temp_agent_dir / "memories" / "INDEX.md"
        assert topic.exists()
        assert "pytest -x" in topic.read_text(encoding="utf-8")
        assert "testing.md" in index.read_text(encoding="utf-8")

    def test_slugifies_topic(self, temp_agent_dir):
        record_lesson(temp_agent_dir, "TUI Slash Commands!", "- a real lesson")
        assert (temp_agent_dir / "memories" / "tui-slash-commands.md").exists()

    def test_dedupes_bullets(self, temp_agent_dir):
        record_lesson(temp_agent_dir, "testing", "- same lesson")
        record_lesson(temp_agent_dir, "testing", "- same lesson")
        content = (temp_agent_dir / "memories" / "testing.md").read_text("utf-8")
        assert content.count("- same lesson") == 1

    def test_index_pointer_not_duplicated(self, temp_agent_dir):
        record_lesson(temp_agent_dir, "testing", "- lesson one")
        record_lesson(temp_agent_dir, "testing", "- lesson two")
        index = (temp_agent_dir / "memories" / "INDEX.md").read_text("utf-8")
        assert index.count("testing.md") == 1

    def test_newest_first(self, temp_agent_dir):
        record_lesson(temp_agent_dir, "t", "- first lesson")
        record_lesson(temp_agent_dir, "t", "- second lesson")
        content = (temp_agent_dir / "memories" / "t.md").read_text("utf-8")
        assert content.index("second lesson") < content.index("first lesson")


class TestParseReviewResponse:
    """The new <user_model> / <lesson topic=…> contract."""

    def test_parses_user_model_and_lessons(self):
        resp = (
            "<user_model>\n## Style\n- terse\n</user_model>\n"
            '<lesson topic="testing">\n- pytest -x\n</lesson>\n'
            '<lesson topic="ci">\n- cache deps\n</lesson>'
        )
        parsed = parse_review_response(resp)
        assert "## Style" in parsed["user_model"]
        assert {l["topic"] for l in parsed["lessons"]} == {"testing", "ci"}

    def test_untagged_lesson_defaults_topic(self):
        parsed = parse_review_response("<lesson>\n- a fact\n</lesson>")
        assert parsed["lessons"][0]["topic"] == "lessons"

    def test_unstructured_content_is_dropped(self):
        # Unstructured prose with no XML blocks must NOT be dumped as a lesson
        # (this was the source of raw thinking-trace pollution in lessons.md).
        parsed = parse_review_response("just some freeform text")
        assert parsed["lessons"] == []
        assert parsed["user_model"] == ""

    def test_empty(self):
        parsed = parse_review_response("")
        assert parsed["user_model"] == ""
        assert parsed["lessons"] == []


class TestMigrateLegacyTiers:
    """One-time USER.md / MEMORY.md → injected surface."""

    def test_migrates_and_renames(self, temp_agent_dir):
        (temp_agent_dir / "USER.md").write_text(
            "# USER\n\n## Communication Style\n- Prefers bullets\n", encoding="utf-8"
        )
        (temp_agent_dir / "MEMORY.md").write_text(
            "# MEMORY\n\n## Facts\n- Chose SSE for /chat\n", encoding="utf-8"
        )
        assert migrate_legacy_tiers(temp_agent_dir) is True

        agent_md = (temp_agent_dir / "agent.md").read_text(encoding="utf-8")
        assert "- Prefers bullets" in agent_md

        lessons = (temp_agent_dir / "memories" / "lessons.md").read_text("utf-8")
        assert "Chose SSE for /chat" in lessons

        # Consumed files renamed → second run is a no-op.
        assert (temp_agent_dir / "USER.md.migrated.bak").exists()
        assert not (temp_agent_dir / "USER.md").exists()
        assert migrate_legacy_tiers(temp_agent_dir) is False

    def test_skips_placeholder_sections(self, temp_agent_dir):
        (temp_agent_dir / "USER.md").write_text(
            "# USER\n\n## Communication Style\n- (auto-detected)\n", encoding="utf-8"
        )
        migrate_legacy_tiers(temp_agent_dir)
        agent_md = temp_agent_dir / "agent.md"
        # Nothing real to migrate → agent.md not created from placeholders.
        assert not agent_md.exists() or "(auto-detected)" not in agent_md.read_text("utf-8")

    def test_no_legacy_files_is_noop(self, temp_agent_dir):
        assert migrate_legacy_tiers(temp_agent_dir) is False

    def test_scrubs_stale_index_refs_self_healing(self, temp_agent_dir):
        # USER.md/MEMORY.md already migrated away (only .bak left), but a
        # pre-rename INDEX.md still points at them — migrate must self-heal it.
        memories = temp_agent_dir / "memories"
        memories.mkdir()
        (memories / "INDEX.md").write_text(
            "# Memory Index\n\n## Top-Level Files\n"
            "- [agent.md](../agent.md) — prefs\n"
            "- [USER.md](../USER.md) — user model\n"
            "- [MEMORY.md](../MEMORY.md) — lessons\n\n"
            "## Topic Files\n- [lessons](lessons.md)\n",
            encoding="utf-8",
        )
        assert migrate_legacy_tiers(temp_agent_dir) is True
        index = (memories / "INDEX.md").read_text(encoding="utf-8")
        assert "USER.md" not in index
        assert "MEMORY.md" not in index
        assert "[agent.md](../agent.md)" in index  # real ref preserved
        assert "[lessons](lessons.md)" in index
        # Idempotent: a second run finds nothing to scrub.
        assert migrate_legacy_tiers(temp_agent_dir) is False


# --- good habits (<habit> parsing + HABITS.md writer) ----------------------


def test_parse_extracts_habit_block():
    from novacode_cli.hermes.memory_tiers import parse_review_response

    content = "<habit>\n- Test-first for races: write the failing test first.\n</habit>"
    parsed = parse_review_response(content)
    assert "Test-first for races" in parsed["habits"]
    assert parsed["lessons"] == []  # habit-only must NOT be misfiled as a lesson
    assert parsed["user_model"] == ""


def test_parse_no_habit_block_is_empty():
    from novacode_cli.hermes.memory_tiers import parse_review_response

    parsed = parse_review_response("<lesson topic='t'>\n- a fact\n</lesson>")
    assert parsed["habits"] == ""
    assert len(parsed["lessons"]) == 1


def test_record_habit_creates_and_appends(tmp_path):  # noqa: ANN001
    from novacode_cli.hermes.memory_tiers import record_habit

    record_habit(tmp_path, "- Flatten nesting with guard clauses.")
    habits = (tmp_path / "HABITS.md").read_text(encoding="utf-8")
    assert "Good Habits" in habits  # header
    assert "Flatten nesting with guard clauses" in habits


def test_record_habit_dedups(tmp_path):  # noqa: ANN001
    from novacode_cli.hermes.memory_tiers import record_habit

    record_habit(tmp_path, "- Extract magic numbers to named constants.")
    record_habit(tmp_path, "- Extract magic numbers to named constants.")
    habits = (tmp_path / "HABITS.md").read_text(encoding="utf-8")
    assert habits.count("Extract magic numbers to named constants") == 1


def test_record_habit_empty_is_noop(tmp_path):  # noqa: ANN001
    from novacode_cli.hermes.memory_tiers import record_habit

    record_habit(tmp_path, "   ")
    assert not (tmp_path / "HABITS.md").exists()


class TestRunawayMemoryGuards:
    """One bad review must not flood the topic corpus.

    On 2026-09-23 a review emitted a word list; the writer created 16,721 topic
    files (one per "lesson", each a single word) in 12 seconds. Every later turn
    then paid ~7s stat-ing and scoring 18k files, and INDEX.md grew to 1.1MB —
    of which the prompt shows only the first 12k.
    """

    def test_a_flood_of_lessons_is_capped(self, temp_agent_dir: Path) -> None:
        from novacode_cli.hermes.memory_tiers import (
            MAX_LESSONS_PER_REVIEW,
            update_from_review,
        )

        lessons = [
            {"topic": f"nova-readme-audit-{w}", "bullets": f"- The {w} check needs a follow-up run"}
            for w in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta")
        ]
        update_from_review(temp_agent_dir, "", lessons)

        written = list((temp_agent_dir / "memories").glob("*.md"))
        topics = [p for p in written if p.name != "INDEX.md"]
        assert len(topics) == MAX_LESSONS_PER_REVIEW, [p.name for p in topics]

    def test_one_word_bullets_are_not_memories(self, temp_agent_dir: Path) -> None:
        """The exact shape of the runaway: a topic whose whole content is '- **Abhorring.**'."""
        record_lesson(temp_agent_dir, "nova-readme-audit-abhorring", "- **Abhorring.**")
        memories = temp_agent_dir / "memories"
        assert not list(memories.glob("nova-readme-audit-*.md")), "no file for a one-word lesson"
        index = memories / "INDEX.md"
        assert not index.exists() or "abhorring" not in index.read_text(encoding="utf-8")

    def test_a_real_lesson_still_lands(self, temp_agent_dir: Path) -> None:
        record_lesson(
            temp_agent_dir,
            "telegram-routing",
            "- Topic ids map to sessions; replying to a message reaches that session\n"
            "- **Ok.**",
        )
        body = (temp_agent_dir / "memories" / "telegram-routing.md").read_text(encoding="utf-8")
        assert "Topic ids map to sessions" in body
        assert "Ok." not in body, "the thin bullet is dropped, the real one kept"
