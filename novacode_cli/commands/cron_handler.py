"""``/cron`` command — manage scheduled (heartbeat) tasks (Enhancement 3).

Subcommands::

    /cron                      list scheduled jobs
    /cron list
    /cron add "0 9 * * *" "review the project and summarise what's left"
    /cron remove <job_id>
    /cron now "check if CI is broken"   fire a one-off task immediately

Jobs are fired by :class:`~novacode_cli.remote.scheduler.CronScheduler`, which
puts them on the same queue the remote bridges use, so they run like any prompt.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

    from novacode_cli.remote.scheduler import CronScheduler
    from novacode_cli.states.Session import SessionState

_ADD_PARTS = 2


async def _ensure_scheduler(session_state: SessionState) -> CronScheduler:
    """Return the session's scheduler, creating + starting it on first use."""
    scheduler = getattr(session_state, "_cron_scheduler", None)
    if scheduler is None:
        from novacode_cli.memory.store import get_durable_store
        from novacode_cli.remote.scheduler import CronScheduler

        queue = getattr(session_state, "_remote_message_queue", None)
        if queue is None:
            import asyncio

            queue = asyncio.Queue(maxsize=100)
            session_state._remote_message_queue = queue
        scheduler = CronScheduler(queue, store=get_durable_store())
        session_state._cron_scheduler = scheduler
    if not scheduler.running:
        await scheduler.start()
    return scheduler


async def handle_cron_command(  # noqa: PLR0911 — command dispatcher, one return per subcommand
    cmd_args: str | None, session_state: SessionState, console: Console
) -> bool:
    """Dispatch a ``/cron`` subcommand. Returns ``True`` (command handled)."""
    scheduler = await _ensure_scheduler(session_state)
    args = (cmd_args or "").strip()

    if not args or args.split()[0] == "list":
        _print_jobs(scheduler, console)
        return True

    try:
        tokens = shlex.split(args)
    except ValueError as exc:
        console.print(f"[red]Could not parse arguments: {exc}[/red]")
        return True

    action = tokens[0]
    rest = tokens[1:]

    if action == "add":
        if len(rest) < _ADD_PARTS:
            console.print('[yellow]Usage:[/yellow] /cron add "<cron expr>" "<task>"')
            return True
        cron_expr, task = rest[0], rest[1]
        try:
            job_id = await scheduler.add_job(cron_expr, task)
        except ValueError as exc:
            console.print(f"[red]Invalid cron expression:[/red] {exc}")
            return True
        console.print(
            f"  [green]✓[/green] Scheduled job [cyan]{job_id}[/cyan] "
            f"([dim]{cron_expr}[/dim]): {task}"
        )
        return True

    if action == "remove":
        if not rest:
            console.print("[yellow]Usage:[/yellow] /cron remove <job_id>")
            return True
        removed = await scheduler.remove_job(rest[0])
        if removed:
            console.print(f"  [green]✓[/green] Removed job [cyan]{rest[0]}[/cyan]")
        else:
            console.print(f"  [dim]No job with id {rest[0]}[/dim]")
        return True

    if action == "now":
        if not rest:
            console.print('[yellow]Usage:[/yellow] /cron now "<task>"')
            return True
        await scheduler.fire_now(rest[0])
        console.print(f"  [green]✓[/green] Fired one-off task: {rest[0]}")
        return True

    console.print(f"[yellow]Unknown /cron subcommand:[/yellow] {action}")
    return True


def _print_jobs(scheduler: CronScheduler, console: Console) -> None:
    """Render the active job list."""
    jobs = scheduler.list_jobs()
    if not jobs:
        console.print('[dim]No scheduled jobs. Add one with /cron add "0 9 * * *" "task".[/dim]')
        return
    console.print("[bold]Scheduled jobs[/bold]")
    for job in jobs:
        console.print(
            f"  [cyan]{job.get('job_id', '?')}[/cyan] "
            f"[dim]{job.get('cron_expr', '?')}[/dim] — {job.get('task', '')}"
        )
