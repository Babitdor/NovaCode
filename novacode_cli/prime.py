"""Session-scoped Deep Work checkpoints and observed execution evidence."""

from __future__ import annotations

import json
import uuid
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

Phase = Literal["define", "investigate", "implement", "verify", "challenge", "deliver"]
PHASES: tuple[Phase, ...] = ("define", "investigate", "implement", "verify", "challenge", "deliver")
EXECUTION_TOOLS = frozenset({"shell", "execute", "execute_code", "run_tests", "run_pytest", "bash"})
MAX_TOOLS = 128
MAX_REPORT = 100_000
MAX_CRITERIA = 12
MAX_EVIDENCE = 8
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

    def __post_init__(self) -> None:
        """Start a new record, leaving the original user message untouched."""
        self.report = {
            "id": uuid.uuid4().hex,
            "thread": self.session.thread_id,
            "goal": self.goal[:4000],
            "phase": "define",
            "status": "running",
            "criteria": [],
            "checkpoints": [],
            "tools": [],
        }
        self.session.prime_report = deepcopy(self.report)

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
            self.session.prime_report = deepcopy(self.report)

    def owns(self, config: Mapping[str, Any]) -> bool:
        """Reject inherited child/subgraph bindings and different conversation threads."""
        values = config.get("configurable", {})
        return (
            self.is_current()
            and values.get("thread_id") == self.report["thread"]
            and "|" not in str(values.get("checkpoint_ns", ""))
        )

    def observe(self, call_id: str, name: str, status: str) -> None:
        """Record a tool's actual returned status, not a model's assertion of success."""
        with self.lock:
            self.report["tools"].append(
                {"id": call_id, "name": name, "status": status, "phase": self.report["phase"]}
            )
            del self.report["tools"][:-MAX_TOOLS]
            self.publish()

    def checkpoint(
        self,
        phase: Phase,
        summary: str,
        criteria: list[str] | None = None,
        evidence: list[str] | None = None,
        unverified_reason: str | None = None,
    ) -> Record:
        """Advance in order; evidence references must resolve to this run's real results."""
        text(summary)
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
            if phase == "define":
                self.report["criteria"] = criteria
            self.report["phase"] = PHASES[min(len(records), len(PHASES) - 1)]
            self.publish()
            return deepcopy(record)

    def finish(self, outcome: str) -> None:
        """Cancellation/restart pauses work; missing milestones cannot become a success claim."""
        with self.lock:
            records = self.report["checkpoints"]
            if outcome != "complete":
                status = "error" if outcome == "error" else "paused"
            elif len(records) != len(PHASES):
                status = "incomplete"
            else:
                status = (
                    "delivered_with_limits"
                    if any(r["unverified_reason"] for r in records)
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
    return "\n".join(lines)


def prime_payload(session: Any) -> Record | None:  # noqa: ANN401 - Real and minimal session proxies.
    """Save a mode and its thread-bound checkpoint snapshot with normal session metadata."""
    report = getattr(session, "prime_report", None)
    if not isinstance(report, dict) or report.get("thread") != getattr(session, "thread_id", None):
        report = None
    enabled = getattr(session, "prime_enabled", False) is True
    return {"enabled": enabled, "report": deepcopy(report)} if enabled or report else None


def restore_prime(session: Any, saved: object) -> None:  # noqa: ANN401 - Session proxy boundary.
    """Old sessions default off; interrupted records stay paused with no automatic replay."""
    session.prime_enabled = False
    session.prime_report = None
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
    if report.get("status") == "running":
        session.prime_report["status"] = "paused"
