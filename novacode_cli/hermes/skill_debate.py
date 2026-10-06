"""Adversarial debate — a critic pass before a skill is frozen.

A drafted skill (from the evolution engine) gets one out-of-band critic round
before it's written to disk: is it specific, correct, genuinely reusable, and not
a duplicate? The critic either **approves** the draft, returns a **revised**
version, or **rejects** it outright. Bounded to one round and degrades gracefully
— any failure approves the original draft so creation is never blocked.

Disable with ``NOVA_SKILL_DEBATE=0``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger("nova.hermes.skill_debate")

_VERDICT_RE = re.compile(
    r"<verdict>\s*(approve|revise|reject)\s*</verdict>", re.IGNORECASE
)


def _debate_enabled() -> bool:
    """Whether adversarial debate is enabled (default on; ``NOVA_SKILL_DEBATE=0`` off)."""
    val = os.environ.get("NOVA_SKILL_DEBATE", "").strip().lower()
    return val not in ("0", "false", "no", "off")


def _emit(verdict: str, name: str) -> None:
    """Surface the debate outcome via the shared Nova event buffer (TUI-safe)."""
    messages = {
        "approved": f"◇ Skill '{name}' debated → approved",
        "revised": f"◇ Skill '{name}' debated → revised",
        "rejected": f"◇ Skill '{name}' debated → rejected (not saved)",
    }
    message = messages.get(verdict)
    if not message:
        return
    try:
        from novacode_cli.events import nova_event_log

        nova_event_log.append(("nova_skill_debate", "◇", "cyan", message))
    except Exception:  # noqa: BLE001
        logger.debug("debate event not surfaced: %s %s", verdict, name)


async def debate_skill_spec(
    spec: dict[str, str] | None,
    *,
    skill_library: list[dict[str, str]] | None = None,
    model: BaseChatModel | None = None,
) -> tuple[dict[str, str] | None, str]:
    """Run one critic round over a drafted skill spec.

    Args:
        spec: ``{"name","description","body"}`` from ``parse_skill_spec``.
        skill_library: ``[{"name","description"}]`` to detect duplicates.
        model: Override model (tests inject a fake); else ``create_model()``.

    Returns:
        ``(spec_or_None, verdict)`` with verdict ∈
        {"approved","revised","rejected","skipped","error"}. ``None`` spec means
        the critic rejected the skill — skip creation. Never raises.
    """
    name = (spec or {}).get("name", "")
    if not spec or not _debate_enabled():
        return spec, "skipped"

    try:
        from langchain_core.messages import SystemMessage

        from novacode_cli.hermes.skill_discovery import parse_skill_spec
        from novacode_cli.prompts import render_template
        from novacode_cli.skills.schema import (
            normalize_skill_frontmatter,
            render_schema_block,
            validate_skill_structure,
        )

        draft = normalize_skill_frontmatter(
            spec.get("body", ""), name, spec.get("description", "")
        )
        prompt = render_template(
            "nova_skill_debate.jinja",
            draft=draft,
            skill_library=skill_library or [],
            issues=validate_skill_structure(draft),
            schema_block=render_schema_block(),
        )

        if model is None:
            from novacode_cli.config.model_create import create_model

            model = create_model()

        resp = await model.ainvoke(
            [SystemMessage(content=prompt)],
            config={
                "run_name": "nova_skill_debate",
                "tags": ["nova", "hermes", "skill-debate"],
                # Out-of-band: keep the critic's output out of the chat stream.
                "metadata": {"nova_oob": True, "skill": name},
            },
        )
        raw = getattr(resp, "content", "")
        text = raw if isinstance(raw, str) else str(raw)

        match = _VERDICT_RE.search(text)
        verdict = match.group(1).lower() if match else "approve"

        if verdict == "reject":
            _emit("rejected", name)
            return None, "rejected"
        if verdict == "revise":
            revised = parse_skill_spec(text)
            if revised:
                _emit("revised", revised.get("name", name))
                return revised, "revised"
            # Said "revise" but emitted no parseable skill — keep the original.
            _emit("approved", name)
            return spec, "approved"
        _emit("approved", name)
        return spec, "approved"  # noqa: TRY300
    except Exception:  # noqa: BLE001
        logger.debug("Skill debate failed for '%s'; approving draft", name, exc_info=True)
        return spec, "error"


__all__ = ["debate_skill_spec"]
