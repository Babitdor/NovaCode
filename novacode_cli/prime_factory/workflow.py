"""Supervised development stages and evidence-bound delivery."""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import sys
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

from novacode_cli.prime_factory.research import ResearchLedger, make_fetcher
from novacode_cli.prime_factory.state import (
    bounded_text,
    digest,
    paths,
    plan_definition,
    validate_checks,
    validate_tasks,
)
from novacode_cli.prime_factory.store import FactoryError
from novacode_cli.prime_factory.workspace import FactoryWorkspace, WorkspaceError, _run
from novacode_cli.prime_memory import DEPENDENCIES, fingerprints

if TYPE_CHECKING:
    from novacode_cli.prime_factory.controller import FactoryController

SCHEMAS = {
    "requirements": {
        "criteria": ["concrete acceptance criterion"],
        "dependencies": {"relevant external dependency": "exact installed/locked version"},
        "summary": "requirements and scope",
    },
    "research": {
        "sources": [
            {
                "url": "version-matched official documentation URL",
                "dependency": "name",
                "version": "exact version",
                "excerpt": "literal relevant page excerpt",
                "decisions": ["decision this source supports"],
            }
        ],
        "unnecessary": "reason only if repository-only with no relevant external behavior",
    },
    "architecture": {
        "tasks": [
            {
                "id": "task1",
                "description": "specific change",
                "files": ["file.py"],
                "depends_on": [],
            }
        ],
        "decisions": ["architecture decision"],
        "setup": [["exact", "environment/tool", "setup", "argv"]],
        "destination": {"repository": "owner/repository", "base": "main"},
    },
    "test_engineer": {
        "checks": [
            {
                "argv": ["python", "-m", "pytest", "tests/test_feature.py"],
                "purpose": "behavior verified",
                "criteria": [0],
                "test_first": True,
                "security": False,
                "tasks": ["responsible_task_id"],
                "failure_contains": ["specific assertion failure"],
                "contains": ["specific passing assertion if available"],
            }
        ],
        "test_files": ["tests/test_feature.py"],
    },
    "verification": {
        "findings": ["unresolved correctness finding, empty if none"],
        "limitations": ["unverified behavior"],
    },
    "security": {
        "findings": ["unresolved security finding, empty if none"],
        "limitations": ["unavailable scan or uncovered security behavior"],
    },
}
MAX_CRITERIA = 12
MAX_ITEMS = 32
MAX_SETUP = 16
MAX_ARGUMENTS = 64
MAX_ENV_PACKAGES = 1024
MAX_SPECIALISTS = 3
MAX_REPAIRS = 3


class FactoryWorkflow:
    """Keep model assertions subordinate to host transitions and observed executions."""

    def __init__(self, controller: FactoryController, runner: Any) -> None:  # noqa: ANN401 - Worker boundary.
        """Use the same controller with production workers or offline scripted workers."""
        self.c, self.runner = controller, runner
        self.workspace = FactoryWorkspace(Path(controller.record["root"]))
        self.research = ResearchLedger(Path(controller.record["root"]))

    async def save(self, stage: str | None = None) -> None:
        """Publish durable stage changes and dashboard progress."""
        from novacode_cli import ui_events as ev

        if stage:
            self.c.record["stage"] = self.c.record["phase"] = stage
        await asyncio.to_thread(self.c.save)
        await self.c.events.put(
            ev.ContextMessage(
                f"Prime factory · {self.c.record['stage']} · {self.c.record['status']}",
                event_type="nova_prime_progress",
                record_id=self.c.record["id"],
                data=self.c.progress(),
            )
        )

    async def remember(self) -> None:
        """Connect current task, source, decision and check facts without granting authority."""
        record = self.c.record
        index = self.research.index

        def persist() -> None:
            sources = tuple(source["id"] for source in record["sources"])
            for task in record["tasks"]:
                inputs = {name: index.fingerprint(name) for name in task["files"]}
                index.put(
                    "task",
                    record["id"] + ":" + task["id"],
                    {
                        "goal": record["goal"],
                        "description": task["description"],
                        "status": task["status"],
                    },
                    sources=sources,
                    fingerprints=inputs,
                    links=(
                        *(("owns", name) for name in task["files"]),
                        *(("depends_on", record["id"] + ":" + key) for key in task["depends_on"]),
                    ),
                )
            for number, decision in enumerate(record.get("decisions", [])):
                index.put(
                    "adr",
                    record["id"] + ":" + str(number),
                    {"decision": decision},
                    sources=sources,
                    links=tuple(
                        ("supports", record["id"] + ":" + task["id"]) for task in record["tasks"]
                    ),
                )
            for dependency, version in record.get("research_requirements", {}).items():
                index.put(
                    "dependency",
                    dependency,
                    {"version": version},
                    sources=sources,
                    links=tuple(("documented_by", key) for key in sources),
                )
            for check in record["checks"]:
                files = set(record.get("test_files", [])) | {
                    name
                    for task in record["tasks"]
                    if task["id"] in check["tasks"]
                    for name in task["files"]
                }
                index.put(
                    "check",
                    record["id"] + ":" + check["id"],
                    {
                        "purpose": check["purpose"],
                        "status": check["status"],
                        "candidate": (record.get("candidate") or {}).get("id"),
                        "evidence_ids": [evidence["id"] for evidence in check["evidence"]],
                    },
                    sources=sources,
                    fingerprints={name: index.fingerprint(name) for name in files},
                    links=tuple(("verifies", record["id"] + ":" + key) for key in check["tasks"]),
                )

        await asyncio.to_thread(persist)

    async def role(self, role: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Provide bounded repository facts and explicit structured role contracts."""
        root = (
            self.workspace.integration.path
            if self.workspace.integration
            else Path(self.c.record["root"])
        )
        knowledge = await asyncio.to_thread(
            self.research.index.retrieve, self.c.record["goal"], limit=5
        )
        return await self.runner.run(
            role,
            role,
            root,
            {
                "schema": SCHEMAS[role],
                "plan": plan_definition(self.c.record),
                "environment": self.research.environment,
                "untrusted_context": {"knowledge": knowledge, **(context or {})},
            },
        )

    async def fetch_sources(self, sources: list[dict[str, Any]]) -> None:
        """Every source must be actually retrieved under the originating URL policy."""
        loop = asyncio.get_running_loop()
        for source in sources:
            url = bounded_text(source.get("url"), 2000)
            version = bounded_text(source.get("version"), 100)
            dependency = bounded_text(source.get("dependency"), 100)

            # A redirect is another URL decision, including an unrelated host.
            def authorized(target: str) -> bool:
                return asyncio.run_coroutine_threadsafe(
                    self.runner.authorize("fetch_url", {"url": target}),
                    loop,
                ).result(timeout=300)

            fetcher = make_fetcher(version, authorize=authorized, dependency=dependency)
            observed = await asyncio.to_thread(fetcher, url)
            if observed.status != HTTPStatus.OK or observed.version != version:
                message = "Required version-matched documentation is unavailable"
                raise FactoryError(message)
            analysis = await self.runner.run(
                "research",
                "source-" + digest(url)[:16],
                Path(self.c.record["root"]),
                {
                    "untrusted_document": observed.content[:64000],
                    "url": observed.url,
                    "goal": self.c.record["goal"],
                    "schema": {
                        "excerpt": "literal relevant excerpt from this retrieved document",
                        "decisions": ["specific implementation decision this source supports"],
                    },
                },
            )
            result = await asyncio.to_thread(
                self.research.fetch,
                url,
                dependency=dependency,
                version=version,
                excerpt=bounded_text(analysis.get("excerpt")),
                decisions=[bounded_text(d) for d in analysis.get("decisions", [])],
                fetcher=lambda _url, receipt=observed: receipt,
            )
            self.c.record["sources"] = [
                s for s in self.c.record["sources"] if s["id"] != result["id"]
            ] + [result]
            await self.save()

    async def planning(self) -> None:  # noqa: PLR0912, PLR0915 - Ordered plan validation before approval.
        """Define requirements, research, architecture and checks before human approval."""
        record = self.c.record
        if not record["criteria"]:
            result = await self.role("requirements")
            criteria = result.get("criteria")
            if not isinstance(criteria, list) or not 1 <= len(criteria) <= MAX_CRITERIA:
                msg = "Requirements must define 1-12 acceptance criteria"
                raise FactoryError(msg)
            record["criteria"] = [bounded_text(c, 500) for c in criteria]
            dependencies = result.get("dependencies")
            if not isinstance(dependencies, dict) or len(dependencies) > MAX_ITEMS:
                msg = "Declare relevant dependency versions or an empty mapping"
                raise FactoryError(msg)
            record["research_requirements"] = {
                bounded_text(k, 100): bounded_text(v, 100) for k, v in dependencies.items()
            }
            await self.save("research")
        if not record["sources"] and not record.get("research_exemption"):
            result = await self.role("research")
            sources = result.get("sources", [])
            if not isinstance(sources, list) or len(sources) > MAX_ITEMS:
                msg = "Research requires a bounded source list"
                raise FactoryError(msg)
            if sources:
                await self.fetch_sources(sources)
            elif not record["research_requirements"]:
                reason = bounded_text(result.get("unnecessary"))
                await asyncio.to_thread(self.research.exempt, reason)
                record["research_exemption"] = reason
            await self.save()
        else:
            # New ledger instances cannot trust a saved successful fetch receipt.
            loop = asyncio.get_running_loop()
            for source in record["sources"]:
                content = source["content"]

                def authorized(target: str) -> bool:
                    return asyncio.run_coroutine_threadsafe(
                        self.runner.authorize("fetch_url", {"url": target}),
                        loop,
                    ).result(timeout=300)

                fetcher = make_fetcher(
                    content["dependency_version"],
                    authorize=authorized,
                    dependency=content["dependency"],
                )
                await asyncio.to_thread(self.research.revalidate_source, source, fetcher)
            if record.get("research_exemption"):
                await asyncio.to_thread(self.research.exempt, record["research_exemption"])
        blockers = await asyncio.to_thread(self.research.validate, record["research_requirements"])
        if blockers:
            raise FactoryError("; ".join(blockers))
        if not record["tasks"]:
            await self.save("architecture")
            result = await self.role("architecture")
            record["tasks"] = validate_tasks(result.get("tasks"))
            record["decisions"] = [bounded_text(d) for d in result.get("decisions", [])]
            setup = result.get("setup", [])
            if (
                not isinstance(setup, list)
                or len(setup) > MAX_SETUP
                or any(
                    not isinstance(argv, list) or not argv or len(argv) > MAX_ARGUMENTS
                    for argv in setup
                )
            ):
                msg = "Setup must contain exact bounded command arrays"
                raise FactoryError(msg)
            record["setup"] = [[bounded_text(arg, 2000) for arg in argv] for argv in setup]
            destination = result.get("destination", {})
            if not isinstance(destination, dict):
                msg = "Invalid delivery destination"
                raise FactoryError(msg)
            record["destination"] = destination
            await self.save()
        if not record["checks"]:
            result = await self.role("test_engineer")
            record["checks"] = validate_checks(
                result.get("checks"),
                record["criteria"],
                {task["id"] for task in record["tasks"]},
            )
            if any(
                check["test_first"] and not check["failure_contains"] for check in record["checks"]
            ):
                message = "Test-first checks require specific assertion failure markers"
                raise FactoryError(message)
            record["test_files"] = paths(result.get("test_files"))
            owned = {f.casefold() for task in record["tasks"] for f in task["files"]}
            if any(file.casefold() in owned for file in record["test_files"]):
                msg = "Developer ownership cannot include frozen acceptance test files"
                raise FactoryError(msg)
            await self.save()
        inputs = sorted(
            {f for task in record["tasks"] for f in task["files"]}
            | set(record["test_files"])
            | {name for name in DEPENDENCIES if (Path(record["root"]) / name).is_file()}
        )
        record["knowledge_coverage"] = await self.research.index.update(inputs[:256])
        await self.remember()
        if not record.get("baseline"):
            root = Path(record["root"])
            record["baseline"] = await asyncio.to_thread(_run, root, ["rev-parse", "HEAD"])
            dirty = await asyncio.to_thread(_run, root, ["status", "--porcelain"])
            record["baseline_selection"] = {
                "revision": record["baseline"],
                "existing_changes": dirty,
                "selection": "committed",
                "meaning": "Existing checkout changes are excluded",
            }
            await self.save()
        await self.save("plan_approval")
        identity = digest(plan_definition(record))
        if record["approvals"].get("plan") != identity and not await self.c.wait_approval(
            "plan",
            plan_definition(record),
            identity,
        ):
            return
        await self.save("implementation")

    async def commit(self, path: Path, files: list[str], message: str) -> str:
        """Commit only leased files through the ordinary execution policy."""
        for argv in (["git", "add", "--", *files], ["git", "commit", "-m", message]):
            code, _, error = await self.runner.execute(argv, path)
            if code != 0:
                raise FactoryError(error or "No reviewable changes were committed")
        return await asyncio.to_thread(_run, path, ["rev-parse", "HEAD"])

    async def candidate(self) -> dict[str, Any]:
        """Recompute evidence identity from integrated revision, dependencies and runtime."""
        root = await asyncio.to_thread(self.workspace._root)
        await self.validate_execution_environment()
        dependencies = tuple(name for name in DEPENDENCIES if (root / name).is_file())
        environment = {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "setup": digest(self.c.record["setup"]),
        }
        environment_path = self.c.record.get("environment_path")
        if environment_path:
            envroot = Path(environment_path)
            metadata = await asyncio.to_thread(
                lambda: (
                    sorted(envroot.glob("Lib/site-packages/*.dist-info/METADATA"))
                    + sorted(envroot.glob("lib/python*/site-packages/*.dist-info/METADATA"))
                )
            )
            if len(metadata) > MAX_ENV_PACKAGES:
                msg = "Isolated environment exceeds metadata bound"
                raise FactoryError(msg)

            def metadata_hashes() -> dict[str, str]:
                result = {}
                for path in metadata:
                    with path.open("rb") as stream:
                        result[str(path.relative_to(envroot))] = hashlib.file_digest(
                            stream, "sha256"
                        ).hexdigest()
                return result

            environment["dependencies"] = digest(await asyncio.to_thread(metadata_hashes))
            environment["executable"] = str(
                envroot / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            )
        return await asyncio.to_thread(
            self.workspace.candidate,
            dependencies=dependencies,
            environment=environment,
        )

    async def checks(self, *, baseline: bool = False) -> list[dict[str, Any]]:
        """Execute definitions unchanged against one integrated candidate."""
        await self.validate_research_environment()
        candidate = await self.candidate()
        record = self.c.record
        frozen = record.get("test_fingerprints")
        if (
            frozen is not None
            and fingerprints(candidate["workspace"], record["test_files"]) != frozen
        ):
            msg = "Frozen acceptance test files changed"
            raise FactoryError(msg)
        failed = []
        for check in record["checks"]:
            self.c.require_plan()
            if baseline and not check["test_first"]:
                continue
            if check.get("repairs", 0) > MAX_REPAIRS:
                msg = "Acceptance check reached the three-round repair limit"
                raise FactoryError(msg)
            code, output, error = await self.runner.execute(
                check["argv"], Path(candidate["workspace"])
            )
            after = await self.candidate()
            if after["id"] != candidate["id"]:
                msg = "Check execution changed the integrated candidate"
                raise FactoryError(msg)
            self.c.check_result(
                check,
                candidate["id"],
                digest(candidate["environment"]),
                check["argv"],
                code,
                output + error,
                baseline=baseline,
            )
            if (
                not baseline
                and check["status"] != "passed"
                and self.c.waived(check["id"], candidate["id"])
            ):
                check["status"] = "waived"
            if not baseline and check["status"] not in {"passed", "waived"}:
                failed.append(check)
        record["candidate"] = candidate
        await self.save()
        return failed

    async def develop_task(self, task: dict[str, Any], *, repair: bool = False) -> None:
        """Workers return changes; the host commits and integrates in dependency order."""
        self.c.ensure_active()
        self.c.require_plan()
        record = self.c.record
        key = task["id"] if not repair else task["id"] + "-repair-" + str(len(task["attempts"]))
        pending = [
            key
            for key in task["attempts"]
            if self.workspace.tasks.get(key, {}).get("status") == "active"
        ]
        if pending:
            key = pending[-1]
        workspace = self.workspace.tasks.get(key)
        if workspace is None:
            workspace = await asyncio.to_thread(
                self.workspace.task,
                key,
                files=task["files"],
                depends_on=tuple(task["depends_on"]) if not repair else (),
            )
        if workspace["status"] != "active":
            msg = "Unfinished task integration requires reconciliation"
            raise FactoryError(msg)
        record["workspace"] = self.workspace.snapshot()
        task["status"] = "running"
        if key not in task["attempts"]:
            task["attempts"].append(key)
        await self.save()
        path = Path(workspace["path"])
        current = await asyncio.to_thread(_run, path, ["rev-parse", "HEAD"])
        if current != workspace["base"]:
            # A durable task branch can outlive a crash between commit and integration.
            self.c.ensure_active()
            await asyncio.to_thread(self.workspace.integrate, key, current)
            task["status"] = "integrated"
            record["workspace"] = self.workspace.snapshot()
            await self.save()
            return
        result = await self.runner.run(
            "developer",
            key,
            path,
            {
                "task": task["description"],
                "plan": plan_definition(record),
                "failures": [c for c in record["checks"] if c["status"] != "passed"],
                "schema": {"summary": "changes made", "limitations": []},
            },
            files=task["files"],
        )
        bounded_text(result.get("summary"))
        revision = await self.commit(Path(workspace["path"]), task["files"], f"Factory: {key}")
        self.c.require_plan()
        self.c.ensure_active()
        await asyncio.to_thread(self.workspace.integrate, key, revision)
        task["status"] = "integrated"
        record["workspace"] = self.workspace.snapshot()
        record["approvals"].pop("delivery", None)
        await self.save()

    async def implement(self) -> None:  # noqa: PLR0912, PLR0915 - Durable implementation boundaries.
        """Write tests before developers and schedule independent tasks in bounded batches."""
        record = self.c.record
        self.c.require_plan()
        self.c.ensure_active()
        if record["workspace"]:
            await asyncio.to_thread(self.workspace.restore, record["workspace"])
        else:
            current = await asyncio.to_thread(_run, Path(record["root"]), ["rev-parse", "HEAD"])
            if current != record["baseline"]:
                record["approvals"].clear()
                msg = "Source baseline changed; a new plan approval is required"
                raise FactoryError(msg)
            record["workspace"] = await asyncio.to_thread(
                self.workspace.prepare,
                record["id"],
                baseline="committed",
            )
            await self.save()
        root = await asyncio.to_thread(self.workspace._root)
        if not record.get("setup_complete"):
            if record["setup"]:
                environment_path = Path(
                    await asyncio.to_thread(
                        _run,
                        root,
                        ["rev-parse", "--path-format=absolute", "--git-path", "nova-factory-env"],
                    )
                )
                code, _, error = await self.runner.execute(
                    [sys.executable, "-m", "venv", str(environment_path)],
                    root,
                )
                if code != 0:
                    raise FactoryError(error or "Could not provision the isolated environment")
                record["environment_path"] = str(environment_path)
                await self.save()
                for argv in record["setup"]:
                    supported = (
                        argv[:4] == ["python", "-m", "pip", "install"]
                        or argv[:2] == ["pip", "install"]
                        or argv[:3] == ["uv", "pip", "install"]
                        or argv[:2] in (["uv", "sync"], ["npm", "ci"], ["npm", "install"])
                    )
                    if not supported:
                        message = (
                            "Setup supports only worktree-local pip, "
                            "uv sync/install or npm installs"
                        )
                        raise FactoryError(message)
                    if any(
                        arg
                        in {
                            "--global",
                            "-g",
                            "--user",
                            "--system",
                            "--prefix",
                            "--target",
                            "--python",
                            "-p",
                            "--project",
                            "--directory",
                            "-C",
                            "--root",
                        }
                        or arg.startswith(
                            (
                                "--prefix=",
                                "--target=",
                                "--python=",
                                "--project=",
                                "--directory=",
                                "--root=",
                            )
                        )
                        for arg in argv
                    ):
                        msg = "Approved setup must install into the worktree environment"
                        raise FactoryError(msg)
                    code, _, error = await self.runner.execute(argv, root)
                    if code != 0:
                        raise FactoryError(error or "Isolated environment setup failed")
            record["setup_complete"] = True
            await self.save()
        await self.validate_research_environment()
        if not record.get("test_fingerprints") and record["test_files"]:
            key = "acceptance-tests"
            work = self.workspace.tasks.get(key)
            if work is None:
                work = await asyncio.to_thread(self.workspace.task, key, files=record["test_files"])
            record["workspace"] = self.workspace.snapshot()
            await self.save()
            testpath = Path(work["path"])
            current = await asyncio.to_thread(_run, testpath, ["rev-parse", "HEAD"])
            if current == work["base"]:
                await self.runner.run(
                    "test_engineer",
                    key,
                    testpath,
                    {
                        "instruction": (
                            "Write meaningful acceptance tests from the approved definitions; "
                            "do not change check commands or weaken assertions."
                        ),
                        "plan": plan_definition(record),
                        "schema": {"summary": "tests added"},
                    },
                    files=record["test_files"],
                )
                current = await self.commit(
                    testpath, record["test_files"], "Factory: acceptance tests"
                )
            if work["status"] != "integrated":
                self.c.ensure_active()
                await asyncio.to_thread(self.workspace.integrate, key, current)
            record["test_fingerprints"] = fingerprints(str(root), record["test_files"])
            record["workspace"] = self.workspace.snapshot()
            await self.save()
        if not record.get("baseline_checks_complete"):
            await self.checks(baseline=True)
            if any(c["test_first"] and not c.get("baseline_failed") for c in record["checks"]):
                msg = "Test-first acceptance requires an observed failure before implementation"
                raise FactoryError(msg)
            record["baseline_checks_complete"] = True
            await self.save()
        while any(t["status"] != "integrated" for t in record["tasks"]):
            ready = [
                t
                for t in record["tasks"]
                if t["status"] != "integrated"
                and all(
                    next(d for d in record["tasks"] if d["id"] == key)["status"] == "integrated"
                    for key in t["depends_on"]
                )
            ]
            if not ready:
                msg = "Task graph cannot make progress"
                raise FactoryError(msg)
            batch: list[dict[str, Any]] = []
            leased: set[str] = set()
            for task in ready:
                owned = {f.casefold() for f in task["files"]}
                if not owned & leased and len(batch) < MAX_SPECIALISTS:
                    batch.append(task)
                    leased |= owned
            workers = [asyncio.create_task(self.develop_task(task)) for task in batch]
            try:
                await asyncio.gather(*workers)
            finally:
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
        await self.save("verification")
        self.research = await asyncio.to_thread(ResearchLedger, root)
        inputs = sorted(
            {f for task in record["tasks"] for f in task["files"]} | set(record["test_files"])
        )
        record["knowledge_coverage"] = await self.research.index.update(inputs[:256])
        await self.save()

    async def validate_execution_environment(self) -> None:
        """Require the provisioned interpreter before dispatching development or checks."""
        record = self.c.record
        path = record.get("environment_path")
        if path:
            interpreter = Path(path) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if not await asyncio.to_thread(interpreter.is_file):
                record["approvals"].clear()
                message = "Isolated environment is missing; setup and plan review are required"
                raise FactoryError(message)

    async def validate_research_environment(self) -> None:
        """Revalidate dependency versions for every candidate, including repairs."""
        record = self.c.record
        await self.validate_execution_environment()
        if record["research_requirements"]:
            root = await asyncio.to_thread(self.workspace._root)
            ledger = await asyncio.to_thread(ResearchLedger, root)
            environment = await asyncio.to_thread(
                ledger.inspect_environment,
                record["research_requirements"],
                environment_path=Path(record["environment_path"])
                if record.get("environment_path")
                else None,
            )
            for dependency, version in record["research_requirements"].items():
                observed = [
                    environment[kind].get(dependency)
                    for kind in ("locked_versions", "installed_versions")
                ]
                mismatched = [
                    actual for actual in observed if actual is not None and actual != version
                ]
                if mismatched:
                    record["approvals"].clear()
                    message = (
                        f"Integrated dependency {dependency} changed to {mismatched[0]}; "
                        "version-matched research and a new plan approval are required"
                    )
                    raise FactoryError(message)

    async def verify(self) -> None:
        """Independent reviewers inspect the integrated result; failures get bounded repairs."""
        record = self.c.record
        while True:
            failed = await self.checks()
            if not failed:
                break
            if any(c["status"] == "unavailable" for c in failed):
                msg = "Required check or scanner is unavailable"
                raise FactoryError(msg)
            if any(c.get("repairs", 0) >= MAX_REPAIRS for c in failed):
                msg = "Acceptance check reached the three-round repair limit"
                raise FactoryError(msg)
            await self.save("implementation")
            for check in failed:
                check["repairs"] = check.get("repairs", 0) + 1
            await self.save()
            responsible = {key for check in failed for key in check["tasks"]}
            for task in record["tasks"]:
                if task["id"] in responsible:
                    await self.develop_task(task, repair=True)
            await self.save("verification")
        results = await asyncio.gather(
            self.role(
                "verification", {"candidate": record["candidate"], "checks": record["checks"]}
            ),
            self.role("security", {"candidate": record["candidate"], "checks": record["checks"]}),
        )
        findings: list[str] = []
        for role, result in zip(("verification", "security"), results, strict=True):
            for key in ("findings", "limitations"):
                items = result.get(key)
                if not isinstance(items, list) or len(items) > MAX_ITEMS:
                    msg = "Independent review must explicitly report findings and limitations"
                    raise FactoryError(msg)
                findings.extend(f"{role}: {bounded_text(item)}" for item in items)
        record["review_findings"] = findings
        await self.remember()
        await self.save()
        unresolved = [
            finding
            for finding in findings
            if not self.c.waived(digest(finding), record["candidate"]["id"])
        ]
        if unresolved:
            raise FactoryError(
                "Independent review has unresolved findings: " + "; ".join(unresolved)
            )
        if (await self.candidate())["id"] != record["candidate"]["id"]:
            msg = "Integrated candidate changed during independent review"
            raise FactoryError(msg)
        await self.save("delivery_approval")

    async def delivery(self) -> None:
        """Present complete review evidence before authorizing branch push and PR creation."""
        from novacode_cli.prime_factory.publication import Publisher

        await self.validate_research_environment()
        record = self.c.record
        candidate = await self.candidate()
        if candidate["id"] != record["candidate"]["id"]:
            record["approvals"].pop("delivery", None)
            msg = "Candidate changed; verification must be repeated"
            raise FactoryError(msg)
        if any(
            (
                c["status"] != "passed"
                and not (c["status"] == "waived" and self.c.waived(c["id"], candidate["id"]))
            )
            or c["evidence"][-1]["candidate"] != candidate["id"]
            for c in record["checks"]
        ):
            msg = "Delivery requires current passing observed checks"
            raise FactoryError(msg)

        async def persist(intent: dict[str, Any]) -> None:
            record["publication"] = intent
            await self.save()

        publisher = Publisher(
            Path(candidate["workspace"]),
            persist,
            getattr(self.runner, "publish_execute", self.runner.execute),
        )
        destination = record["destination"]
        base_revision = await self.runner.delivery_base(
            Path(candidate["workspace"]),
            destination.get("repository", ""),
            destination.get("base", "main"),
        )
        candidate["delivery_base_revision"] = base_revision
        candidate["pr_diff"] = await asyncio.to_thread(
            _run,
            Path(candidate["workspace"]),
            [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--binary",
                f"{base_revision}...{candidate['revision']}",
                "--",
            ],
        )
        record["candidate"] = candidate
        intent = publisher.prepare(
            candidate,
            destination=destination.get("repository", ""),
            branch=candidate["branch"],
            base=destination.get("base", "main"),
            title=record["goal"][:200],
            body="Goal: "
            + record["goal"]
            + "\n\nValidation:\n"
            + "\n".join("- " + c["purpose"] + ": " + c["status"] for c in record["checks"])
            + "\n\nHuman exceptions:\n"
            + "\n".join(
                f"- {key}: {waiver['reason']} (unverified; human waiver)"
                for key, waiver in record.get("waivers", {}).items()
                if waiver["candidate"] == candidate["id"]
            ),
        )
        record["publication"] = intent
        await self.save()
        details = {
            "publication": intent,
            "research": record["sources"],
            "checks": record["checks"],
            "security_findings": record["review_findings"],
            "waivers": record.get("waivers", {}),
            "usage": record["usage"],
        }
        if not await self.c.wait_approval("delivery", details, intent["id"]):
            return
        self.c.require_plan()
        await self.save("publication")
        await publisher.publish(intent, approved_intent=record["approvals"]["delivery"])
        record["status"] = "published"
        await self.save("complete")

    async def run(self) -> None:
        """Resume only persisted stages; exceptions suspend rather than inventing success."""
        record = self.c.record
        try:
            if record["stage"] in {"requirements", "research", "architecture", "plan_approval"}:
                await self.planning()
            if record["status"] != "running":
                return
            if record["stage"] == "implementation":
                await self.implement()
            elif record["workspace"]:
                await asyncio.to_thread(self.workspace.restore, record["workspace"])
            if record["stage"] == "verification":
                await self.verify()
            if record["stage"] in {"delivery_approval", "publication"}:
                await self.delivery()
        except (FactoryError, WorkspaceError, OSError, ValueError, KeyError, TypeError) as exc:
            record["blockers"] = [str(exc)]
            self.c.request_pause()
        finally:
            await self.c.stop_workers()
