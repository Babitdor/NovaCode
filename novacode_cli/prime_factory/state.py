"""Validated factory plans and immutable acceptance definitions."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from novacode_cli.prime_factory.store import FactoryError

MAX_OWNED_FILES = 128
MAX_TASKS = 64
MAX_CHECKS = 32
MAX_ARGV = 64
MAX_ASSERTIONS = 8


def digest(value: object) -> str:
    """Hash lossless canonical data for approvals and observed evidence."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def bounded_text(value: object, limit: int = 4000) -> str:
    """Reject truncated or missing worker assertions."""
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        msg = "Expected bounded nonempty text"
        raise FactoryError(msg)
    return value


def paths(value: object) -> list[str]:
    """Validate repository-relative ownership, including Windows on Linux hosts."""
    if not isinstance(value, list) or len(value) > MAX_OWNED_FILES:
        msg = "Expected at most 128 owned files"
        raise FactoryError(msg)
    result = []
    for item in value:
        name = bounded_text(item, 500).replace("\\", "/")
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or PureWindowsPath(name).drive
            or ".." in path.parts
            or any(part in {".git", ".nova", ".env", ".aws", ".codex"} for part in path.parts)
            or any(char in name for char in "*?[]\x00\n\r")
        ):
            msg = "Ownership must name explicit repository files"
            raise FactoryError(msg)
        result.append(path.as_posix())
    if len(result) != len(set(result)):
        msg = "Duplicate file ownership"
        raise FactoryError(msg)
    return result


def validate_tasks(value: object) -> list[dict[str, Any]]:
    """Require an acyclic dependency graph before approving any implementation."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_TASKS:
        msg = "Plan requires 1-64 tasks"
        raise FactoryError(msg)
    tasks: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            msg = "Invalid task"
            raise FactoryError(msg)
        key = bounded_text(raw.get("id"), 60)
        if not key.replace("_", "").replace("-", "").isalnum():
            msg = "Task IDs must be simple identifiers"
            raise FactoryError(msg)
        dependencies = raw.get("depends_on", [])
        if not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies):
            msg = "Invalid task dependencies"
            raise FactoryError(msg)
        tasks.append(
            {
                "id": key,
                "description": bounded_text(raw.get("description")),
                "files": paths(raw.get("files")),
                "depends_on": dependencies,
                "status": "pending",
                "attempts": [],
            }
        )
    keys = {task["id"] for task in tasks}
    if len(keys) != len(tasks):
        msg = "Duplicate task IDs"
        raise FactoryError(msg)
    resolved: set[str] = set()
    while len(resolved) < len(tasks):
        ready = {t["id"] for t in tasks if set(t["depends_on"]) <= resolved}
        if ready <= resolved:
            msg = "Task dependencies contain a cycle or unknown task"
            raise FactoryError(msg)
        resolved |= ready
    return tasks


def check_definition(check: dict[str, Any]) -> dict[str, Any]:
    """Keep approval and execution signatures on the same acceptance fields."""
    return {
        **{key: check[key] for key in ("argv", "criteria", "purpose", "test_first", "security")},
        **{key: check.get(key, []) for key in ("tasks", "contains", "failure_contains")},
    }


def validate_checks(
    value: object, criteria: list[str], task_ids: set[str] | list[str] | None = None
) -> list[dict[str, Any]]:
    """Assign identity from definitions so renaming cannot reset repair limits."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CHECKS:
        msg = "Define 1-32 acceptance checks"
        raise FactoryError(msg)
    result: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            msg = "Invalid acceptance definition"
            raise FactoryError(msg)
        argv = raw.get("argv")
        indexes = raw.get("criteria")
        if not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV:
            msg = "Checks require command argument arrays"
            raise FactoryError(msg)
        argv = [bounded_text(arg, 2000) for arg in argv]
        if (
            not isinstance(indexes, list)
            or not indexes
            or any(type(i) is not int or not 0 <= i < len(criteria) for i in indexes)
        ):
            msg = "Checks must cover existing acceptance criteria"
            raise FactoryError(msg)
        owners = raw.get("tasks", sorted(task_ids or []))
        if (
            not isinstance(owners, list)
            or any(not isinstance(owner, str) for owner in owners)
            or len(owners) != len(set(owners))
            or (task_ids is not None and (not owners or not set(owners) <= set(task_ids)))
        ):
            msg = "Acceptance tasks must identify responsible planned tasks"
            raise FactoryError(msg)
        assertions = {}
        for field in ("contains", "failure_contains"):
            expected = raw.get(field, [])
            if not isinstance(expected, list) or len(expected) > MAX_ASSERTIONS:
                msg = "Acceptance output assertions require at most eight literals"
                raise FactoryError(msg)
            assertions[field] = [bounded_text(term, 500) for term in expected]
        definition = {
            "argv": argv,
            "criteria": sorted(set(indexes)),
            "purpose": bounded_text(raw.get("purpose")),
            "test_first": raw.get("test_first") is True,
            "security": raw.get("security") is True,
            "tasks": sorted(owners),
            **assertions,
        }
        result.append(
            {
                **definition,
                "id": digest(definition),
                "failures": 0,
                "status": "pending",
                "evidence": [],
            }
        )
    if len({c["id"] for c in result}) != len(result):
        msg = "Duplicate acceptance definitions"
        raise FactoryError(msg)
    if {i for c in result for i in c["criteria"]} != set(range(len(criteria))):
        msg = "Every criterion needs an acceptance check"
        raise FactoryError(msg)
    return result


def plan_definition(record: dict[str, Any]) -> dict[str, Any]:
    """Include every approval-relevant field while excluding mutable task/check status."""
    return {
        "goal": record["goal"],
        "criteria": record["criteria"],
        "tasks": [
            {k: t[k] for k in ("id", "description", "files", "depends_on")} for t in record["tasks"]
        ],
        "checks": [{"id": c["id"], **check_definition(c)} for c in record["checks"]],
        **{
            key: record.get(key)
            for key in (
                "decisions",
                "sources",
                "research_requirements",
                "research_exemption",
                "isolation",
                "routes",
                "budget",
                "output_token_limit",
                "setup",
                "test_files",
                "destination",
                "baseline_selection",
            )
        },
    }
