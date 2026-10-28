"""Project-scoped Prime drafts; no instructions, approvals or executable replay."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from novacode_cli.memory.store import get_durable_store
from novacode_cli.security.prompt_injection import _credential_values, _redact
from novacode_cli.session.history_export import redact

DEPENDENCIES = (
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
)
MAX_INPUTS = 32
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 100_000
MAX_RECALL = 5
MAX_OBSERVED_OUTPUT = 20_000
Record = dict[str, Any]


def session_directory(session: Any) -> str:  # noqa: ANN401 - Session proxies.
    """Use Nova's selected workspace; minimal session proxies may supply it directly."""
    from novacode_cli.config.config import settings

    return str(
        getattr(session, "working_directory", None)
        or getattr(session, "workspace_root", None)
        or settings.get_workspace_root()
    )


def continuation_context(report: Record) -> Record:
    """Carry source context without recursively nesting the complete history of child runs."""
    return {
        key: report.get(key)
        for key in (
            "id",
            "goal",
            "request",
            "criteria",
            "files",
            "dependencies",
            "fingerprints",
            "findings",
            "questions",
            "next_action",
            "cwd",
            "revision",
        )
    }


def safe_text(value: str, limit: int = 2000) -> str:
    """Bound excerpts, redact known credentials, and omit environment assignments."""
    value = redact(_redact(value, _credential_values()))
    value = re.sub(
        r"(?im)^\s*(?:export\s+|set\s+|\$env:)?[\w.-]+\s*=.*$",
        "[assignment omitted]",
        value,
    )
    value = re.sub(
        r"(?i)(\b(?:password|token|secret|api[_-]?key)\b[\s:=]+)\S+",
        r"\1[redacted]",
        value,
    )
    return value[:limit]


def sanitized(value: Any) -> Any:  # noqa: ANN401 - JSON storage boundary.
    """Redact every persisted string, including model-authored findings."""
    if isinstance(value, str):
        return safe_text(value, 4000)
    if isinstance(value, dict):
        return {key: sanitized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    return value


def execution_evidence(
    *,
    command: str,
    cwd: str | None,
    exit_code: int | None,
    output: str,
    completed: bool,
    truncated: bool,
) -> Record:
    """Bound and redact execution artifacts before checkpoint persistence sees them.

    Omitted or redacted text cannot satisfy a literal assertion. The observer never
    decodes model-visible output to reconstruct trusted execution metadata.
    """
    captured = safe_text(output, MAX_OBSERVED_OUTPUT)
    if re.search(r"(?i)\b(?:printenv|os\.environ|GetEnvironmentVariables)\b|\benv:\s*", command):
        captured = "[environment output omitted]"
    saved_command = safe_text(command)
    return {
        "command": saved_command,
        "cwd": cwd,
        "exit_code": exit_code,
        "output": captured,
        "completed": completed,
        "truncated": truncated or captured != output or saved_command != command,
    }


def project_context(directory: str) -> Record:  # noqa: PLR0912 - Git's loose/packed and worktree layouts.
    """Resolve linked worktrees through Git's common directory without executing code."""
    cwd = Path(directory).resolve()
    root, common, gitdir = cwd, None, None
    for candidate in (cwd, *cwd.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            root, gitdir = candidate, marker.resolve()
            break
        if marker.is_file():
            try:
                pointer = marker.read_text(encoding="utf-8").strip()
                if pointer.startswith("gitdir: "):
                    root = candidate
                    gitdir = (candidate / pointer[8:]).resolve()
                    break
            except OSError:
                continue
    revision = None
    if gitdir is not None:
        try:
            link = gitdir / "commondir"
            common = (gitdir / link.read_text().strip()).resolve() if link.is_file() else gitdir
            head = (gitdir / "HEAD").read_text().strip()
            if head.startswith("ref: "):
                ref = head[5:]
                direct = common / ref
                if direct.is_file():
                    revision = direct.read_text().strip()
                else:
                    packed = common / "packed-refs"
                    for line in packed.read_text().splitlines() if packed.is_file() else []:
                        if line.endswith(" " + ref):
                            revision = line.split()[0]
                            break
            else:
                revision = head
        except OSError:
            revision = None
    identity = os.path.normcase(str(common or root))
    return {
        "project": hashlib.sha256(identity.encode()).hexdigest(),
        "root": str(root),
        "cwd": str(cwd),
        "revision": revision,
        "common_dir": str(common) if common else None,
    }


def input_paths(root: str, paths: list[str]) -> list[str]:
    """Normalize declared project inputs, rejecting paths outside this checkout."""
    if len(paths) > MAX_INPUTS:
        msg = "Declare at most 32 input files"
        raise ValueError(msg)
    base = Path(root).resolve()
    result = []
    for name in paths:
        path = (base / name).resolve()
        if not path.is_relative_to(base):
            msg = "Prime inputs must be inside the current project"
            raise ValueError(msg)
        result.append(path.relative_to(base).as_posix())
    return sorted(set(result))


def fingerprints(root: str, paths: list[str]) -> dict[str, str | None]:
    """Hash bounded inputs; missing, unreadable or oversized inputs remain unavailable."""
    result = {}
    base = Path(root).resolve()
    for name in paths:
        path = (base / name).resolve()
        digest = None
        try:
            if (
                path.is_relative_to(base)
                and path.is_file()
                and path.stat().st_size <= MAX_FILE_BYTES
            ):
                with path.open("rb") as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) <= MAX_FILE_BYTES:
                    digest = hashlib.sha256(data).hexdigest()
        except OSError:
            pass
        result[name] = digest
    return result


def freshness(root: str, recorded: Record) -> str:
    """A missing environment/input never counts as current passing evidence."""
    current = fingerprints(root, list(recorded))
    if not recorded or any(value is None for value in current.values()):
        return "unavailable"
    if any(Path(root, name).is_file() and name not in recorded for name in DEPENDENCIES):
        return "stale"
    return "current" if current == recorded else "stale"


class PrimeMemory:
    """Separate versioned task/check/evidence namespaces in the existing durable store."""

    def __init__(self, context: Record) -> None:
        """Bind records to a canonical project, retaining each record's checkout."""
        self.context = context
        self.store = get_durable_store()

    def namespace(self, kind: str) -> tuple[str, ...]:
        """Keep existing verification and wiki namespaces untouched."""
        return ("nova", "prime", "v1", self.context["project"], kind)

    def save(self, kind: str, record: Record) -> None:
        """Persist a bounded, redacted snapshot with a unique identity."""
        value = sanitized({**record, "version": 1})
        if len(json.dumps(value)) > MAX_RECORD_BYTES:
            msg = "Prime record exceeds storage limit"
            raise ValueError(msg)
        self.store.put(self.namespace(kind), record["id"], value)

    def get(self, kind: str, identity: str) -> Record | None:
        """Read detail only when requested; unknown versions are not executable context."""
        item = self.store.get(self.namespace(kind), identity)
        return item.value if item and item.value.get("version") == 1 else None

    def records(self, kind: str) -> list[Record]:
        """Read recent records with explicit pagination bounds."""
        return [
            item.value
            for item in self.store.search(self.namespace(kind), limit=200)
            if item.value.get("version") == 1
        ]

    def forget(self, kind: str, identity: str) -> None:
        """Remove only this saved record, never source files or conversation history."""
        self.store.delete(self.namespace(kind), identity)

    def recall(
        self,
        files: list[str],
        dependencies: list[str],
        *,
        exclude_run_id: str | None = None,
    ) -> list[Record]:
        """Rank declared overlap without embeddings, providers or command execution."""
        matches = []
        for check in self.records("checks"):
            if check.get("run_id") == exclude_run_id:
                continue
            overlap = len(set(files) & set(check.get("files", [])))
            overlap += len(set(dependencies) & set(check.get("dependencies", [])))
            if overlap:
                matches.append((overlap, check))
        matches.sort(key=lambda item: item[0], reverse=True)
        recalled: list[Record] = []
        remaining = 1990
        for _, check in matches:
            state = freshness(self.context["root"], check.get("fingerprints", {}))
            summary = safe_text(
                f"{check['id']} · {check.get('finding', 'draft')} · "
                f"{check.get('status', 'unverified')} · {state} · "
                f"{check.get('purpose', '')}",
                380,
            )
            if len(summary) + 1 > remaining or len(recalled) >= MAX_RECALL:
                break
            remaining -= len(summary) + 1
            recalled.append({"id": check["id"], "summary": summary, "freshness": state})
        return recalled
