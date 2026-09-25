"""Semantic-tier writers — the consolidation engines' output surface.

This module writes Nova's **Semantic memory tier**: durable facts distilled by
consolidation (the review engine and ``/dream``). Crucially, it writes the
surface ``AgentMemoryMiddleware`` actually *injects*, so learned facts re-enter
the agent's context on the next turn:

    ~/.nova/{assistant_id}/
    ├── agent.md            # user model + preferences (always injected)
    └── memories/
        ├── INDEX.md        # topic pointer list (always injected)
        └── <topic>.md      # topic facts/lessons (read on demand via INDEX)

User-model updates land in ``agent.md`` (see :func:`update_user_model`);
cross-session lessons land in topic files with an ``INDEX.md`` pointer (see
:func:`record_lesson`).

Legacy ``USER.md`` / ``MEMORY.md`` (a previous tier generation that was never
read back) are retained here as **migration readers only** — see
:func:`migrate_legacy_tiers`.

Files are compacted when they exceed ``MAX_MEMORY_CHARS`` (shared with the
read side via ``novacode_cli/memory/limits.py``), keeping the newest content.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novacode_cli.memory.limits import MAX_MEMORY_CHARS

logger = logging.getLogger("nova.hermes.memory_tiers")


def _emit_memory_event(message: str, icon: str = "📝") -> None:
    """Surface a memory-tier note without printing to the console.

    These writes run inside Hermes's *out-of-band* review, so a direct
    ``console.print`` here bypasses the UI event stream and corrupts the Textual
    TUI (it overlaps the input box). Instead we append to the shared Nova event
    buffer, which ``iterate_agent_events`` drains into a ``ContextMessage`` that
    both the Rich console and the TUI render. Best-effort — a memory write must
    never fail because its notice couldn't be shown.
    """
    try:
        from novacode_cli.events import nova_event_log

        nova_event_log.append(("nova_memory", icon, "dim", message))
    except Exception:  # noqa: BLE001
        logger.debug("memory event (not surfaced): %s", message)


_MEMORY_TRUNCATION_NOTICE = "\n\n... [older history truncated — only recent entries shown]"

# Default H1 header for a freshly-created topic lesson file.
_DEFAULT_TOPIC_HEADER = "# {title}\n\nLessons captured during reviews / dreams.\n"
# H1 header for a freshly-created HABITS.md (the always-injected good-habits file).
_HABITS_HEADER = "# Good Habits\n\nReusable practices that worked well, captured during reviews.\n"
# A safe default topic for unstructured or untagged lessons.
_DEFAULT_TOPIC = "lessons"

#: Topic files one review may write. A single degenerate review (the model
#: emitted a word list) once created 16,721 one-word topic files in 12s, which
#: grew the corpus to 18k files: every turn then spent ~7s stat-ing and scoring
#: them, and INDEX.md (1.1MB) was truncated to its first 1% in the prompt.
MAX_LESSONS_PER_REVIEW = 5

#: A lesson bullet must say something: at least this many words. "Abhorring."
#: is not a lesson. Deliberately minimal — the runaway wrote one-word bullets,
#: and a real lesson is never one word.
MIN_BULLET_WORDS = 2


# ── Public API ─────────────────────────────────────────────────────────────


def ensure_memory_tiers(agent_dir: Path) -> None:
    """Ensure the semantic-tier scaffolding (the ``memories/`` directory) exists.

    The always-injected ``agent.md`` is created by the agent bootstrap; topic
    files and ``INDEX.md`` are created on demand by :func:`record_lesson`. This
    just guarantees the directory is present.

    Args:
        agent_dir: Path to the agent directory (``~/.nova/{assistant_id}/``).
    """
    (agent_dir / "memories").mkdir(parents=True, exist_ok=True)


def compact_memory_file(path: Path, max_chars: int = MAX_MEMORY_CHARS) -> bool:
    """Compact a memory file by keeping the newest entries.

    Memory files are written newest-first (new sections/lessons are prepended
    below the ``# Title`` header), so the **head** of the file is the most
    recent content. When the file exceeds the limit we keep the head and drop
    the older tail — matching the injection-side truncation in
    ``agent_memory.py`` (see ``novacode_cli/memory/limits.py`` for the invariant).

    Args:
        path: Path to the memory file to compact.
        max_chars: Maximum allowed characters (default: 12_000).

    Returns:
        True if compaction was performed, False otherwise.
    """
    if not path.exists():
        return False

    content = path.read_text(encoding="utf-8")
    if len(content) <= max_chars:
        return False

    # Keep the newest half (the head).
    end_pos = min(len(content), max_chars // 2)

    # Cut at a section boundary so we don't slice mid-section; else fall back to
    # the nearest preceding newline.
    header_pos = content.find("\n## ", end_pos)
    if header_pos != -1 and header_pos < end_pos + 500:
        end_pos = header_pos
    else:
        newline_pos = content.rfind("\n", max(0, end_pos - 200), end_pos)
        if newline_pos != -1:
            end_pos = newline_pos

    truncated = content[:end_pos].rstrip() + "\n" + _MEMORY_TRUNCATION_NOTICE + "\n"

    # Guard: don't write if compaction would grow the file.
    if len(truncated) >= len(content):
        return False
    path.write_text(truncated, encoding="utf-8")

    _emit_memory_event(
        f"Compacted {path.name} ({len(content)} → {len(truncated)} chars)",
        icon="📦",
    )
    return True


def _merge_section(content: str, new_section: str) -> str:
    """Replace an existing ``## Section`` (matched by header) or append it.

    ``new_section`` includes its own ``## Header`` line; if a section with the
    same header already exists, its header + body are replaced wholesale,
    otherwise the section is appended at the end.
    """
    section_header = new_section.split("\n", 1)[0].strip()
    lines = content.split("\n")
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        if line.startswith("## ") and line.strip() == section_header:
            out.extend(new_section.rstrip().split("\n"))
            i += 1
            # Skip the old body up to the next section / H1.
            while i < len(lines) and not (
                lines[i].startswith("## ")
                or (lines[i].startswith("# ") and not lines[i].startswith("##"))
            ):
                i += 1
            replaced = True
        else:
            out.append(line)
            i += 1
    if not replaced:
        return content.rstrip() + "\n\n" + new_section.strip() + "\n"
    return "\n".join(out)


def update_user_model(agent_dir: Path, new_content: str) -> None:
    """Merge user-model section(s) into ``agent.md`` (the always-injected surface).

    ``new_content`` is one or more ``## Section`` blocks (as emitted by the
    review's ``<user_model>`` block). Each section replaces an existing one with
    the same header, or is appended. Plain bullets (no header) go under ``## Notes``.

    Args:
        agent_dir: Path to the agent directory.
        new_content: User-model content (``## Section`` blocks or bullets).
    """
    new_content = (new_content or "").strip()
    if not new_content:
        return
    agent_md = agent_dir / "agent.md"
    content = agent_md.read_text(encoding="utf-8") if agent_md.exists() else "# Agent Memory\n"

    if new_content.startswith("## "):
        for sec in re.split(r"(?=^## )", new_content, flags=re.MULTILINE):
            sec = sec.strip()
            if sec:
                content = _merge_section(content, sec)
    else:
        content = _merge_section(content, "## Notes\n" + new_content)

    agent_md.parent.mkdir(parents=True, exist_ok=True)
    agent_md.write_text(content.rstrip() + "\n", encoding="utf-8")
    _emit_memory_event("Updated user model in agent.md")
    compact_memory_file(agent_md)


def _slugify_topic(raw: str) -> str:
    """Normalize a topic name into a safe kebab-case filename stem."""
    slug = (raw or "").strip().lower()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:50]


#: Lines of prose before the pointer list in ``INDEX.md`` (the header this
#: module writes, plus any the user has added under it).
_INDEX_HEADER_RE = re.compile(r"\A(.*?)(?=^- \[|\Z)", re.DOTALL | re.MULTILINE)


def _upsert_index_pointer(memories_dir: Path, topic_slug: str) -> None:
    """Ensure ``memories/INDEX.md`` has a pointer to ``<topic_slug>.md``.

    PREPENDED, not appended. The index is injected into every prompt truncated
    to the per-block budget (``memory/limits.py``), and truncation keeps the
    head — so the head has to be the newest topics. Appending meant a long
    index showed the model only its oldest pointers.
    """
    index = memories_dir / "INDEX.md"
    line = f"- [{topic_slug}]({topic_slug}.md)"
    if index.exists():
        content = index.read_text(encoding="utf-8")
        if re.search(rf"\]\(\s*{re.escape(topic_slug)}\.md\s*\)", content):
            return  # pointer already present
        header = _INDEX_HEADER_RE.match(content)
        cut = header.end() if header else 0
        content = content[:cut].rstrip() + "\n\n" + line + "\n" + content[cut:].lstrip("\n")
    else:
        content = (
            "# Memory Index\n\n"
            "Topic files capturing facts and lessons learned across sessions.\n\n" + line + "\n"
        )
    index.write_text(content, encoding="utf-8")
    _emit_memory_event(f"Indexed memory topic: {topic_slug}")


def _worthwhile_bullets(bullets: str) -> str:
    """Drop bullets too thin to be a lesson; "" when none survive.

    The write side is the only place that can stop junk from reaching every
    later prompt: a one-word bullet carries nothing but still costs a file, an
    INDEX pointer, and a slot in the per-turn scoring corpus.
    """
    kept = []
    for line in (bullets or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith(("-", "*", "•")):
            kept.append(line)  # prose/sub-lines belong to the bullet above
            continue
        words = re.sub(r"[*_`\[\]()#.]+", " ", stripped.lstrip("-*• ")).split()
        if len(words) >= MIN_BULLET_WORDS:
            kept.append(line)
    return "\n".join(kept).strip()


def record_lesson(agent_dir: Path, topic: str, bullets: str) -> None:
    """Record cross-session lesson bullets to ``memories/<topic>.md`` + INDEX.

    Lessons are prepended (newest-first) under a timestamped section, deduped
    against what the topic file already holds, and the topic is registered in
    ``memories/INDEX.md`` (which ``AgentMemoryMiddleware`` injects every turn).

    Args:
        agent_dir: Path to the agent directory.
        topic: Topic name (slugified into the filename).
        bullets: Bullet lines to record under this topic.
    """
    bullets = _worthwhile_bullets(bullets)
    if not bullets:
        return
    topic_slug = _slugify_topic(topic) or _DEFAULT_TOPIC
    memories_dir = agent_dir / "memories"
    memories_dir.mkdir(parents=True, exist_ok=True)
    topic_file = memories_dir / f"{topic_slug}.md"

    if topic_file.exists():
        content = topic_file.read_text(encoding="utf-8")
    else:
        content = _DEFAULT_TOPIC_HEADER.format(title=topic_slug.replace("-", " ").title())

    deduped = _dedup_against(content, bullets)
    if not deduped:
        _emit_memory_event(f"Lesson added no new memory to '{topic_slug}' (all duplicates)")
        return

    # Prepend (newest-first) after the H1 header / intro, before the first section.
    insert_at = content.find("\n## ")
    if insert_at == -1:
        insert_at = len(content)
    before, after = content[:insert_at], content[insert_at:]
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"\n## Review — {timestamp}\n\n{deduped}\n"
    topic_file.write_text(before.rstrip() + "\n" + entry + after, encoding="utf-8")

    _upsert_index_pointer(memories_dir, topic_slug)
    _emit_memory_event(f"Recorded lesson to memories/{topic_slug}.md")
    compact_memory_file(topic_file)


def record_habit(agent_dir: Path, bullets: str) -> None:
    """Record good-habit bullets to ``HABITS.md`` (the always-injected file).

    Mirrors :func:`record_lesson`: bullets are prepended (newest-first) under a
    timestamped section, deduped against what the file already holds, and the
    file is compacted when it exceeds ``MAX_MEMORY_CHARS``. Best-effort.

    Args:
        agent_dir: The agent directory (``~/.nova/agents/<id>/``).
        bullets: Bullet lines describing reusable good habits.
    """
    if not (bullets or "").strip():
        return
    agent_dir.mkdir(parents=True, exist_ok=True)
    habits_file = agent_dir / "HABITS.md"

    content = habits_file.read_text(encoding="utf-8") if habits_file.exists() else _HABITS_HEADER

    deduped = _dedup_against(content, bullets)
    if not deduped:
        _emit_memory_event("Habit added no new memory (all duplicates)")
        return

    insert_at = content.find("\n## ")
    if insert_at == -1:
        insert_at = len(content)
    before, after = content[:insert_at], content[insert_at:]
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"\n## Review — {timestamp}\n\n{deduped}\n"
    habits_file.write_text(before.rstrip() + "\n" + entry + after, encoding="utf-8")

    _emit_memory_event("Recorded good habit to HABITS.md", icon="✨")
    compact_memory_file(habits_file)


_LESSON_BLOCK_RE = re.compile(
    r'<lesson(?:\s+topic\s*=\s*["\']?([^"\'>]*)["\']?)?\s*>(.*?)</lesson>',
    re.DOTALL | re.IGNORECASE,
)


def parse_review_response(response_content: str) -> dict[str, Any]:
    """Extract structured data from an LLM review response (new contract).

    Expected format (XML tags)::

        <user_model>
        ## Communication Style
        - Prefers concise bullet points
        </user_model>

        <lesson topic="testing">
        - Tests are run with `pytest -x` for fast feedback
        </lesson>

    ``<user_model>`` → ``agent.md`` (the injected user surface); each
    ``<lesson topic="…">`` → ``memories/<topic>.md`` (+ INDEX pointer).

    Args:
        response_content: The raw response content from the LLM.

    Returns:
        ``{"user_model": str, "lessons": [{"topic": str, "bullets": str}, ...]}``.
    """
    result: dict[str, Any] = {"user_model": "", "lessons": [], "habits": ""}

    if not response_content:
        return result

    user_matches = re.findall(
        r"<user_model>(.*?)</user_model>",
        response_content,
        re.DOTALL | re.IGNORECASE,
    )
    if user_matches:
        result["user_model"] = "\n".join(m.strip() for m in user_matches)

    for match in _LESSON_BLOCK_RE.finditer(response_content):
        topic = (match.group(1) or _DEFAULT_TOPIC).strip() or _DEFAULT_TOPIC
        bullets = match.group(2).strip()
        if bullets:
            result["lessons"].append({"topic": topic, "bullets": bullets})

    habit_matches = re.findall(
        r"<habit>(.*?)</habit>",
        response_content,
        re.DOTALL | re.IGNORECASE,
    )
    if habit_matches:
        result["habits"] = "\n".join(m.strip() for m in habit_matches if m.strip())

    # NOTE: unstructured responses (no <user_model>/<lesson>/<habit> blocks) are
    # intentionally dropped — they are usually raw thinking/prose, not distilled
    # lessons. Dumping them verbatim was the source of thinking-trace pollution
    # in lessons.md. The review prompt already asks for <lesson> blocks; if the
    # model emits prose instead, recording nothing is correct.

    return result


def _normalize_bullet(line: str) -> str:
    """Canonicalize a bullet line for duplicate comparison."""
    return re.sub(r"\s+", " ", line.lstrip("-*• ").strip().lower())


def _dedup_against(existing: str, block: str) -> str:
    """Drop bullet lines from ``block`` already present in ``existing``.

    Compares normalized bullet text so trivial whitespace/case differences
    don't slip a near-duplicate into a topic file. Non-bullet lines (headers,
    prose) are always kept.
    """
    known = {
        _normalize_bullet(ln)
        for ln in existing.splitlines()
        if ln.lstrip().startswith(("-", "*", "•"))
    }
    kept: list[str] = []
    for line in block.splitlines():
        if line.lstrip().startswith(("-", "*", "•")):
            norm = _normalize_bullet(line)
            if norm and norm in known:
                continue
            known.add(norm)
        kept.append(line)
    return "\n".join(kept).strip()


def update_from_review(
    agent_dir: Path,
    user_model: str,
    lessons: list[dict[str, str]],
) -> None:
    """Apply review learnings to the injected semantic surface.

    User-model updates land in ``agent.md``; each lesson lands in its
    ``memories/<topic>.md`` topic file (+ INDEX pointer).

    Args:
        agent_dir: Path to the agent directory.
        user_model: ``## Section`` block(s) for ``agent.md`` (may be empty).
        lessons: ``[{"topic", "bullets"}, ...]`` from the review.
    """
    if user_model:
        update_user_model(agent_dir, user_model)

    written = 0
    for lesson in lessons or []:
        if written >= MAX_LESSONS_PER_REVIEW:
            logger.warning(
                "Review produced %d lessons; keeping the first %d (see "
                "MAX_LESSONS_PER_REVIEW)",
                len(lessons),
                MAX_LESSONS_PER_REVIEW,
            )
            break
        topic = (lesson.get("topic") or _DEFAULT_TOPIC).strip() or _DEFAULT_TOPIC
        bullets = lesson.get("bullets") or ""
        if bullets.strip():
            record_lesson(agent_dir, topic, bullets)
            written += 1


# ── Legacy migration ───────────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\(auto-detected\)|\(captured during reviews\)", re.IGNORECASE)


def _section_has_real_content(section: str) -> bool:
    """True if a ``## Section`` block has a non-placeholder bullet."""
    return any(
        ln.lstrip().startswith(("-", "*", "•")) and not _PLACEHOLDER_RE.search(ln)
        for ln in section.splitlines()
    )


def migrate_legacy_tiers(agent_dir: Path) -> bool:
    """One-time merge of legacy ``USER.md`` / ``MEMORY.md`` into the injected surface.

    ``USER.md`` non-placeholder sections → ``agent.md``; ``MEMORY.md`` bullets →
    ``memories/lessons.md`` (+ INDEX). Migrated files are renamed to
    ``*.migrated.bak`` so a second run is a filesystem-level no-op too. The
    caller should additionally guard with a durable-store flag.

    Returns:
        True if any content was migrated.
    """
    migrated = False

    user_md = agent_dir / "USER.md"
    if user_md.exists():
        try:
            content = user_md.read_text(encoding="utf-8")
            for sec in re.split(r"(?=^## )", content, flags=re.MULTILINE):
                sec = sec.strip()
                if sec.startswith("## ") and _section_has_real_content(sec):
                    update_user_model(agent_dir, sec)
                    migrated = True
            user_md.rename(user_md.with_name("USER.md.migrated.bak"))
        except OSError:
            logger.exception("Failed migrating USER.md")

    memory_md = agent_dir / "MEMORY.md"
    if memory_md.exists():
        try:
            content = memory_md.read_text(encoding="utf-8")
            bullets = "\n".join(
                ln
                for ln in content.splitlines()
                if ln.lstrip().startswith(("-", "*", "•")) and not _PLACEHOLDER_RE.search(ln)
            )
            if bullets.strip():
                record_lesson(agent_dir, _DEFAULT_TOPIC, bullets)
                migrated = True
            memory_md.rename(memory_md.with_name("MEMORY.md.migrated.bak"))
        except OSError:
            logger.exception("Failed migrating MEMORY.md")

    # Scrub stale INDEX.md pointers to the legacy files. Runs unconditionally
    # (not gated on USER.md/MEMORY.md still existing) so an index written before
    # the rename self-heals on a later launch — their content now lives in
    # agent.md + memories/lessons.md, which the index already lists.
    if _scrub_legacy_index_refs(agent_dir):
        migrated = True

    if migrated:
        _emit_memory_event("Migrated legacy USER.md/MEMORY.md → agent.md + memories/")
    return migrated


# Matches a markdown list line whose link target is the legacy USER.md / MEMORY.md
# (e.g. ``- [USER.md](../USER.md) — …``), regardless of the relative path prefix.
_LEGACY_INDEX_REF_RE = re.compile(
    r"^.*\]\([^)]*(?:USER|MEMORY)\.md\).*$",
    re.IGNORECASE | re.MULTILINE,
)


def _scrub_legacy_index_refs(agent_dir: Path) -> bool:
    """Remove dead ``USER.md`` / ``MEMORY.md`` pointers from ``memories/INDEX.md``.

    Returns True if the index was changed.
    """
    index = agent_dir / "memories" / "INDEX.md"
    if not index.exists():
        return False
    try:
        content = index.read_text(encoding="utf-8")
    except OSError:
        return False
    scrubbed = _LEGACY_INDEX_REF_RE.sub("", content)
    if scrubbed == content:
        return False
    # Collapse the blank lines left where the pointer lines were removed.
    scrubbed = re.sub(r"\n{3,}", "\n\n", scrubbed)
    try:
        index.write_text(scrubbed, encoding="utf-8")
    except OSError:
        return False
    _emit_memory_event("Scrubbed legacy USER.md/MEMORY.md refs from INDEX.md")
    return True
