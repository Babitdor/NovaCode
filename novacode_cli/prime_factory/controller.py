"""Durable host decisions, usage reservations and independent worker attempts."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from novacode_cli.states.Session import SessionState

from novacode_cli.prime_factory import DEFAULT_OUTPUT_LIMIT, ROLES, VERSION
from novacode_cli.prime_factory.state import bounded_text, check_definition, digest, plan_definition
from novacode_cli.prime_factory.store import FactoryError, FactoryStore
from novacode_cli.prime_memory import project_context, safe_text

ACTIVE = {"running", "starting", "uncertain"}
MAX_ACTIVE_SECONDS = 86400
MAX_SPECIALISTS = 3


def storage_path(root: Path) -> Path:
    """Share state across worktrees without writing into a tracked checkout."""
    context = project_context(str(root))
    return Path(context.get("common_dir") or root / ".nova") / "prime_factory" / "runs.sqlite3"


class FactoryController:
    """Only host code can approve stages, certify checks or authorize publication."""

    def __init__(
        self, store: FactoryStore, record: dict[str, Any], session: SessionState | None = None
    ) -> None:
        """Bind one optimistic durable owner and its originating session."""
        self.store, self.record, self.session = store, record, session
        self.lock = asyncio.Lock()
        self.handles: dict[str, Any] = {}
        self.events: asyncio.Queue[Any] = asyncio.Queue()
        self._clock = time.monotonic()
        self._active = record["status"] == "running"
        self._storage_failed = False
        self._waiting: set[str] = set()
        self._approval_epoch = 0
        self._pending_approvals: set[asyncio.Future[Any]] = set()

    @classmethod
    def create(
        cls,
        root: Path,
        goal: str,
        thread: str,
        routes: dict[str, Any],
        *,
        session: SessionState | None = None,
        store: FactoryStore | None = None,
    ) -> FactoryController:
        """Persist intent before the first model call or workspace mutation."""
        record = {
            "version": VERSION,
            "factory": True,
            "id": uuid.uuid4().hex,
            "thread": thread,
            "root": str(root.resolve()),
            "goal": bounded_text(goal),
            "stage": "requirements",
            "phase": "requirements",
            "status": "running",
            "criteria": [],
            "tasks": [],
            "sources": [],
            "checks": [],
            "attempts": [],
            "approvals": {},
            "blockers": [],
            "routes": routes,
            "output_token_limit": DEFAULT_OUTPUT_LIMIT,
            "reservations": {},
            "budget": {"tokens": 500_000, "active_seconds": 3600.0},
            "usage": {"tokens": 0, "active_seconds": 0.0, "by_role": {}},
            "candidate": None,
            "workspace": None,
            "isolation": "worktree",
            "baseline_selection": None,
            "publication": None,
        }
        controller = cls(store or FactoryStore(storage_path(root)), record, session)
        controller.save()
        return controller

    def tick(self) -> None:
        """Count active elapsed time once across all workers, never per worker."""
        with self.store.lock:
            now = time.monotonic()
            workers = {a["id"] for a in self.record["attempts"] if a["status"] in ACTIVE}
            all_waiting = bool(self._waiting) and workers <= self._waiting
            if self._active and not all_waiting:
                self.record["usage"]["active_seconds"] += max(0, now - self._clock)
            self._clock = now

    def begin_wait(self, key: str) -> None:
        """Exclude human wait time only while every active worker is waiting."""
        with self.store.lock:
            self.tick()
            self._waiting.add(key)

    def end_wait(self, key: str) -> None:
        """Finish a host-owned wait without charging the preceding idle interval."""
        with self.store.lock:
            self.tick()
            self._waiting.discard(key)

    def save(self) -> None:
        """Fail closed on storage errors; no caller may dispatch after a failed save."""
        with self.store.lock:
            if self._storage_failed:
                msg = "Factory storage failed; further mutations are blocked"
                raise FactoryError(msg)
            self.tick()
            try:
                self.store.save(self.record)
            except Exception as exc:
                self._storage_failed = True
                if self.record["status"] not in {"cancelled", "published"}:
                    self.record["status"] = "paused"
                self._active = False
                msg = "Factory storage failed; further mutations are blocked"
                raise FactoryError(msg) from exc
            if (
                self.session is not None
                and getattr(self.session, "thread_id", self.record["thread"])
                == self.record["thread"]
            ):
                self.session.prime_report = self.progress()

    def progress(self) -> dict[str, Any]:
        """Return a detached dashboard snapshot without raw worker transcripts."""
        snapshot = {
            key: deepcopy(value)
            for key, value in self.record.items()
            if key not in {"candidate", "attempts", "checks", "sources", "publication"}
        }
        candidate = self.record.get("candidate")
        snapshot["candidate"] = (
            {
                key: candidate[key]
                for key in ("id", "revision", "branch", "workspace")
                if key in candidate
            }
            if candidate
            else None
        )
        snapshot["attempts"] = [
            {
                key: deepcopy(value)
                for key, value in attempt.items()
                if key not in {"result", "output", "recovery"}
            }
            for attempt in self.record["attempts"][-64:]
        ]
        snapshot["checks"] = [
            {
                **{key: deepcopy(value) for key, value in check.items() if key != "evidence"},
                "evidence_count": len(check["evidence"]),
                "evidence": [
                    {key: value for key, value in evidence.items() if key != "output"}
                    for evidence in check["evidence"][-1:]
                ],
            }
            for check in self.record["checks"]
        ]
        snapshot["sources"] = [
            {
                "id": source["id"],
                "content": {
                    key: value
                    for key, value in source["content"].items()
                    if key not in {"excerpt", "decisions"}
                },
            }
            for source in self.record["sources"]
        ]
        publication = self.record.get("publication")
        snapshot["publication"] = (
            {
                key: publication[key]
                for key in ("id", "status", "url", "destination", "branch", "base")
                if key in publication
            }
            if publication
            else None
        )
        return snapshot

    def _invalidate_approvals(self) -> None:
        self._approval_epoch += 1
        for future in tuple(self._pending_approvals):

            def reject(target: asyncio.Future[Any] = future) -> None:
                if not target.done():
                    target.set_result({"response": {"approved": False}})

            if not future.get_loop().is_closed():
                future.get_loop().call_soon_threadsafe(reject)

    def request_pause(self) -> None:
        """Fence new work immediately; the driver stops owned workers at its next boundary."""
        with self.store.lock:
            self.tick()
            self._active = False
            self._invalidate_approvals()
            if self.record["status"] not in {"cancelled", "published"}:
                self.record["status"] = "paused"
            if not self._storage_failed:
                self.save()

    def request_cancel(self) -> None:
        """Preserve unfinished branches and prevent subsequent resume dispatch."""
        with self.store.lock:
            if self.record["status"] == "published":
                msg = "Published runs cannot be cancelled"
                raise FactoryError(msg)
            self.tick()
            self._active = False
            self._invalidate_approvals()
            self.record["status"] = "cancelled"
            if not self._storage_failed:
                self.save()

    def set_budget(self, tokens: int, active_seconds: float) -> None:
        """A human budget change invalidates plan and delivery approval."""
        if type(tokens) is not int or tokens <= 0 or not 0 < active_seconds <= MAX_ACTIVE_SECONDS:
            msg = "Budget requires positive tokens and at most 1440 active minutes"
            raise FactoryError(msg)
        self.record["budget"] = {"tokens": tokens, "active_seconds": active_seconds}
        self.record["approvals"].clear()
        self.request_pause()

    def ensure_active(self) -> None:
        """Check shared limits immediately before every dispatch and mutation."""
        if (
            self.session is not None
            and getattr(self.session, "thread_id", self.record["thread"]) != self.record["thread"]
        ):
            self.request_pause()
            message = "Conversation changed; the factory is paused until explicitly resumed"
            raise FactoryError(message)
        self.tick()
        if self._storage_failed or self.record["status"] != "running":
            msg = "Factory is suspended; explicit resume is required"
            raise FactoryError(msg)
        usage, budget = self.record["usage"], self.record["budget"]
        reserved = sum(r["tokens"] for r in self.record["reservations"].values())
        if usage["tokens"] + reserved >= budget["tokens"] or (
            usage["active_seconds"] >= budget["active_seconds"]
        ):
            self.record["blockers"] = ["Shared factory budget exhausted"]
            self.request_pause()
            msg = "Shared factory budget exhausted"
            raise FactoryError(msg)

    async def reserve(self, attempt: str, call: str, tokens: int) -> dict[str, Any]:
        """Reserve conservative capacity before allowing any provider request."""
        async with self.lock:
            self.ensure_attempt(attempt)
            self.ensure_active()
            if type(tokens) is not int or tokens <= 0 or not call:
                msg = "Invalid model reservation"
                raise FactoryError(msg)
            key = attempt + ":" + call
            if key in self.record["reservations"]:
                msg = "Duplicate model reservation"
                raise FactoryError(msg)
            reserved = sum(r["tokens"] for r in self.record["reservations"].values())
            if self.record["usage"]["tokens"] + reserved + tokens > self.record["budget"]["tokens"]:
                self.record["blockers"] = ["Insufficient capacity for the next model call"]
                self.request_pause()
                raise FactoryError(self.record["blockers"][0])
            self.record["reservations"][key] = {"tokens": tokens, "attempt": attempt}
            self.save()
            return {"reservation": key}

    async def reconcile(self, attempt: str, key: str, actual: int | None) -> None:
        """Charge unknown/failed calls conservatively; never silently reclaim uncertain usage."""
        async with self.lock:
            worker = self.ensure_attempt(attempt)
            reservation = self.record["reservations"].get(key)
            if not reservation or reservation["attempt"] != attempt:
                msg = "Foreign or already reconciled model reservation"
                raise FactoryError(msg)
            if actual is not None and (type(actual) is not int or actual < 0):
                msg = "Invalid provider usage"
                raise FactoryError(msg)
            amount = reservation["tokens"] if actual is None else actual
            usage = self.record["usage"]
            usage["tokens"] += amount
            role = usage["by_role"].setdefault(worker["role"], {"tokens": 0, "estimated_calls": 0})
            role["tokens"] += amount
            role["estimated_calls"] += actual is None
            del self.record["reservations"][key]
            self.save()
            if usage["tokens"] >= self.record["budget"]["tokens"]:
                self.record["blockers"] = ["Shared factory budget exhausted"]
                self.request_pause()

    def ensure_attempt(self, identity: str) -> dict[str, Any]:
        """Accept events only through the host-bound current attempt identity."""
        found = next((a for a in self.record["attempts"] if a["id"] == identity), None)
        if found is None or found["status"] not in ACTIVE:
            msg = "Unknown, stale or completed worker attempt"
            raise FactoryError(msg)
        return found

    def start_attempt(self, role: str, task: str, cwd: Path) -> dict[str, Any]:
        """Persist trusted identity before allocating a process."""
        self.ensure_active()
        if (
            role not in ROLES
            or sum(a["status"] in ACTIVE for a in self.record["attempts"]) >= MAX_SPECIALISTS
        ):
            msg = "Factory specialist limit reached or invalid role"
            raise FactoryError(msg)
        if role in {"developer", "test_engineer"} and self.record["stage"] == "implementation":
            self.require_plan()
        elif role == "developer":
            msg = "Implementation cannot begin before plan approval"
            raise FactoryError(msg)
        attempt = {
            "id": uuid.uuid4().hex,
            "run": self.record["id"],
            "role": role,
            "task": task,
            "cwd": str(cwd.resolve()),
            "status": "starting",
            "route": self.record["routes"][role],
            "recovery": None,
        }
        import psutil

        isolation = self.record["isolation"]
        container = isinstance(isolation, dict) and not task.startswith("command-")
        attempt["recovery"] = {
            "kind": "factory_start",
            "attempt": attempt["id"],
            "since": time.time(),
            "owner": psutil.Process().username(),
            "container": container,
            "name": "nova-team-" + attempt["id"],
            "labels": {
                "nova.agent": attempt["id"],
                "nova.team": self.record["id"],
                "nova.root": self.record["id"],
                "nova.project": self.record["root"],
                "nova.attempt": attempt["id"],
            },
        }
        self.record["attempts"].append(attempt)
        self.save()
        return attempt

    def require_plan(self) -> None:
        """Freeze scope, environment, models, budgets and acceptance after human review."""
        approval = self.record["approvals"].get("plan")
        if approval != digest(plan_definition(self.record)):
            msg = "The current plan requires human approval"
            raise FactoryError(msg)

    def waive(self, target: str, reason: str) -> None:
        """Record an explicit human exception for one check or finding on one candidate."""
        candidate = self.record.get("candidate") or {}
        known = {check["id"] for check in self.record["checks"]} | {
            digest(finding) for finding in self.record.get("review_findings", [])
        }
        if (
            self.record["status"] in {"cancelled", "published"}
            or not candidate.get("id")
            or target not in known
        ):
            message = "Waivers require a known check or finding on the current unfinished candidate"
            raise FactoryError(message)
        note = bounded_text(reason, 2000)
        self.request_pause()
        self.record.setdefault("waivers", {})[target] = {
            "candidate": candidate["id"],
            "reason": note,
        }
        self.save()

    def waived(self, target: str, candidate: str) -> bool:
        """A candidate change expires human exceptions without weakening acceptance definitions."""
        return (self.record.get("waivers", {}).get(target) or {}).get("candidate") == candidate

    def approve(self, kind: str, identity: str) -> None:
        """Called only by the owning UI response handler, never exposed as a model tool."""
        if self.record["status"] not in {"running", "waiting_approval"}:
            msg = "Suspended or terminal runs cannot accept an expired approval"
            raise FactoryError(msg)
        expected = (
            digest(plan_definition(self.record))
            if kind == "plan"
            else (self.record.get("publication") or {}).get("id")
        )
        if kind not in {"plan", "delivery"} or not expected or expected != identity:
            msg = "Approval does not match the current plan or publication intent"
            raise FactoryError(msg)
        self.record["approvals"][kind] = identity
        self.save()

    async def stop_workers(self) -> None:
        """Keep uncertain workers fenced until termination is proven."""
        for key, handle in list(self.handles.items()):
            try:
                stopped = await handle.stop()
            except Exception:  # noqa: BLE001 - One uncertain worker cannot prevent cleanup of peers.
                stopped = False
            attempt = next((a for a in self.record["attempts"] if a["id"] == key), None)
            if attempt is not None:
                attempt["status"] = "stopped" if stopped else "uncertain"
            if stopped:
                self.handles.pop(key, None)
        if not self._storage_failed:
            self.save()

    async def resume(self) -> None:
        """Reconcile old workers and reservations before explicitly restarting dispatch."""
        if self.record["status"] in {"cancelled", "published"}:
            msg = "Cancelled or published runs cannot be resumed"
            raise FactoryError(msg)
        from novacode_cli.teams.recovery import RecoveryHandle

        for attempt in self.record["attempts"]:
            if attempt["status"] in ACTIVE:
                recovery = attempt.get("recovery") or {}
                if recovery.get("kind") == "factory_start":
                    from novacode_cli.prime_factory.recovery import reconcile_start

                    if not await reconcile_start(recovery):
                        msg = "Worker startup remains uncertain; no work was replayed"
                        raise FactoryError(msg)
                    attempt["status"] = "stopped"
                    continue
                handle = self.handles.get(attempt["id"]) or RecoveryHandle(
                    attempt.get("recovery") or {}
                )
                if not await handle.stop():
                    msg = "An uncertain worker still owns the workspace"
                    raise FactoryError(msg)
                attempt["status"] = "stopped"
        for key, reservation in list(self.record["reservations"].items()):
            self.record["usage"]["tokens"] += reservation["tokens"]
            attempt = next(
                (a for a in self.record["attempts"] if a["id"] == reservation["attempt"]), None
            )
            role_name = attempt["role"] if attempt else "unknown"
            role = self.record["usage"]["by_role"].setdefault(
                role_name, {"tokens": 0, "estimated_calls": 0}
            )
            role["tokens"] += reservation["tokens"]
            role["estimated_calls"] += 1
            del self.record["reservations"][key]
        self.record["approvals"].pop("delivery", None)
        self.record["blockers"] = []
        self.record["status"] = "running"
        self._waiting.clear()
        self._invalidate_approvals()
        self._clock = time.monotonic()
        self._active = True
        self.save()
        self.ensure_active()

    async def wait_approval(self, kind: str, details: dict[str, Any], identity: str) -> bool:
        """Yield an ordinary routed plan approval, mandatory even with tool auto approval."""
        from novacode_cli import ui_events as ev

        epoch = self._approval_epoch
        wait_key = "approval:" + kind
        self.begin_wait(wait_key)
        self.record["status"] = "waiting_approval"
        self.save()
        future = asyncio.get_running_loop().create_future()
        self._pending_approvals.add(future)
        await self.events.put(
            ev.InterruptRequest(
                "plan",
                {
                    "plan": json.dumps(details, ensure_ascii=False, indent=2),
                    "factory": True,
                    "requires_owner_approval": True,
                    "run_id": self.record["id"],
                    "continuation_id": self.record["id"],
                    "approval_kind": kind,
                    "approval_digest": identity,
                },
                future,
            )
        )
        try:
            result = await future
        finally:
            self._pending_approvals.discard(future)
            self.end_wait(wait_key)
        with self.store.lock:
            if epoch != self._approval_epoch or self.record["status"] != "waiting_approval":
                return False
            response = result.get("response", {}) if isinstance(result, dict) else {}
            approved = isinstance(response, dict) and response.get("approved") is True
            if not approved:
                self.record["blockers"] = [
                    f"Human {kind} approval required; resume {self.record['id']}"
                ]
                self.request_pause()
                return False
            self.approve(kind, identity)
            self.record["status"] = "running"
            self._clock = time.monotonic()
            self._active = True
            self.save()
            return True

    def check_result(
        self,
        check: dict[str, Any],
        candidate: str,
        environment: str,
        argv: list[str],
        code: int | None,
        output: str,
        *,
        baseline: bool = False,
    ) -> None:
        """Only host-observed exact executions can satisfy frozen acceptance checks."""
        self.require_plan()
        if check not in self.record["checks"] or argv != check["argv"]:
            msg = "Acceptance definition changed"
            raise FactoryError(msg)
        evidence = {
            "id": uuid.uuid4().hex,
            "check": check["id"],
            "candidate": candidate,
            "environment": environment,
            "definition": digest(check_definition(check)),
            "exit_code": code,
            "output": safe_text(output, 12000),
            "observed_at": time.time(),
            "baseline": baseline,
        }
        check["evidence"].append(evidence)
        if baseline:
            check["baseline_failed"] = (
                code is not None
                and code not in {0, 126, 127, 9009}
                and all(term in output for term in check.get("failure_contains", []))
            )
        else:
            passed = code == 0 and all(term in output for term in check.get("contains", []))
            unavailable = code is None or code in {126, 127, 9009}
            check["status"] = "passed" if passed else "unavailable" if unavailable else "failed"
            if not passed:
                check["failures"] += 1
            if check["test_first"] and not check.get("baseline_failed"):
                check["status"] = "unverified"
        self.save()
