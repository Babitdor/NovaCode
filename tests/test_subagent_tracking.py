"""Memory bounds for per-turn subagent activity summaries."""

from __future__ import annotations

from novacode_cli.core.streaming import format_condensed_activity
from novacode_cli.core.subagent_tracking import SubagentTracker


def test_subagent_activity_keeps_counts_without_retaining_every_tool_call() -> None:
    tracker = SubagentTracker()
    namespace = ("subagents", "worker")
    categories = {"read_file": "files_read"}

    for i in range(2_000):
        tracker.record_tool_call(
            namespace,
            "worker",
            "read_file",
            {"path": f"file-{i}.py"},
            categories,
        )
    for _ in range(500):
        tracker.record_error(namespace, "read_file")

    activity = tracker.subagent_activity_by_ns[namespace]
    assert "calls" not in activity
    assert len(activity["files_read"]) == 10
    assert activity["files_read_count"] == 2_000
    assert activity["error_count"] == 500
    assert "📖 file-0.py, file-1.py, file-2.py, +1997" in format_condensed_activity(activity)
    assert "❌ 500" in format_condensed_activity(activity)
