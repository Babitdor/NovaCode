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

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novacode_cli.memory.limits import MAX_MEMORY_CHARS

logger = logging.getLogger("nova.hermes.memory_tiers")


def _emit_memory_event(message: str, icon: str = "●") -> None:
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
        icon="▣",
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


# ── Lesson scope ───────────────────────────────────────────────────────────
# One assistant works in many codebases, but its memory was a single pool: a
# fact about project A ("`/app/run.py` implements run_tasks…") was offered in
# project B. Battle-testing the loop across unrelated Terminal-Bench tasks made
# it obvious — nearly every lesson was the answer to one task. Lessons now carry
# a scope: "general" ones go to the shared pool, "project" ones to a directory
# only that project reads.
PROJECTS_DIRNAME = "projects"


def project_memory_key(workspace_root: Path | str | None, sandbox_id: str | None = None) -> str:
    """A stable directory name for the codebase a session is working in.

    A remote sandbox is its own, throwaway project (two sandboxes that both
    mount ``/app`` have nothing in common), so it is keyed by the sandbox id.
    A local session is keyed by its workspace path: the folder name for
    readability plus a short hash so two ``backend/`` folders do not collide.
    """
    if sandbox_id:
        return f"sandbox-{_slugify_topic(str(sandbox_id)) or 'unknown'}"
    if not workspace_root:
        return ""
    resolved = str(Path(workspace_root).expanduser().resolve())
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:8]  # noqa: S324 — a name, not security
    return f"{_slugify_topic(Path(resolved).name)[:30] or 'project'}-{digest}"


def lesson_dir(agent_dir: Path, project: str | None = None) -> Path:
    """Where lessons of this scope live: the shared pool, or one project's own."""
    base = agent_dir / "memories"
    return base / PROJECTS_DIRNAME / project if project else base


def existing_topics(agent_dir: Path, project: str | None = None, limit: int = 40) -> list[str]:
    """Topic names already on file (general, plus this project's), newest first.

    Shown to the reviewer so it files a lesson under a topic that exists instead
    of coining a near-synonym for it.
    """
    dirs = [lesson_dir(agent_dir)] + ([lesson_dir(agent_dir, project)] if project else [])
    files = [p for d in dirs if d.is_dir() for p in d.glob("*.md") if p.name != "INDEX.md"]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return list(dict.fromkeys(p.stem for p in files))[:limit]


def _same_topic(memories_dir: Path, topic_slug: str) -> str:
    """Map a new topic name onto an existing one that means the same thing.

    Reviews name topics freely, and the same subject came back as
    ``polyglot-rust-cpp`` and ``rust-cpp-polyglot`` — two files, two index
    pointers, half the lessons in each. Two names made of the same words are the
    same topic; so are names that mostly overlap.
    """
    if (memories_dir / f"{topic_slug}.md").exists() or not memories_dir.is_dir():
        return topic_slug
    words = set(topic_slug.split("-"))
    best, best_score = topic_slug, 0.0
    for path in memories_dir.glob("*.md"):
        if path.name == "INDEX.md":
            continue
        other = set(path.stem.split("-"))
        score = len(words & other) / len(words | other)
        if score > best_score:
            best, best_score = path.stem, score
    return best if best_score >= _SAME_TOPIC_OVERLAP else topic_slug


# Share of words two topic names must have in common to be treated as one.
# Half is not enough — `build-and-test` would swallow `build-and-deploy` — so it
# takes a clear majority; a reordering of the same words always qualifies.
_SAME_TOPIC_OVERLAP = 0.6


def record_lesson(agent_dir: Path, topic: str, bullets: str, *, project: str | None = None) -> None:
    """Record cross-session lesson bullets to ``memories/<topic>.md`` + INDEX.

    Lessons are prepended (newest-first) under a timestamped section, deduped
    against what the topic file already holds, and the topic is registered in
    ``memories/INDEX.md`` (which ``AgentMemoryMiddleware`` injects every turn).

    Args:
        agent_dir: Path to the agent directory.
        topic: Topic name (slugified into the filename).
        bullets: Bullet lines to record under this topic.
        project: Project key (see :func:`project_memory_key`) for a lesson that
            only holds in that codebase; ``None`` for a general one.
    """
    bullets = _worthwhile_bullets(bullets)
    if not bullets:
        return
    memories_dir = lesson_dir(agent_dir, project)
    memories_dir.mkdir(parents=True, exist_ok=True)
    topic_slug = _same_topic(memories_dir, _slugify_topic(topic) or _DEFAULT_TOPIC)
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

    _emit_memory_event("Recorded good habit to HABITS.md", icon="✦")
    compact_memory_file(habits_file)


_LESSON_BLOCK_RE = re.compile(r"<lesson\b([^>]*)>(.*?)</lesson>", re.DOTALL | re.IGNORECASE)
_LESSON_ATTR_RE = re.compile(r'(\w+)\s*=\s*["\']?([^"\'\s>]*)["\']?')
# A pitfall is recorded in three fields so the symptom is always there to match
# against: a free-text lesson that gave only the fix could not be found by the
# session that next hit the same error.
_PITFALL_BLOCK_RE = re.compile(r"<pitfall\b([^>]*)>(.*?)</pitfall>", re.DOTALL | re.IGNORECASE)


def _pitfall_field(body: str, name: str) -> str:
    match = re.search(rf"<{name}>(.*?)</{name}>", body, re.DOTALL | re.IGNORECASE)
    return " ".join(match.group(1).split()) if match else ""


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
        attrs = {k.lower(): v for k, v in _LESSON_ATTR_RE.findall(match.group(1) or "")}
        topic = (attrs.get("topic") or _DEFAULT_TOPIC).strip() or _DEFAULT_TOPIC
        bullets = match.group(2).strip()
        if bullets:
            # "" when the model did not say; update_from_review decides then.
            scope = attrs.get("scope", "").strip().lower()
            result["lessons"].append({"topic": topic, "bullets": bullets, "scope": scope})

    for match in _PITFALL_BLOCK_RE.finditer(response_content):
        attrs = {k.lower(): v for k, v in _LESSON_ATTR_RE.findall(match.group(1) or "")}
        symptom, cause, fix = (_pitfall_field(match.group(2), f) for f in ("symptom", "cause", "fix"))
        if not symptom:
            continue  # without a symptom there is nothing to recall it by
        bullet = f"- **Symptom:** {symptom}"
        if cause:
            bullet += f" **Cause:** {cause}"
        if fix:
            bullet += f" **Fix:** {fix}"
        result["lessons"].append(
            {
                "topic": (attrs.get("topic") or "pitfalls").strip() or "pitfalls",
                "bullets": bullet,
                "scope": attrs.get("scope", "").strip().lower(),
            }
        )

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


# ── Same fact, different words ─────────────────────────────────────────────
# A review re-derives what earlier reviews already wrote down, in new words each
# time: one run filed "run `apt-get update` first" four times in one topic and
# "`all_gather` is not autograd-aware" four times in another. Exact-text dedup
# sees four different strings. Comparing the words that carry the meaning sees
# one fact.
_FACT_STOPWORDS = frozenset(
    "the and for with that this from into onto over then than when while until "
    "can will may must should would could has have had are was were been being "
    "its it's you your use used using via also just only very more most such "
    "one two any all each both own same other".split()
)
# Shared meaningful words needed, and the share of the SHORTER bullet they must
# cover. Measured against the shorter one because a paraphrase is often the same
# fact plus an explanation; the floor of 5 keeps two short bullets that merely
# name the same tool from being called the same fact.
_SAME_FACT_MIN_SHARED = 5
_SAME_FACT_OVERLAP = 0.55


def _fact_words(line: str) -> frozenset[str]:
    """The meaning-bearing words of a bullet, lightly stemmed."""
    words = set()
    for word in re.findall(r"[a-z0-9_]+", line.lower()):
        if len(word) < 3 or word in _FACT_STOPWORDS:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            # Leave at least three letters: "thing" must not become "th".
            if len(word) - len(suffix) >= 3 and word.endswith(suffix):
                word = word[: -len(suffix)]
                break
        words.add(word)
    return frozenset(words)


def _same_fact(
    a: frozenset[str], b: frozenset[str], overlap: float = _SAME_FACT_OVERLAP
) -> bool:
    shared = len(a & b)
    return shared >= _SAME_FACT_MIN_SHARED and shared / min(len(a), len(b)) >= overlap


def _bullets(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.lstrip().startswith(("-", "*", "•"))]


def _dedup_against(existing: str, block: str) -> str:
    """Drop bullet lines from ``block`` already present in ``existing``.

    A bullet is "already present" when its normalized text matches, or when it
    states the same fact in other words (see :func:`_same_fact`). Non-bullet
    lines (headers, prose) are always kept.
    """
    known = {_normalize_bullet(ln) for ln in _bullets(existing)}
    known_lines = list(_bullets(existing))
    kept: list[str] = []
    for line in block.splitlines():
        if line.lstrip().startswith(("-", "*", "•")):
            norm = _normalize_bullet(line)
            if norm and norm in known:
                continue
            if _says_the_same(line, known_lines):
                continue
            known.add(norm)
            known_lines.append(line)
        kept.append(line)
    return "\n".join(kept).strip()


def _is_variant(a: str, b: str) -> bool:
    """Two bullets that share a template but differ in the part that matters.

    An embedding averages over the words, so swapping one of them barely moves
    it: "tests run with `pytest -x`" against "`pytest -q`" scores 0.999, and
    "listens on port 8080" against "9090" scores 0.78. Those are different
    facts. When two bullets differ by only a word or two on each side, the
    difference IS the content, so they are never treated as one.
    """
    words_a = set(re.findall(r"[\w.+/:=-]+", a.lower()))
    words_b = set(re.findall(r"[\w.+/:=-]+", b.lower()))
    only_a, only_b = len(words_a - words_b), len(words_b - words_a)
    return 1 <= only_a <= _VARIANT_MAX_DIFF and 1 <= only_b <= _VARIANT_MAX_DIFF


# Words that may differ on each side before two look-alike bullets stop being
# "the same template with a different value" and become a real rewording.
_VARIANT_MAX_DIFF = 2


def _says_the_same(line: str, others: list[str], *, confirming: bool = False) -> bool:
    """Does ``line`` state the same fact as any of ``others``?

    Judged by meaning when the local embedding model is available (see
    :mod:`novacode_cli.memory.semantic`), otherwise by shared words.
    ``confirming`` selects the stricter word test used when one project's fact
    is vouching for another's; the embedding needs no second setting, because it
    already keeps the pair that test exists for well apart.
    """
    if not others:
        return False
    from novacode_cli.memory import semantic

    def plain(text: str) -> str:
        return text.strip().lstrip("-*• ").strip()

    scores = semantic.similarities(plain(line), [plain(o) for o in others])
    if scores:
        return any(
            score >= semantic.SAME_FACT and not _is_variant(line, other)
            for score, other in zip(scores, others, strict=True)
        )
    facts = _fact_words(line)
    overlap = _CONFIRMATION_OVERLAP if confirming else _SAME_FACT_OVERLAP
    return any(
        _same_fact(facts, _fact_words(o), overlap) and not _is_variant(line, o) for o in others
    )


# ── Earning the shared pool ────────────────────────────────────────────────
# The reviewer's own "this is general" cannot be trusted: asked to keep task
# answers private, it still marked `range-coder-encoding` — the internals of one
# task's decoder — and four single-task skills as general. Generality is a claim
# about OTHER projects, so it is settled by other projects: a lesson moves to the
# shared pool once a second, different project has independently recorded the
# same fact. Until then it stays with the project that learned it, where it is
# still available every time that project is worked on.
_PROMOTION_SCAN_PROJECTS = 60  # newest other projects checked for a matching fact
# Confirmation asks for more agreement than deduplication does. Dropping a
# paraphrase by mistake loses one bullet; sharing by mistake puts one project's
# detail in front of every other. At the dedup threshold a note about the MIPS
# cross-compiler package was "confirmed" because it ended with the same remark
# about `apt-get update` that other tasks had made.
_CONFIRMATION_OVERLAP = 0.7


_OUTCOME_FILE = "OUTCOME"  # in a project's lesson dir; no .md, so never read as a lesson
_SOURCES_FILE = ".shared-sources.json"  # which projects stand behind each shared bullet


def project_outcome(agent_dir: Path, project: str) -> str:
    """"passed", "failed", or "" when nobody outside the session has judged it."""
    try:
        return (lesson_dir(agent_dir, project) / _OUTCOME_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _corroborated(agent_dir: Path, project: str, line: str) -> str | None:
    """The other project that recorded this same fact, if there is one.

    A project whose work was judged a failure cannot confirm anything.
    """
    root = agent_dir / "memories" / PROJECTS_DIRNAME
    if not root.is_dir():
        return None
    others = [d for d in root.iterdir() if d.is_dir() and d.name != project]
    others.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for directory in others[:_PROMOTION_SCAN_PROJECTS]:
        if project_outcome(agent_dir, directory.name) == "failed":
            continue
        for path in directory.glob("*.md"):
            if path.name == "INDEX.md":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if _says_the_same(line, _bullets(text), confirming=True):
                return directory.name
    return None


def _read_sources(agent_dir: Path) -> dict[str, list[str]]:
    try:
        data = json.loads((lesson_dir(agent_dir) / _SOURCES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_sources(agent_dir: Path, sources: dict[str, list[str]]) -> None:
    directory = lesson_dir(agent_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / _SOURCES_FILE).write_text(json.dumps(sources, indent=1), encoding="utf-8")


def _promote_corroborated(agent_dir: Path, topic: str, bullets: str, project: str) -> int:
    """Copy to the shared pool the bullets a second project has confirmed."""
    # Facts already in the shared pool under ANY topic: two tasks confirmed the
    # same `apt-get update` lesson under two topic names and it was shared twice.
    shared = [
        ln
        for path in lesson_dir(agent_dir).glob("*.md")
        if path.name != "INDEX.md"
        for ln in _bullets(path.read_text(encoding="utf-8"))
    ]
    sources = _read_sources(agent_dir)
    confirmed = []
    for line in _bullets(_worthwhile_bullets(bullets)):
        if _says_the_same(line, shared):
            continue
        other = _corroborated(agent_dir, project, line)
        if other:
            confirmed.append(line)
            shared.append(line)
            sources[_normalize_bullet(line)] = sorted({project, other})
    if confirmed:
        record_lesson(agent_dir, topic, "\n".join(confirmed))  # general; dedups itself
        _write_sources(agent_dir, sources)
    return len(confirmed)


def _drop_shared_bullet(agent_dir: Path, normalized: str) -> int:
    """Remove one bullet from the shared pool, and its topic file if that empties it."""
    removed = 0
    directory = lesson_dir(agent_dir)
    for path in directory.glob("*.md"):
        if path.name == "INDEX.md":
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [
            ln
            for ln in lines
            if not (ln.lstrip().startswith(("-", "*", "•")) and _normalize_bullet(ln) == normalized)
        ]
        if len(kept) == len(lines):
            continue
        removed += len(lines) - len(kept)
        if _bullets("\n".join(kept)):
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            continue
        path.unlink()
        index = directory / "INDEX.md"
        if index.exists():
            pointer = f"]({path.stem}.md)"
            index.write_text(
                "\n".join(ln for ln in index.read_text(encoding="utf-8").splitlines() if pointer not in ln)
                + "\n",
                encoding="utf-8",
            )
    return removed


def mark_project_outcome(agent_dir: Path, project: str, passed: bool) -> int:
    """Record how a project's work was judged from OUTSIDE the session.

    A session only sees its own checks. A grader, a CI run or the user may know
    better: on Terminal-Bench one agent's own verification printed "ALL PASS"
    while the task's real tests failed four of thirteen. When the outside verdict
    is a failure, anything that project helped put in the shared pool is taken
    back out unless two other projects still stand behind it.

    Returns:
        The number of shared bullets retracted.
    """
    directory = lesson_dir(agent_dir, project)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / _OUTCOME_FILE).write_text("passed" if passed else "failed", encoding="utf-8")
    if passed:
        return 0
    sources = _read_sources(agent_dir)
    retracted = 0
    for normalized, backers in list(sources.items()):
        if project not in backers:
            continue
        standing = [b for b in backers if b != project and project_outcome(agent_dir, b) != "failed"]
        if len(standing) >= 2:  # noqa: PLR2004 — two projects is the bar for "general"
            sources[normalized] = standing
            continue
        retracted += _drop_shared_bullet(agent_dir, normalized)
        del sources[normalized]
    _write_sources(agent_dir, sources)
    return retracted


def update_from_review(
    agent_dir: Path,
    user_model: str,
    lessons: list[dict[str, str]],
    *,
    project: str | None = None,
    shareable: bool = True,
) -> None:
    """Apply review learnings to the injected semantic surface.

    User-model updates land in ``agent.md``; each lesson lands in its
    ``memories/<topic>.md`` topic file (+ INDEX pointer).

    Args:
        agent_dir: Path to the agent directory.
        user_model: ``## Section`` block(s) for ``agent.md`` (may be empty).
        lessons: ``[{"topic", "bullets", "scope"}, ...]`` from the review.
        project: Key of the codebase this session is in. Every lesson is
            recorded for this project. One the review marked
            ``scope="general"`` is additionally copied to the shared pool, but
            only once a different project has recorded the same fact — the
            review's label alone is not enough. With no project key everything
            is general, as before.
        shareable: False when the session's own last check failed. Lessons are
            still kept for the project, but none can reach the shared pool.
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
            # With a project, every lesson is first the project's own. One the
            # review calls general is then shared only if another project has
            # already recorded the same fact (see _promote_corroborated).
            record_lesson(agent_dir, topic, bullets, project=project or None)
            if project and shareable and lesson.get("scope") == "general":
                _promote_corroborated(agent_dir, topic, bullets, project)
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
