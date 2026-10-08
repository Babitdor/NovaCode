"""Display records for background agent work outside the shell registry."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentTask:
    task_id: str
    command: str
    started_at: float = field(default_factory=time.monotonic)
    status: str = "running"
    finished_at: float | None = None
    worker: Any = None
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    exit_code: int | None = None

    def finish(self, status: str) -> None:
        if self.status == "running":
            self.status = status
            self.finished_at = time.monotonic()

    def runtime(self) -> float:
        return max(0.0, (self.finished_at or time.monotonic()) - self.started_at)

    def status_glyph(self) -> str:
        return {"running": "●", "done": "✓", "failed": "✖", "terminated": "■"}.get(self.status, "○")

    @property
    def output(self) -> str:
        return "\n".join(self.logs)
