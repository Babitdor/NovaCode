"""Prompt templates for Nova CLI using Jinja2.

Besides plain rendering, this module implements the *render-time* half of prompt
hill-climbing (Loop-Engineering Enhancement 2): when the
:class:`~novacode_cli.hermes.prompt_evolution.PromptEvolutionEngine` has written
a **candidate** version of a template, a fraction of renders are routed to it so
the candidate and the current active version can be A/B compared.

Override files live entirely under ``~/.nova/prompt_history/<stem>/`` and are
loaded by a *separate* Jinja environment so they can never contaminate the
package templates:

- ``active.jinja``    — a promoted override (takes precedence over the package).
- ``candidate.jinja`` — an under-test candidate (used for a slice of renders).

If neither exists (the overwhelmingly common case) rendering is exactly the old
behaviour: the packaged template via :data:`_env`.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger("nova.prompts")

# Template directory (packaged templates).
TEMPLATES_DIR = Path(__file__).parent

# User-space override root for evolving templates (never the package dir).
PROMPT_HISTORY_DIR = Path.home() / ".nova" / "prompt_history"

# Jinja environment — FileSystemLoader auto-reloads templates when mtime
# changes (auto_reload=True by default), so no manual cache is needed.
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(default=False),
    trim_blocks=True,
    lstrip_blocks=True,
)

# A SEPARATE environment for override files. Keeping it distinct from ``_env``
# guarantees a candidate template can never shadow a packaged one by name.
_override_env = Environment(
    loader=FileSystemLoader(str(PROMPT_HISTORY_DIR)),
    autoescape=select_autoescape(default=False),
    trim_blocks=True,
    lstrip_blocks=True,
)

#: Probability a render uses the candidate when one exists.
_CANDIDATE_SHARE = 0.5

#: Per-process variant choice per template ("candidate" | "active"). Decided once
#: (first render after a candidate appears) so a whole session is internally
#: consistent, and readable later for quality attribution.
_AB_CHOICE: dict[str, str] = {}
# Not cryptographic — just a 50/50 routing coin for an A/B experiment.
_ab_rng = random.Random()  # noqa: S311


def _stem(name: str) -> str:
    """Template name without the ``.jinja`` suffix (the override subdir name)."""
    return name.removesuffix(".jinja")


def _choose_variant(name: str) -> str:
    """Return this process's A/B variant for ``name``, deciding once and caching."""
    choice = _AB_CHOICE.get(name)
    if choice is None:
        choice = "candidate" if _ab_rng.random() < _CANDIDATE_SHARE else "active"
        _AB_CHOICE[name] = choice
    return choice


def _is_fresh(override: Path, name: str) -> bool:
    """True unless the packaged template changed after ``override`` was written.

    An override is a full COPY of the template as it was when evolution ran, so
    once the packaged file is edited the override silently reverts that edit
    for every session it serves (a month-old candidate was undoing prompt
    fixes for half of all sessions). A deliberate edit to the package wins; the
    evolution loop can propose a new candidate against the new text.
    """
    try:
        package = Path(_env.loader.searchpath[0]) / name  # type: ignore[union-attr]
        package_mtime = package.stat().st_mtime
    except (AttributeError, IndexError, OSError):
        return True  # nothing to compare against: keep the override
    try:
        fresh = override.stat().st_mtime >= package_mtime
    except OSError:
        return False
    if not fresh:
        logger.warning("Ignoring stale prompt override %s (package template is newer)", override)
    return fresh


def current_variant(name: str) -> str | None:
    """Return the active A/B variant for ``name``, or ``None`` if no candidate.

    Used by the evolution engine to attribute a turn's quality to the variant
    that produced this session's prompts.
    """
    return None


def generated_override(path: Path) -> bool:
    """Recognize an unchanged engine-generated override without deleting it."""
    import json
    manifest = path.parent / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        version = data.get("active_version")
        original = path.parent / f"{version}.jinja"
        return bool(version and original.is_file() and original.read_bytes() == path.read_bytes())
    except (OSError, ValueError):
        return False


def reset_ab_choices() -> None:
    """Clear cached A/B choices (test isolation / after a promotion or rollback)."""
    _AB_CHOICE.clear()


def render_template(name: str, **kwargs: Any) -> str:
    """Render a Jinja template, honouring any active / candidate override.

    Args:
        name: Template filename (e.g., ``'core_agent_system.jinja'``).
        **kwargs: Template context variables.

    Returns:
        Rendered template string.
    """
    # Main-agent identity and execution rules are release-controlled. Global
    # prompt history and activated experiments may customize other templates,
    # but must never replace the base instructions shipped with Nova.
    if name in {
        "core_agent_system.jinja", "System_Prompt_Nova.jinja", "nova_standing_instructions.jinja",
        "coding_workflow.jinja", "file_paths.jinja",
    }:
        return _env.get_template(name).render(**kwargs)

    from novacode_cli.hermes.activation import active_artifacts
    for kind, target, body in active_artifacts.get():
        if kind == "prompt" and target == name:
            return _env.from_string(body).render(**kwargs)
    override_dir = PROMPT_HISTORY_DIR / _stem(name)
    # Fast path: no overrides for this template → packaged behaviour, one stat.
    if not override_dir.exists():
        return _env.get_template(name).render(**kwargs)

    stem = _stem(name)
    active = override_dir / "active.jinja"
    has_active = active.exists() and _is_fresh(active, name) and not generated_override(active)
    try:
        if has_active:
            return _override_env.get_template(f"{stem}/active.jinja").render(**kwargs)
    except Exception:
        # A broken override must never take down prompt assembly — fall back to
        # the packaged template.
        logger.exception("Override render failed for %s; using packaged template", name)
    return _env.get_template(name).render(**kwargs)
