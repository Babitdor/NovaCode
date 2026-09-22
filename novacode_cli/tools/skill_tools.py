"""Agent-facing skill management — the write path for autonomous self-improvement.

While ``NovaLearningMiddleware`` can create skills *after the fact* from an
out-of-band review (see ``novacode_cli/hermes/skill_discovery.py``), this module
gives the **agent itself** an in-loop write path: a single ``skill_manage``
meta-tool the agent invokes the moment it recognizes a reusable workflow — right
after solving a hard task, finding a non-obvious workaround, or completing a
multi-step procedure it expects to repeat.

The two paths are complementary: ``skill_manage`` is the primary creator (best
signal — the agent has full session context), and the review ``<skill>`` block
stays as a safety net for what the agent forgot to capture.

Sub-operations (``action``):
    create  — new skill authored by the skill-creation sub-agent (LLM)
    patch   — surgical, unique string replacement in an existing SKILL.md
    edit    — full rewrite of an existing skill's body (frontmatter preserved)
    delete  — remove a skill (user/project only; bundled skills are protected)

CRITICAL: this runs *inside the live agent loop / TUI*, so it must NEVER
``console.print`` — that corrupts the Textual UI. All user-facing notifications
go through ``nova_event_log`` via ``_emit_tui_event``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from langchain.tools import tool

logger = logging.getLogger("nova.tools.skill_manage")


# ── Helpers ────────────────────────────────────────────────────────────────


def _user_skills_dir() -> Path:
    """Return ~/.nova/skills/ (created if missing). Agent-authored skills land here."""
    from novacode_cli.config.config import Settings

    return Settings.from_environment().ensure_user_skills_dir()


def _resolve_existing_skill(name: str) -> tuple[Path, str] | None:
    """Locate an existing skill by name across writable + bundled sources.

    Returns ``(skill_dir, source)`` where source is ``"user"`` / ``"project"`` /
    ``"claude"``, or ``None`` if no skill with that name exists. Used by
    patch/edit/delete to find the target and (for delete) gate on provenance.
    """
    from novacode_cli.config.config import Settings

    settings = Settings.from_environment()

    user_dir = settings.ensure_user_skills_dir()
    if (user_dir / name / "SKILL.md").exists():
        return user_dir / name, "user"

    for proj_dir in settings.get_project_skills_dirs():
        if (proj_dir / name / "SKILL.md").exists():
            return proj_dir / name, "project"

    claude_dir = settings.get_global_claude_skills_dir()
    if (claude_dir / name / "SKILL.md").exists():
        return claude_dir / name, "claude"

    return None


def _split_frontmatter(content: str) -> tuple[str, str]:
    """Split a SKILL.md into ``(frontmatter_block, body)``.

    The frontmatter block includes both ``---`` delimiters and the trailing
    newline. Returns ``("", content)`` when no frontmatter is present.
    """
    if not content.startswith("---"):
        return "", content
    end = content.find("\n---", 3)
    if end == -1:
        return "", content
    # Advance past the closing delimiter line.
    nl = content.find("\n", end + 1)
    if nl == -1:
        return content, ""
    return content[: nl + 1], content[nl + 1 :].lstrip("\n")


# ── The tool ───────────────────────────────────────────────────────────────


@tool
async def skill_manage(
    action: str,
    name: str,
    description: str = "",
    body: str = "",
    old: str = "",
    new: str = "",
) -> str:
    """Create, refine, or remove a reusable skill (your self-improvement write path).

    Invoke this the moment you recognize a workflow worth keeping — typically after:
    - solving a task that took 5+ tool calls,
    - finding a non-obvious workaround or gotcha, or
    - completing a multi-step procedure you expect to repeat.

    A skill is a ``SKILL.md`` file (YAML frontmatter + markdown steps) the agent
    auto-loads on future similar tasks. Ground skills in what you actually did —
    real files, commands, conventions, and pitfalls — never invented steps.

    Actions:
      - ``create``: Author a new skill. Provide ``name`` (kebab-case) and a
        ``description`` that states *when to use it* (the trigger). The body is
        written for you by the skill-creation sub-agent from your description, so
        make the description rich and specific.
      - ``patch``: Surgically fix an existing skill. Provide ``name``, ``old``
        (exact text to find — must appear exactly once), and ``new``. Preferred
        for small updates; preserves the rest of the skill.
      - ``edit``: Replace an existing skill's entire body. Provide ``name`` and
        ``body`` (markdown, no frontmatter). Optionally ``description`` to update
        the trigger. Use only when a patch can't express the change.
      - ``delete``: Soft-delete a skill by ``name`` (moved to an archive, so it
        is recoverable). Only user/project skills; bundled skills are protected.
      - ``history``: List the saved versions of a skill (for rollback).
      - ``rollback``: Restore a skill's previous version. Provide ``name`` and
        optionally ``old`` as the version id (defaults to the most recent
        snapshot). Use this to instantly undo a buggy/ineffective change.

    Args:
        action: ``create`` | ``patch`` | ``edit`` | ``delete`` | ``history`` | ``rollback``.
        name: Skill name in kebab-case (e.g. ``add-tui-slash-command``).
        description: For ``create``/``edit`` — the "use when…" trigger.
        body: For ``edit`` — the new markdown body (no frontmatter).
        old: For ``patch`` — exact text to replace (must be unique in the file).
        new: For ``patch`` — replacement text.

    Returns:
        A short human-readable result describing what happened.
    """
    from novacode_cli.hermes.skill_discovery import (
        _emit_tui_event,
        _slugify_skill_name,
    )
    from novacode_cli.skills import versioning

    action = (action or "").strip().lower()
    name = _slugify_skill_name(name or "")
    if not name:
        return "skill_manage: a valid kebab-case 'name' is required."

    try:
        if action == "create":
            skills_dir = _user_skills_dir()
            if (skills_dir / name / "SKILL.md").exists():
                return f"skill_manage: skill '{name}' already exists — use action='patch' or 'edit' to update it."
            if not description:
                return "skill_manage: 'description' is required for create (it becomes the trigger and guides authoring)."

            from novacode_cli.skills.skill_creation import _generate_skill

            result = await _generate_skill(name, skills_dir, description)
            if result:
                # Baseline v1 snapshot so even the first version is rollback-able.
                versioning.snapshot(skills_dir / name, reason="create", source="agent")
                skill_md_path = skills_dir / name / "SKILL.md"
                _emit_tui_event(
                    "nova_skill_created",
                    f"Agent created skill: {name}\n   {description}\n   {skills_dir / name}",
                )
                return f"Created skill '{name}' at {skill_md_path}."
            _emit_tui_event(
                "nova_skill_error", f"Agent skill creation failed for '{name}'"
            )
            return f"skill_manage: failed to author skill '{name}' (the skill-creation sub-agent produced no SKILL.md)."

        if action == "patch":
            resolved = _resolve_existing_skill(name)
            if resolved is None:
                return f"skill_manage: no skill named '{name}' found to patch."
            skill_dir, source = resolved
            if source == "claude":
                return f"skill_manage: '{name}' is a bundled skill and cannot be modified."
            if not old:
                return "skill_manage: 'old' (exact text to replace) is required for patch."
            skill_md = skill_dir / "SKILL.md"
            content = skill_md.read_text(encoding="utf-8")
            count = content.count(old)
            if count == 0:
                return f"skill_manage: 'old' text not found in '{name}'/SKILL.md — patch aborted."
            if count > 1:
                return f"skill_manage: 'old' text appears {count} times in '{name}' — make it unique, patch aborted."
            versioning.snapshot(skill_dir, reason="patch", source="agent")
            skill_md.write_text(content.replace(old, new, 1), encoding="utf-8")
            _emit_tui_event("nova_skill_refined", f"Agent patched skill: {name}")
            return f"Patched skill '{name}' (previous version saved — rollback available)."

        if action == "edit":
            resolved = _resolve_existing_skill(name)
            if resolved is None:
                return f"skill_manage: no skill named '{name}' found to edit."
            skill_dir, source = resolved
            if source == "claude":
                return f"skill_manage: '{name}' is a bundled skill and cannot be modified."
            if not body:
                return "skill_manage: 'body' is required for edit (the new markdown content, no frontmatter)."
            skill_md = skill_dir / "SKILL.md"
            existing = skill_md.read_text(encoding="utf-8")
            frontmatter, _ = _split_frontmatter(existing)
            if description:
                from novacode_cli.skills.schema import yaml_str

                frontmatter = f"---\nname: {name}\ndescription: {yaml_str(description)}\n---\n"
            elif not frontmatter:
                frontmatter = f'---\nname: {name}\ndescription: "Reusable workflow: {name}"\n---\n'
            versioning.snapshot(skill_dir, reason="edit", source="agent")
            skill_md.write_text(f"{frontmatter}\n{body.strip()}\n", encoding="utf-8")
            _emit_tui_event("nova_skill_refined", f"Agent rewrote skill: {name}")
            return f"Rewrote skill '{name}' (previous version saved — rollback available)."

        if action == "delete":
            resolved = _resolve_existing_skill(name)
            if resolved is None:
                return f"skill_manage: no skill named '{name}' found to delete."
            skill_dir, source = resolved
            if source == "claude":
                return f"skill_manage: '{name}' is a bundled skill and cannot be deleted."
            # Soft-delete: move to the archive so it can be recovered.
            dest = versioning.archive_skill(skill_dir)
            # Best-effort: drop any install-lock entry for this skill.
            try:
                from novacode_cli.skills.skill_lock import SkillLock

                SkillLock.for_skills_dir(skill_dir.parent).remove(name)
            except Exception:  # noqa: BLE001
                pass
            _emit_tui_event("nova_skill_refined", f"Agent archived skill: {name}")
            if dest is None:
                return f"skill_manage: could not archive '{name}'."
            return f"Archived skill '{name}' (recoverable at {dest})."

        if action == "history":
            resolved = _resolve_existing_skill(name)
            if resolved is None:
                return f"skill_manage: no skill named '{name}' found."
            skill_dir, _ = resolved
            versions = versioning.list_versions(skill_dir)
            if not versions:
                return f"skill_manage: '{name}' has no saved version history yet."
            lines = [f"Version history for '{name}' (newest last):"]
            for v in versions:
                lines.append(f"  {v['id']}  [{v.get('source','?')}] {v.get('reason','')}")
            return "\n".join(lines)

        if action == "rollback":
            resolved = _resolve_existing_skill(name)
            if resolved is None:
                return f"skill_manage: no skill named '{name}' found to roll back."
            skill_dir, source = resolved
            if source == "claude":
                return f"skill_manage: '{name}' is a bundled skill and cannot be rolled back."
            ok, msg = versioning.restore(skill_dir, old or None)
            if ok:
                _emit_tui_event("nova_skill_refined", f"Rolled back skill: {name}")
                return f"Rolled back '{name}': {msg}."
            return f"skill_manage: rollback of '{name}' failed — {msg}."

        return (
            f"skill_manage: unknown action '{action}'. "
            "Use one of: create | patch | edit | delete | history | rollback."
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("skill_manage(%s, %s) failed", action, name)
        return f"skill_manage: '{action}' on '{name}' failed: {exc}"
