"""Granular tools over the same session scheduler as /cron."""

from langchain.tools import tool


def build_cron_tools(session_state) -> list:
    """Bind jobs to this session's queue, lifecycle and durable scheduler."""
    from novacode_cli.commands.cron_handler import _ensure_scheduler

    async def get_scheduler():
        if not getattr(session_state, "headless", False):
            return await _ensure_scheduler(session_state)
        # A one-shot run can inspect/delete saved jobs without starting a queue
        # producer that has no consumer and cannot outlive this process.
        import asyncio

        from novacode_cli.memory.store import get_durable_store
        from novacode_cli.remote.scheduler import CronScheduler

        scheduler = CronScheduler(asyncio.Queue(), store=get_durable_store())
        await scheduler._load_jobs()
        return scheduler

    @tool("CronCreateTool")
    async def create(cron_expr: str, task: str) -> dict:
        """Schedule a recurring agent task using five-field cron in local time.

        Jobs persist across restarts and run while an interactive Nova session is
        open. Creation follows Nova's approval policy for recurring work.
        """
        from novacode_cli.remote.scheduler import parse_cron

        if getattr(session_state, "headless", False):
            return {"error": "Recurring tasks require an interactive Nova session."}
        if not task.strip():
            return {"error": "task must not be blank."}
        try:
            parse_cron(cron_expr)
        except ValueError as exc:
            return {"error": str(exc)}
        scheduler = await get_scheduler()
        job_id = await scheduler.add_job(cron_expr, task.strip())
        return {
            "job_id": job_id,
            "cron_expr": cron_expr,
            "task": task.strip(),
            "timezone": "local",
            "running": scheduler.running,
        }

    @tool("CronDeleteTool")
    async def delete(job_id: str) -> dict:
        """Delete a recurring job by the exact ID from CronListTool."""
        if not job_id.strip():
            return {"error": "job_id must not be blank."}
        scheduler = await get_scheduler()
        return {"job_id": job_id, "deleted": await scheduler.remove_job(job_id)}

    @tool("CronListTool")
    async def list_jobs() -> dict:
        """List persisted recurring jobs with their IDs, cron expressions and tasks."""
        scheduler = await get_scheduler()
        return {"jobs": scheduler.list_jobs(), "timezone": "local", "running": scheduler.running}

    return [create, delete, list_jobs]
