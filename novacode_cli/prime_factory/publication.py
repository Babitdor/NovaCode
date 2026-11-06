"""Approval-bound publication with durable intents and uncertain-outcome reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from novacode_cli.prime_factory.workspace import WorkspaceError, _digest, _run

Execute = Callable[[list[str], Path], Awaitable[tuple[int | None, str, str]]]
Persist = Callable[[dict[str, Any]], Awaitable[None]]


class Publisher:
    """Use only the host's policy-authorized executor; no direct network operations."""

    def __init__(self, root: Path, persist: Persist, execute: Execute) -> None:
        """Require persistence and execution capabilities supplied by the host."""
        self.root, self.persist, self.execute = root.resolve(), persist, execute
        self._lock = asyncio.Lock()

    def prepare(
        self,
        candidate: dict[str, Any],
        *,
        destination: str,
        branch: str,
        title: str,
        body: str,
        base: str = "main",
    ) -> dict[str, Any]:
        """Create the exact review payload; this method grants no publication authority."""
        if (
            candidate.get("workspace") != str(self.root)
            or not candidate.get("id")
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", destination) is None
            or not branch
            or branch.startswith("-")
            or not base
            or base.startswith("-")
        ):
            msg = "Publication destination, branch and candidate are required"
            raise WorkspaceError(msg)
        record = {
            "candidate": candidate,
            "destination": destination,
            "branch": branch,
            "base": base,
            "title": title,
            "body": body,
        }
        return {"id": _digest(record), "status": "prepared", **record}

    async def _command(self, argv: list[str]) -> str:
        code, output, error = await self.execute(argv, self.root)
        if code != 0:
            raise WorkspaceError(error or "Publication command failed or outcome is uncertain")
        return output

    async def publish(self, intent: dict[str, Any], *, approved_intent: str) -> dict[str, Any]:
        """Reconcile before retrying, and require approval of the entire immutable intent."""
        async with self._lock:
            return await self._publish(intent, approved_intent=approved_intent)

    async def _publish(  # noqa: PLR0915 - Reconcile before each durable publication side effect.
        self,
        intent: dict[str, Any],
        *,
        approved_intent: str,
    ) -> dict[str, Any]:
        payload = {
            key: intent[key]
            for key in ("candidate", "destination", "branch", "base", "title", "body")
        }
        if approved_intent != intent.get("id") or _digest(payload) != approved_intent:
            msg = "Publication requires human approval of the exact intent"
            raise WorkspaceError(msg)
        candidate = intent["candidate"]
        if candidate.get("workspace") != str(self.root):
            msg = "Publication workspace changed"
            raise WorkspaceError(msg)
        revision = await asyncio.to_thread(_run, self.root, ["rev-parse", "HEAD"])
        dirty = await asyncio.to_thread(_run, self.root, ["status", "--porcelain"])
        if revision != candidate["revision"] or dirty:
            msg = "Publication approval expired after candidate changes"
            raise WorkspaceError(msg)
        for name, digest in candidate.get("dependencies", {}).items():
            path = (self.root / name).resolve()
            if not path.is_relative_to(self.root):
                msg = "Publication dependency escaped the workspace"
                raise WorkspaceError(msg)
            content = await asyncio.to_thread(path.read_bytes)
            if hashlib.sha256(content).hexdigest() != digest:
                msg = "Publication dependency changed"
                raise WorkspaceError(msg)
        # This must succeed before either network side effect can be issued.
        intent = {**intent, "status": "publishing"}
        await self.persist(intent)
        if candidate.get("delivery_base_revision"):
            remote = await self._command(
                [
                    "git",
                    "ls-remote",
                    "--exit-code",
                    "--",
                    f"https://github.com/{intent['destination']}.git",
                    f"refs/heads/{intent['base']}",
                ]
            )
            observed = remote.split()
            if observed != [candidate["delivery_base_revision"], f"refs/heads/{intent['base']}"]:
                message = (
                    "Destination base changed; prepare the complete PR diff for approval again"
                )
                raise WorkspaceError(message)
        existing = await self._command(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                intent["destination"],
                "--head",
                intent["branch"],
                "--state",
                "all",
                "--json",
                "url,headRefOid,baseRefName,title,body,state",
            ]
        )
        rows = json.loads(existing)
        if not isinstance(rows, list):
            msg = "Cannot reconcile publication outcome"
            raise WorkspaceError(msg)
        if rows:
            matching = [
                row
                for row in rows
                if (
                    isinstance(row, dict)
                    and row.get("headRefOid") == revision
                    and row.get("baseRefName") == intent["base"]
                    and row.get("title") == intent["title"]
                    and row.get("body") == intent["body"]
                    and row.get("state") in {"OPEN", "MERGED", "CLOSED"}
                )
            ]
            if len(matching) != 1 or len(rows) != 1:
                msg = "Destination branch has a different or ambiguous pull request"
                raise WorkspaceError(msg)
            result = {
                **intent,
                "status": "published",
                "url": matching[0]["url"],
                "pr_state": matching[0]["state"],
            }
            await self.persist(result)
            return result
        await self._command(
            [
                "git",
                "push",
                "--",
                f"https://github.com/{intent['destination']}.git",
                f"{revision}:refs/heads/{intent['branch']}",
            ]
        )
        intent = {**intent, "status": "pushed"}
        await self.persist(intent)
        body_path = Path(
            await asyncio.to_thread(
                _run,
                self.root,
                [
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-path",
                    f"factory-pr-{intent['id']}.md",
                ],
            )
        )
        await asyncio.to_thread(
            body_path.write_text, intent["body"], encoding="utf-8", newline="\n"
        )
        output = await self._command(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                intent["destination"],
                "--head",
                intent["branch"],
                "--base",
                intent["base"],
                "--title",
                intent["title"],
                "--body-file",
                str(body_path),
            ]
        )
        url = output.strip()
        if (
            re.fullmatch(
                r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*", url
            )
            is None
        ):
            message = "PR creation outcome is uncertain; resume to reconcile before retrying"
            raise WorkspaceError(message)
        result = {**intent, "status": "published", "url": url}
        await self.persist(result)
        return result
