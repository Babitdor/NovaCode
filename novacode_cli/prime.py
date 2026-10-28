"""Session-scoped Deep Work checkpoints and observed execution evidence."""

from __future__ import annotations

import json
import time
import uuid
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, Literal

from novacode_cli.prime_memory import (
    DEPENDENCIES,
    PrimeMemory,
    continuation_context,
    fingerprints,
    freshness,
    input_paths,
    project_context,
    safe_text,
    sanitized,
    session_directory,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

Phase = Literal["define", "investigate", "implement", "verify", "challenge", "deliver"]
PHASES: tuple[Phase, ...] = ("define", "investigate", "implement", "verify", "challenge", "deliver")
EXECUTION_TOOLS = frozenset({"shell", "execute", "execute_code", "run_tests", "run_pytest", "bash"})
MAX_TOOLS = 128
MAX_REPORT = 100_000
MAX_CRITERIA = 12
MAX_EVIDENCE = 8
MAX_CHECKS = 32
MAX_CHECK_RESULTS = 64
MAX_EXIT_CODE = 255
Record = dict[str, Any]


def text(value: str, limit: int = 2000) -> str:
    """Require bounded notes without silently shortening evidence."""
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        msg = f"Use non-empty text of at most {limit} characters"
        raise ValueError(msg)
    return value


@dataclass
class PrimeRun:
    """One foreground request; checkpoints never confer tool permissions."""

    session: Any
    goal: str
    report: Record = field(init=False)
    lock: Any = field(default_factory=RLock, repr=False)
    memory: Any = field(init=False, default=None, repr=False)
    observations: Record = field(default_factory=dict, repr=False)
    starts: Record = field(default_factory=dict, repr=False)
    sequence: int = 0

    def __post_init__(self) -> None:
        """Start a new record, leaving the original user message untouched."""
        context = project_context(session_directory(self.session))
        pending = getattr(self.session, "prime_pending", None)
        previous = getattr(self.session, "prime_report", None)
        if (
            pending is None
            and isinstance(previous, dict)
            and previous.get("status")
            in {
                "paused",
                "error",
                "incomplete",
            }
        ):
            pending = continuation_context(previous)
        self.session.prime_pending = None
        source_goal = pending.get("goal") if isinstance(pending, dict) else None
        objective = (
            source_goal if isinstance(source_goal, str) and source_goal.strip() else self.goal
        )
        self.report = {
            **context,
            "version": 1,
            "id": uuid.uuid4().hex,
            "thread": self.session.thread_id,
            "goal": objective[:4000],
            "request": self.goal[:4000],
            "phase": "define",
            "status": "running",
            "criteria": [],
            "checkpoints": [],
            "tools": [],
            "checks": [],
            "evidence": [],
            "files": [],
            "dependencies": [],
            "fingerprints": {},
            "findings": [],
            "questions": [],
            "next_action": "Define completion criteria",
            "recalled": [],
            "parent_id": pending.get("id") if isinstance(pending, dict) else None,
            "continuation": pending if isinstance(pending, dict) else None,
        }
        self.session.prime_report = deepcopy(self.report)
        try:
            self.memory = PrimeMemory(context)
        except Exception:  # noqa: BLE001 - Storage must not break authorized foreground work.
            self.report["storage_error"] = (
                "Prime storage unavailable; drafts may not survive restart"
            )
        self.publish()
        if isinstance(pending, dict) and pending.get("files"):
            try:
                self.declare_inputs(pending["files"], pending.get("dependencies"))
            except (ValueError, OSError):
                self.report["continuation_warning"] = (
                    "Saved inputs unavailable; declare current workspace inputs"
                )
                self.publish()

    def is_current(self) -> bool:
        """Fence late results from cleared or superseded requests."""
        saved = getattr(self.session, "prime_report", None)
        return (
            self.session.thread_id == self.report["thread"]
            and isinstance(saved, dict)
            and saved.get("id") == self.report["id"]
        )

    def publish(self) -> None:
        """Replace the snapshot so UI/autosave readers never share mutable lists."""
        if self.is_current():
            snapshot = deepcopy(self.report)
            # Full definitions and excerpts have their own records. Task/session
            # snapshots retain IDs and results without multiplying captured output.
            snapshot["checks"] = [
                {
                    key: check[key]
                    for key in ("id", "purpose", "status", "finding", "criteria", "evidence_ids")
                }
                for check in self.report["checks"]
            ]
            snapshot["evidence"] = [
                {
                    key: evidence[key]
                    for key in (
                        "id",
                        "run_id",
                        "check_id",
                        "tool_call_id",
                        "status",
                        "reason",
                        "finding",
                        "exit_code",
                        "observed_at",
                    )
                }
                for evidence in self.report["evidence"]
            ]
            self.session.prime_report = sanitized(snapshot)
            self.persist("tasks", snapshot)

    def persist(self, kind: str, record: Record) -> None:
        """Storage failures degrade to visible local drafts without another provider request."""
        if self.memory is not None:
            try:
                self.memory.save(kind, record)
            except Exception:  # noqa: BLE001 - Best-effort durable memory boundary.
                self.report["storage_error"] = (
                    "Prime storage unavailable; drafts may not survive restart"
                )
            else:
                return
        self.report["storage_error"] = "Prime storage unavailable; drafts may not survive restart"
        if self.is_current():
            self.session.prime_report["storage_error"] = self.report["storage_error"]

    def declare_inputs(self, files: list[str], dependencies: list[str] | None = None) -> None:
        """Recall checks only after the agent has declared relevant file/dependency inputs."""
        with self.lock:
            root = self.report["root"]
            declared = input_paths(root, files)
            deps = input_paths(root, dependencies or self.report["dependencies"])
            deps = sorted(set(deps) | {name for name in DEPENDENCIES if Path(root, name).is_file()})
            self.report["files"] = declared
            self.report["dependencies"] = deps
            self.report["fingerprints"] = fingerprints(root, declared + deps)
            if self.memory is not None:
                try:
                    self.report["recalled"] = self.memory.recall(
                        declared,
                        deps,
                        exclude_run_id=self.report["id"],
                    )
                except Exception:  # noqa: BLE001 - Recall failure is context loss, not tool failure.
                    self.report["storage_error"] = "Prime recall unavailable"
            self.publish()

    def recall_context(self) -> str:
        """Keep automatic recalled context within five summaries and 2,000 characters."""
        return "\n".join(item["summary"] for item in self.report["recalled"][:5])[:2000]

    def progress(self) -> Record:
        """Expose immutable status and IDs without copying raw commands into event consumers."""
        with self.lock:
            checks = [
                {key: check[key] for key in ("id", "status", "finding")}
                for check in self.report["checks"]
            ]
            return {
                "phase": self.report["phase"],
                "status": self.report["status"],
                "checks": checks,
                "recalled_count": len(self.report["recalled"]),
                "storage_error": self.report.get("storage_error"),
            }

    def define_check(
        self,
        *,
        purpose: str,
        command: str,
        files: list[str],
        criteria: list[int],
        expected_exit: int = 0,
        contains: list[str] | None = None,
        dependencies: list[str] | None = None,
    ) -> Record:
        """Define immutable acceptance assertions before executing a command."""
        text(purpose, 500)
        text(command)
        if not criteria or any(
            type(i) is not int or not 0 <= i < len(self.report["criteria"]) for i in criteria
        ):
            msg = "Reference defined completion criteria by zero-based index"
            raise ValueError(msg)
        if type(expected_exit) is not int or abs(expected_exit) > MAX_EXIT_CODE:
            msg = "Expected exit code must be an integer between -255 and 255"
            raise ValueError(msg)
        assertions = [text(item, 300) for item in (contains or [])]
        if len(assertions) > MAX_EVIDENCE:
            msg = "Use at most eight assertions and 32 checks per run"
            raise ValueError(msg)
        with self.lock:
            if len(self.report["checks"]) >= MAX_CHECKS:
                msg = "Use at most eight assertions and 32 checks per run"
                raise ValueError(msg)
            self.declare_inputs(sorted(set(self.report["files"] + files)), dependencies)
            self.sequence += 1
            check = {
                "id": uuid.uuid4().hex,
                "run_id": self.report["id"],
                "version": 1,
                "purpose": purpose,
                "command": command,
                "cwd": self.report["cwd"],
                "criteria": criteria,
                "expected_exit": expected_exit,
                "contains": assertions,
                "files": input_paths(self.report["root"], files),
                "dependencies": self.report["dependencies"],
                "defined_at": self.sequence,
                "finding": "draft",
                "status": "unverified",
                "fingerprints": {},
                "revision": self.report["revision"],
                "evidence_ids": [],
            }
            self.report["checks"].append(check)
            self.persist("checks", check)
            self.publish()
            return deepcopy(check)

    def start_tool(self, call_id: str) -> None:
        """Capture declared inputs before execution, including parallel tool calls."""
        with self.lock:
            self.sequence += 1
            self.starts[call_id] = {
                "sequence": self.sequence,
                "fingerprints": fingerprints(
                    self.report["root"],
                    self.report["files"] + self.report["dependencies"],
                ),
            }

    def owns(self, config: Mapping[str, Any]) -> bool:
        """Reject inherited child/subgraph bindings and different conversation threads."""
        values = config.get("configurable", {})
        return (
            self.is_current()
            and values.get("thread_id") == self.report["thread"]
            and "|" not in str(values.get("checkpoint_ns", ""))
        )

    def observe(
        self,
        call_id: str,
        name: str,
        status: str,
        *,
        execution: Record | None = None,
    ) -> None:
        """Record a tool's actual returned status, not a model's assertion of success."""
        with self.lock:
            if call_id not in self.starts:
                self.start_tool(call_id)
            if call_id in self.observations:
                return
            self.observations[call_id] = {
                **self.starts.pop(call_id),
                "execution": deepcopy(execution),
                "name": name,
                "observed_at": time.time_ns(),
            }
            while len(self.observations) > MAX_TOOLS:
                del self.observations[next(iter(self.observations))]
            self.report["tools"].append(
                {"id": call_id, "name": name, "status": status, "phase": self.report["phase"]}
            )
            del self.report["tools"][:-MAX_TOOLS]
            self.publish()

    def record_check(self, check_id: str, tool_call_id: str) -> Record:
        """Bind assertions to trusted structured results, never to model-authored summaries."""
        with self.lock:
            check = next((c for c in self.report["checks"] if c["id"] == check_id), None)
            observation = self.observations.get(tool_call_id)
            if check is None or observation is None:
                msg = "Use a check and observed tool-call ID from this run"
                raise ValueError(msg)
            if observation["sequence"] <= check["defined_at"]:
                msg = "Define the check before starting execution"
                raise ValueError(msg)
            execution = observation["execution"]
            if isinstance(execution, dict) and (
                execution.get("command") != check["command"]
                or (execution.get("cwd") is not None and execution.get("cwd") != check["cwd"])
            ):
                msg = "Observed command and working directory must match the definition"
                raise ValueError(msg)
            status, reason = "unverified", "Structured completed execution metadata unavailable"
            inputs = {
                name: observation["fingerprints"].get(name)
                for name in check["files"] + check["dependencies"]
            }
            if isinstance(execution, dict) and (
                type(execution.get("exit_code")) is int
                and execution.get("completed") is True
                and execution.get("truncated") is False
                and isinstance(execution.get("output"), str)
                and execution.get("cwd") == check["cwd"]
                and observation["name"] in EXECUTION_TOOLS
            ):
                if not inputs or any(value is None for value in inputs.values()):
                    reason = "Declared inputs unavailable"
                elif freshness(self.report["root"], inputs) != "current":
                    reason = "Inputs changed during or after execution; rerun the check"
                else:
                    passed = execution["exit_code"] == check["expected_exit"] and all(
                        literal in execution["output"] for literal in check["contains"]
                    )
                    status = "passed" if passed else "failed"
                    reason = (
                        "Assertions satisfied"
                        if passed
                        else "Exit code or literal assertion failed"
                    )
            signature = (
                check["command"],
                check["files"],
                check["dependencies"],
                check["expected_exit"],
                check["contains"],
            )
            candidates = list(self.report["evidence"])
            if self.memory is not None:
                try:
                    candidates += self.memory.records("evidence")
                except Exception:  # noqa: BLE001 - Promotion fails closed when history is unavailable.
                    self.report["storage_error"] = "Prime evidence history unavailable"
            demonstrated = status == "passed" and any(
                e.get("signature") == list(signature)
                and e.get("status") == "failed"
                and e.get("observed_at", observation["observed_at"]) < observation["observed_at"]
                and any(
                    e.get("fingerprints", {}).get(name) != inputs.get(name)
                    for name in check["files"]
                )
                for e in candidates
            )
            evidence = {
                "id": uuid.uuid4().hex,
                "run_id": self.report["id"],
                "check_id": check_id,
                "tool_call_id": tool_call_id,
                "status": status,
                "reason": reason,
                "finding": "demonstrated" if demonstrated else "draft",
                "fingerprints": inputs,
                "signature": list(signature),
                "observed_at": observation["observed_at"],
                "cwd": self.report["cwd"],
                "revision": self.report["revision"],
                "exit_code": execution.get("exit_code") if isinstance(execution, dict) else None,
                "excerpt": safe_text(execution.get("output", ""))
                if isinstance(execution, dict)
                else "",
            }
            if len(self.report["evidence"]) >= MAX_CHECK_RESULTS:
                msg = "Use at most 64 evidence records per run"
                raise ValueError(msg)
            self.report["evidence"].append(evidence)
            check.update({key: evidence[key] for key in ("status", "finding", "fingerprints")})
            check["evidence_ids"].append(evidence["id"])
            self.persist("evidence", evidence)
            self.persist("checks", check)
            self.publish()
            return deepcopy(evidence)

    def refresh_checks(self) -> None:
        """Invalidate stale passes after subsequent edits or missing environments."""
        for check in self.report["checks"]:
            state = freshness(self.report["root"], check.get("fingerprints", {}))
            if check["status"] == "passed" and state != "current":
                check["status"] = "unverified"
                check["freshness"] = state
                self.persist("checks", check)

    def checkpoint(  # noqa: PLR0912 - Validate phase invariants before committing one checkpoint.
        self,
        phase: Phase,
        summary: str,
        criteria: list[str] | None = None,
        evidence: list[str] | None = None,
        unverified_reason: str | None = None,
        files: list[str] | None = None,
        dependencies: list[str] | None = None,
        questions: list[str] | None = None,
        next_action: str | None = None,
    ) -> Record:
        """Advance in order; evidence references must resolve to this run's real results."""
        text(summary)
        if questions is not None:
            if len(questions) > MAX_CRITERIA:
                msg = "Use at most twelve unresolved questions"
                raise ValueError(msg)
            questions = [text(q, 300) for q in questions]
        if next_action is not None:
            text(next_action, 500)
        if files is not None:
            input_paths(self.report["root"], files)
            input_paths(self.report["root"], dependencies or [])
        with self.lock:
            records = self.report["checkpoints"]
            if len(records) >= len(PHASES) or phase != PHASES[len(records)]:
                msg = "Record the current Prime phase in order; completed phases are immutable"
                raise ValueError(msg)
            if phase == "define":
                if not criteria or len(criteria) > MAX_CRITERIA:
                    msg = "Define 1-12 concrete completion criteria before investigating"
                    raise ValueError(msg)
                criteria = [text(item, 300) for item in criteria]
            elif criteria:
                msg = "Completion criteria are set during Define; new scope needs an owner decision"
                raise ValueError(msg)
            evidence = list(dict.fromkeys(evidence or []))
            tools = {item["id"]: item for item in self.report["tools"]}
            if len(evidence) > MAX_EVIDENCE or any(key not in tools for key in evidence):
                msg = (
                    "Evidence must reference at most eight observed tool-call IDs from this request"
                )
                raise ValueError(msg)
            if unverified_reason is not None:
                text(unverified_reason)
            if (
                phase == "verify"
                and not unverified_reason
                and not any(
                    tools[key]["name"] in EXECUTION_TOOLS and tools[key]["status"] == "success"
                    for key in evidence
                )
            ):
                msg = "Cite observed execution evidence or explain what could not be verified"
                raise ValueError(msg)
            record = {
                "phase": phase,
                "summary": summary,
                "evidence": evidence,
                "unverified_reason": unverified_reason,
            }
            records.append(record)
            self.refresh_checks()
            self.report["findings"] = [r["summary"] for r in records]
            if questions is not None:
                self.report["questions"] = questions
            self.report["next_action"] = (
                text(next_action, 500)
                if next_action
                else (PHASES[min(len(records), len(PHASES) - 1)])
            )
            if files is not None:
                self.declare_inputs(files, dependencies)
            self.report["fingerprints"] = fingerprints(
                self.report["root"],
                self.report["files"] + self.report["dependencies"],
            )
            self.report["revision"] = project_context(self.report["cwd"])["revision"]
            if phase == "define":
                self.report["criteria"] = criteria
            self.report["phase"] = PHASES[min(len(records), len(PHASES) - 1)]
            self.publish()
            return deepcopy(record)

    def finish(self, outcome: str) -> None:
        """Cancellation/restart pauses work; missing milestones cannot become a success claim."""
        with self.lock:
            self.refresh_checks()
            self.report["fingerprints"] = fingerprints(
                self.report["root"],
                self.report["files"] + self.report["dependencies"],
            )
            self.report["revision"] = project_context(self.report["cwd"])["revision"]
            records = self.report["checkpoints"]
            if outcome != "complete":
                status = "error" if outcome == "error" else "paused"
            elif len(records) != len(PHASES):
                status = "incomplete"
            else:
                status = (
                    "delivered_with_limits"
                    if any(r["unverified_reason"] for r in records)
                    or any(c["status"] != "passed" for c in self.report["checks"])
                    or (
                        set(range(len(self.report["criteria"])))
                        - {
                            i
                            for c in self.report["checks"]
                            if c["status"] == "passed"
                            for i in c["criteria"]
                        }
                    )
                    else "delivered"
                )
            self.report["status"] = status
            self.publish()


current_run: ContextVar[PrimeRun | None] = ContextVar("nova_prime_run", default=None)


def receipt(report: Record) -> str:
    """Separate checkpoint assessments from observed facts in a compact delivery record."""
    records = report.get("checkpoints", [])
    lines = [
        f"Prime Deep Work · {report.get('status', 'incomplete').replace('_', ' ')}",
        f"Checkpoints: {len(records)}/{len(PHASES)}",
    ]
    if report.get("criteria"):
        lines.append("Completion criteria: " + "; ".join(report["criteria"]))
        covered = {
            i
            for c in report.get("checks", [])
            if c.get("status") == "passed"
            and freshness(
                report.get("root", "."), c.get("fingerprints", report.get("fingerprints", {}))
            )
            == "current"
            for i in c.get("criteria", [])
        }
        unmet = [criterion for i, criterion in enumerate(report["criteria"]) if i not in covered]
        if unmet:
            lines.append("Unverified criteria: " + "; ".join(unmet))
    lines.extend(
        f"Check {check['id']}: {check['status']} · {check['purpose']} · {check['finding']} · "
        + freshness(
            report.get("root", "."),
            check.get("fingerprints", report.get("fingerprints", {})),
        )
        for check in report.get("checks", [])
    )
    lines.extend(
        f"{evidence['status'].title()}: {evidence['reason']}"
        for evidence in report.get("evidence", [])
        if evidence["status"] != "passed"
    )
    if report.get("storage_error"):
        lines.append(report["storage_error"])
    for record in records:
        if record["phase"] in {"verify", "challenge", "deliver"}:
            lines.append(f"{record['phase'].title()} (agent assessment): {record['summary']}")
        if record.get("unverified_reason"):
            lines.append("Not verified: " + record["unverified_reason"])
    lines.append(
        f"Observed tool results: {len(report.get('tools', []))}. "
        "A tool returning successfully does not by itself prove tests passed."
    )
    if len(records) < len(PHASES):
        lines.append("Missing checkpoints: " + ", ".join(PHASES[len(records) :]))
    return safe_text("\n".join(lines), MAX_REPORT)


def prime_payload(session: Any) -> Record | None:  # noqa: ANN401 - Real and minimal session proxies.
    """Save a mode and its thread-bound checkpoint snapshot with normal session metadata."""
    report = getattr(session, "prime_report", None)
    if not isinstance(report, dict) or report.get("thread") != getattr(session, "thread_id", None):
        report = None
    enabled = getattr(session, "prime_enabled", False) is True
    pending = getattr(session, "prime_pending", None)
    return (
        {"enabled": enabled, "report": deepcopy(report), "pending": deepcopy(pending)}
        if enabled or report
        else None
    )


def _valid_saved_records(report: Record) -> bool:
    """Validate new optional collections while keeping the original session schema readable."""
    if any(not isinstance(report.get(key, ""), str) for key in ("goal", "root", "cwd")):
        return False
    inputs = report.get("fingerprints", {})
    if not isinstance(inputs, dict) or any(
        not isinstance(key, str) or (value is not None and not isinstance(value, str))
        for key, value in inputs.items()
    ):
        return False
    fields = (
        ("checks", MAX_CHECKS, ("id", "purpose", "status", "finding")),
        (
            "evidence",
            MAX_CHECK_RESULTS,
            ("id", "run_id", "check_id", "tool_call_id", "status", "reason", "finding"),
        ),
        ("recalled", 5, ("id", "summary")),
    )
    for name, limit, required in fields:
        records = report.get(name, [])
        if not isinstance(records, list) or len(records) > limit:
            return False
        if any(
            not isinstance(item, dict)
            or any(not isinstance(item.get(key), str) for key in required)
            for item in records
        ):
            return False
        if name in {"checks", "evidence"} and any(
            item["status"] not in {"passed", "failed", "unverified"}
            or item["finding"] not in {"draft", "demonstrated"}
            for item in records
        ):
            return False
    return all(
        isinstance(c.get("criteria"), list)
        and all(type(i) is int and 0 <= i < MAX_CRITERIA for i in c["criteria"])
        for c in report.get("checks", [])
    )


def restore_prime(session: Any, saved: object) -> None:  # noqa: ANN401 - Session proxy boundary.
    """Old sessions default off; interrupted records stay paused with no automatic replay."""
    session.prime_enabled = False
    session.prime_report = None
    session.prime_pending = None
    if not isinstance(saved, dict):
        return
    session.prime_enabled = saved.get("enabled") is True
    report = saved.get("report")
    if not isinstance(report, dict) or report.get("thread") != session.thread_id:
        return
    try:
        encoded = json.dumps(report, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        return
    if (
        len(encoded) > MAX_REPORT
        or not _valid_saved_records(report)
        or not isinstance(report.get("status"), str)
        or report.get("status")
        not in {"running", "paused", "error", "incomplete", "delivered", "delivered_with_limits"}
        or report.get("phase") not in PHASES
        or not isinstance(report.get("checkpoints"), list)
        or len(report["checkpoints"]) > len(PHASES)
        or not isinstance(report.get("criteria"), list)
        or len(report["criteria"]) > MAX_CRITERIA
        or any(not isinstance(item, str) for item in report["criteria"])
        or not isinstance(report.get("tools"), list)
        or len(report["tools"]) > MAX_TOOLS
        or any(
            not isinstance(t, dict)
            or not isinstance(t.get("id"), str)
            or not isinstance(t.get("name"), str)
            or not isinstance(t.get("status"), str)
            or t.get("status") not in {"success", "error", "unknown"}
            for t in report["tools"]
        )
        or any(
            not isinstance(r, dict)
            or r.get("phase") not in PHASES
            or not isinstance(r.get("summary"), str)
            or not isinstance(r.get("evidence"), list)
            or any(not isinstance(key, str) for key in r["evidence"])
            or (
                r.get("unverified_reason") is not None
                and not isinstance(r["unverified_reason"], str)
            )
            for r in report["checkpoints"]
        )
        or [r["phase"] for r in report["checkpoints"]] != list(PHASES[: len(report["checkpoints"])])
    ):
        return
    records = report["checkpoints"]
    if (
        report["phase"] != PHASES[min(len(records), len(PHASES) - 1)]
        or (records and not report["criteria"])
        or (
            report["status"] in {"delivered", "delivered_with_limits"}
            and len(records) != len(PHASES)
        )
    ):
        return
    session.prime_report = deepcopy(report)
    pending = saved.get("pending")
    if isinstance(pending, dict) and pending.get("id") == report.get("id"):
        session.prime_pending = deepcopy(pending)
    if report.get("status") == "running":
        session.prime_report["status"] = "paused"
