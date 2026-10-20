"""Stage autonomous skill changes without changing the active library."""

from __future__ import annotations

from pathlib import Path

from novacode_cli.hermes.activation import content_hash
from novacode_cli.hermes.ledger import auto_approve_enabled, main_agent_learning_ledger


def propose_skill(
    name: str,
    content: str,
    *,
    source: Path | None = None,
    archive: bool = False,
    origin: str = "skill_manage",
    evidence: tuple[str, ...] = (),
) -> str:
    """Return a revision reference for a create, update, or recoverable archive."""
    from novacode_cli.config.config import Settings
    from novacode_cli.hermes.memory_tiers import project_memory_key
    from novacode_cli.skills.schema import prepare_skill_content

    if not archive:
        content = prepare_skill_content(content, name)
    settings = Settings.from_environment()
    user_root = settings.ensure_user_skills_dir().resolve()
    scope = "shared" if source is None or source.resolve().parent.parent == user_root else "project"
    source_hash = content_hash(source.read_bytes()) if source else ""
    record = main_agent_learning_ledger().propose(
        content=content,
        project=project_memory_key(Path.cwd()),
        scope=scope,
        kind="skill_archive" if archive else "skill",
        target=name,
        source_hash=source_hash,
        source_path=str(source.resolve()) if source else "",
        origin=origin,
        evidence=evidence,
        auto_approve=auto_approve_enabled(),
    )
    return record.reference
