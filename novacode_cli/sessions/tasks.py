"""A tab's read-only job snapshots and controls routed to its own worker."""

from __future__ import annotations

from collections import deque
import time

from novacode_cli.shell.jobs import BackgroundJob


def job_snapshot(registry, *, include_logs=False):
    return [
        dict(
            id=j.id,
            command=j.command,
            tool_name=j.tool_name,
            started_at=j.started_at,
            status=j.status,
            exit_code=j.exit_code,
            finished_at=j.finished_at,
            pid=j.pid,
            logs=[j.output[-20000:]] if include_logs else [],
        )
        for j in registry.list_jobs()
    ]


class TabJobRegistry:
    def __init__(self, send):
        self._send = send
        self._jobs = {}

    def update(self, rows):
        jobs = {}
        for row in rows:
            data = dict(row)
            logs = data.pop("logs", [])
            job = BackgroundJob(**data)
            job.logs = deque(logs, maxlen=1000)
            jobs[job.id] = job
        self._jobs = jobs

    def list_jobs(self):
        return list(self._jobs.values())

    def active(self):
        return [job for job in self.list_jobs() if job.status == "running"]

    def mark_exited(self):
        for job in self.active():
            job.status = "terminated"
            job.finished_at = time.time()

    def resolve(self, ref):
        try:
            return self._jobs.get(int(str(ref).removeprefix("task_")))
        except (ValueError, TypeError):
            return None

    def terminate(self, job_id):
        job = self.resolve(job_id)
        if job is None or job.status != "running":
            return False
        self._send("terminate", job.id)
        return True

    def restart(self, job_id):
        self._send("restart", job_id)

    def clear_completed(self):
        self._send("clear", None)

    def refresh(self):
        self._send("list", None)
