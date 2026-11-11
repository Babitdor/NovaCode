"""Bind memory wiki maintenance to one agent's own persistent directory."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote

from langchain.tools import tool

from novacode_cli.hermes.ledger import LearningLedger
from novacode_cli.hermes.memory_tiers import _worthwhile_bullets, lesson_proposal_target
from novacode_cli.memory.wiki import maintain_wiki, validate_wiki

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool


_PROJECT_PAGE_PARTS = 3
_SHARED_PAGE_PARTS = 1
_MINIMUM_MEANINGFUL_BULLET_WORDS = 2


def _validate_lesson_evidence(
    agent_dir: Path, evidence: list[str], scope: str, project_key: str
) -> tuple[str, ...]:
    """Require citations to existing factual pages visible in this scope."""
    from novacode_cli.memory.wiki import _safe

    if not evidence:
        message = "Cite at least one source topic page as evidence."
        raise ValueError(message)
    normalized = []
    prefix = "/memories/memories/"
    for source in evidence:
        if not source.startswith(prefix):
            message = "Evidence must cite a topic page under /memories/memories/."
            raise ValueError(message)
        relative = PurePosixPath(unquote(source[len(prefix) :]))
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".md":
            message = "Evidence path is not a safe topic page."
            raise ValueError(message)
        if relative.name == "INDEX.md" or relative.name.startswith("wiki-"):
            message = "Generated wiki navigation is not factual evidence."
            raise ValueError(message)
        if relative.parts[:1] == ("projects",):
            if (
                scope != "project"
                or len(relative.parts) != _PROJECT_PAGE_PARTS
                or relative.parts[1] != project_key
            ):
                message = "Evidence is outside the proposed project scope."
                raise ValueError(message)
        elif len(relative.parts) != _SHARED_PAGE_PARTS:
            message = "Evidence path is not a topic page."
            raise ValueError(message)
        path = agent_dir / "memories" / Path(*relative.parts)
        _safe(agent_dir, path)
        if not path.is_file():
            message = f"Evidence page does not exist: {source}"
            raise ValueError(message)
        normalized.append(source)
    return tuple(normalized)


def create_memory_lesson_proposal_tool(agent_dir: Path, project_key: str) -> BaseTool:
    """Create an approval-gated lesson writer bound to this agent and project."""

    @tool
    def propose_memory_lesson(
        topic: str,
        content: str,
        evidence: list[str],
        scope: Literal["shared", "project"] = "project",
    ) -> str:
        """Stage a Markdown lesson section for review; this never edits a page.

        Include substantive Markdown bullets and cite the memory source paths
        that support them. Project lessons are limited to the active project.
        Every proposal requires explicit approval with /learning.
        """
        candidate = content.strip()
        bullet_lines = re.findall(r"(?m)^\s*[-*•]\s+.+$", candidate)
        weak_bullets = [
            line
            for line in bullet_lines
            if len(re.sub(r"[*_`\[\]()#.]+", " ", line.lstrip("-*• ")).split())
            < _MINIMUM_MEANINGFUL_BULLET_WORDS
        ]
        cleaned = _worthwhile_bullets(candidate)
        if not cleaned or not bullet_lines or weak_bullets:
            return json.dumps(
                {
                    "error": (
                        "Provide at least one substantive Markdown bullet; every bullet "
                        "must contain at least two meaningful words."
                    ),
                    "complete": False,
                }
            )
        if scope == "project" and not project_key:
            return json.dumps(
                {
                    "error": "No active project; use shared scope only for reusable facts.",
                    "complete": False,
                }
            )
        project = project_key if scope == "project" else None
        try:
            sources = _validate_lesson_evidence(agent_dir, evidence, scope, project_key)
            target, source_hash = lesson_proposal_target(agent_dir, topic, project=project)
            record = LearningLedger(agent_dir / "learning.sqlite3").propose(
                content=cleaned,
                project=project or "",
                scope=scope,
                kind="lesson",
                target=target,
                source_hash=source_hash,
                evidence=sources,
                origin="dream",
                task="memory wiki consolidation",
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return json.dumps({"error": str(exc), "complete": False})
        return json.dumps(
            {
                "reference": record.reference,
                "scope": record.scope,
                "target": record.target,
                "source_hash": record.source_hash,
                "status": record.status,
                "requires_approval": True,
            }
        )

    return propose_memory_lesson


def create_memory_wiki_tool(agent_dir: Path) -> BaseTool:
    """Create a tool with its destination bound by the application, not the model."""

    @tool
    def memory_wiki(action: Literal["check", "maintain"] = "check") -> str:
        """Check or rebuild your linked memory wiki after consolidating topic notes.

        Check reports broken relative links. Maintain backs up source files,
        rebuilds the compact INDEX and topic maps, and refreshes related links.
        It preserves topic bodies and user rules; it does not decide which
        conflicting claims are true or delete unique facts. Use after /dream.
        """
        try:
            result = (
                maintain_wiki(agent_dir)
                if action == "maintain"
                else {"broken_links": validate_wiki(agent_dir)}
            )
            return json.dumps(result)
        except (OSError, ValueError, RuntimeError) as exc:
            return json.dumps({"error": str(exc), "complete": False})

    return memory_wiki
