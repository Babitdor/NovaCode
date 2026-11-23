"""Session-loop helpers shared by the terminal UI and the socket port.

A loop is a bounded recurrence the *host* owns: nothing here starts a turn, and
nothing survives the session. The terminal UI polls its own timer; the port
polls a task. Both ask this module what to say and what to run, so the two
frontends cannot drift on the wording or on which skill a `/loop` names.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from novacode_cli.loops import LoopSpec, load_instructions

if TYPE_CHECKING:
    from typing import Any

POLL_SECONDS = 1.0
"""How often a host looks for a due job. One tick, no catch-up queue."""

INHERIT_NOTE = (
    "This iteration inherits the session's existing tool permissions and approvals. "
    "Use schedule_wakeup(stop=true) when the task is complete or cancelled. "
    "For a self-paced loop, call schedule_wakeup(delay_seconds=...) to continue; "
    "without that call it stops after this iteration."
)


def instruction(job_id: str, prompt: str) -> str:
    """The turn text one loop iteration runs.

    Args:
        job_id: The loop's identity, so the model can address it.
        prompt: The loop's own instruction, already resolved.

    Returns:
        The instruction with its provenance, permissions note and exit rule.
    """
    return chr(10).join((f"[Session loop {job_id}]", prompt, "", INHERIT_NOTE))


async def iteration_prompt(state: Any, spec: LoopSpec, assistant_id: str) -> str:  # noqa: ANN401
    """Resolve what one iteration should do, off the event loop.

    Args:
        state: The session state (its workspace root, if it has one).
        spec: The loop's definition; either a prompt or "use loop.md".
        assistant_id: The assistant whose skills a `/skill:` prompt may name.

    Returns:
        The prompt text to run this iteration.

    Raises:
        ValueError: A direct `--run` skill or an unknown skill name.
    """
    project = getattr(state, "workspace_root", None)
    if not project:
        from novacode_cli.config.config import settings

        project = settings.get_workspace_root()
    prompt = spec.prompt or await asyncio.to_thread(load_instructions, str(project or ""))
    if not prompt.startswith("/"):
        return prompt
    from novacode_cli.commands.skill_invoke import _try_skill_invocation

    name, _, arguments = prompt[1:].partition(" ")
    if arguments.strip().startswith("--run"):
        message = "Loop skills use the agent's tool approvals; direct --run is unsupported."
        raise ValueError(message)
    skill = await _try_skill_invocation(
        name.removeprefix("skill:"), arguments or None, state, assistant_id
    )
    if skill is None:
        message = f"Unknown loop skill: /{name}"
        raise ValueError(message)
    return skill.prompt
