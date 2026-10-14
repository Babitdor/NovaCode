"""Shared review controls; decisions always name a displayed ledger revision."""

import asyncio
from pathlib import Path

from novacode_cli.commands import CommandContext, CommandRegistry
from novacode_cli.hermes.ledger import LearningLedger


def handle_learning_review(args: str, *, agent_dir: Path, project: str) -> str:
    """List concrete proposals or apply a decision for an exact revision."""
    parts = args.split()
    action = parts[0] if parts else "review"
    ledger = LearningLedger(agent_dir / "learning.sqlite3")
    if action == "review":
        from novacode_cli.hermes.activation import content_hash
        from novacode_cli.prompts import PROMPT_HISTORY_DIR, TEMPLATES_DIR, generated_override

        imported_hashes = {(r.target, r.content) for r in ledger.list() if r.kind == "prompt"}
        for path in PROMPT_HISTORY_DIR.glob("*/active.jinja"):
            if generated_override(path):
                target = path.parent.name + ".jinja"
                content = path.read_text(encoding="utf-8")
                source = TEMPLATES_DIR / target
                if source.is_file() and (target, content) not in imported_hashes:
                    ledger.propose(
                        content=content,
                        target=target,
                        kind="prompt",
                        scope="shared",
                        source_hash=content_hash(source.read_bytes()),
                        origin="legacy_prompt",
                        evidence=(str(path), "approval unavailable"),
                    )
        generated = []
        roots = [agent_dir / "memories", agent_dir / "memories" / "projects" / project]
        for root in roots:
            for path in root.glob("*.md"):
                body = path.read_text(encoding="utf-8")
                if (
                    "Lessons captured during reviews / dreams." in body[:300]
                    and "pinned: true" not in body
                ):
                    generated.append(path)
        ledger.import_legacy(generated, project=project)
        records = ledger.list(project=project)
        ambiguous = [
            str(path)
            for path in PROMPT_HISTORY_DIR.glob("*/active.jinja")
            if not generated_override(path)
        ]
        ownership_note = (
            (
                "\n\nPreserved manual or ambiguous overrides (ownership review required): "
                + ", ".join(ambiguous)
            )
            if ambiguous
            else ""
        )
        if not records:
            return "No learning proposals." + ownership_note
        return (
            "\n\n".join(
                f"{r.reference} · {r.scope} · {r.kind} · {r.status}\n"
                f"Applicability: {r.applicability or 'unspecified'}\n"
                f"Target: {r.target or r.kind}; source hash: {r.source_hash or 'unverified'}\n"
                f"Evidence: {', '.join(r.evidence) or 'unverified'}\n"
                f"Intended change: {r.content}\nRevisions: "
                + ", ".join(f"{v.revision}:{v.status}" for v in ledger.history(r.id))
                for r in records
            )
            + ownership_note
        )
    if action in {"approve", "reject", "rollback"} and len(parts) == 2:  # noqa: PLR2004
        # A project command must not approve another project's proposal.
        identity = parts[1].partition("@")[0]
        history = ledger.history(identity)
        if not history or (history[-1].scope == "project" and history[-1].project != project):
            msg = "Proposal does not belong to this project"
            raise ValueError(msg)
        record = ledger.transition(parts[1], action)
        try:
            ledger.export(agent_dir / "learning-approved.md", project=project)
        except OSError as error:
            return (
                f"{record.reference}: {record.status}. Ledger saved; "
                f"markdown export failed: {error}"
            )
        return f"{record.reference}: {record.status}. Application occurs at the next turn boundary."
    msg = (
        "Usage: /learning review|approve <id@revision>|reject <id@revision>|rollback <id@revision>"
    )
    raise ValueError(msg)


async def handle_learning_command(ctx: CommandContext) -> bool:
    """Expose the same learning decisions on console session transports."""
    from novacode_cli.config.config import console, settings
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.hermes.memory_tiers import project_memory_key

    args = (ctx.cmd_args or "status").strip()
    cfg = NovaConfig()
    if args in {"on", "off"}:
        cfg.set_learning_enabled(args == "on")
        if ctx.session_state:
            from novacode_cli.config.model_create import create_model

            await ctx.session_state.switch_model(create_model())
        console.print(f"Learning {args}; generated changes require review.")
    elif args == "status":
        console.print(
            f"Learning {'on' if cfg.get_learning_enabled() else 'off'}. "
            "/learning review lists proposals."
        )
    else:
        try:
            result = await asyncio.to_thread(
                handle_learning_review,
                args,
                agent_dir=settings.get_agent_dir(ctx.assistant_id),
                project=project_memory_key(Path.cwd()),
            )
            console.print(result, markup=False)
        except (ValueError, OSError) as error:
            console.print(str(error), markup=False)
    return True


def register_commands(registry: CommandRegistry) -> None:
    """Register the shared console learning command."""
    registry.register("learning", handle_learning_command)
