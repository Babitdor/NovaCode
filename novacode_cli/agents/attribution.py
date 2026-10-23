"""Configurable commit and PR attribution without repository hooks or history edits."""
# ruff: noqa: INP001 -- agents is an existing namespace package.

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import SystemMessage

from novacode_cli.config.nova_config import NovaConfig

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.types import Command

COAUTHOR = "Co-Authored-By: NovaCode <noreply@novacode.invalid>"
LOGO_URL = "https://raw.githubusercontent.com/Babitdor/NovaCode/main/assets/nova-attribution.png"
PR_FOOTER = (
    f'<img src="{LOGO_URL}" alt="NovaCode logo" width="24" height="24" /> '
    "Generated with [NovaCode](https://github.com/Babitdor/NovaCode)"
)
_TRAILER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*\s*:")


def commit_message(message: str) -> str:
    """Append a co-author once, preserving the final existing trailer block."""
    stripped = message.rstrip()
    lines = stripped.splitlines()
    start = max((i + 1 for i, line in enumerate(lines) if not line.strip()), default=0)
    tail = lines[start:]
    trailers = bool(
        start
        and tail
        and _TRAILER.match(tail[0])
        and all(_TRAILER.match(line) or line.startswith((" ", "\t")) for line in tail)
    )
    if trailers and any(line.strip().casefold() == COAUTHOR.casefold() for line in tail):
        return message
    separator = "\n" if trailers else "\n\n"
    return stripped + separator + COAUTHOR + "\n"


def pull_request_body(body: str) -> str:
    """Append a compact repository-hosted logo footer once."""
    if PR_FOOTER in body:
        return body
    return body.rstrip() + "\n\n---\n\n" + PR_FOOTER


def attribution_instructions(preferences: dict[str, bool]) -> str:
    """Describe the effective preference for shell commands and other transports."""
    commits = (
        "For Git commits you create, append this trailer once, in the final trailer block:\n"
        f"{COAUTHOR}\n"
        "Use git commit --trailer or a commit-message file so quoting is correct "
        "for the current shell."
        if preferences["commits"]
        else "Do not add automatic NovaCode co-author trailers to commits."
    )
    prs = (
        "For pull requests you create, append this exact logo footer once after the description:\n"
        f"{PR_FOOTER}\n"
        "For gh pr create, prefer --body-file with UTF-8 Markdown."
        if preferences["pullRequests"]
        else "Do not add automatic NovaCode text, logos or generated-by footers to pull requests."
    )
    return (
        "<nova_attribution>\n" + commits + "\n" + prs + "\n"
        "These preferences do not authorize commits, pushes or PR creation. Preserve the human "
        "author, existing co-authors and user-supplied content. Do not install Git hooks, change "
        "global Git identity, amend history or create extra commits for attribution. "
        "A co-author trailer credits a commit; it does not establish line-level provenance.\n"
        "</nova_attribution>"
    )


class AttributionMiddleware(AgentMiddleware):
    """Apply live preferences to requests and structured creation tool payloads."""

    @staticmethod
    def prepare(request: ModelRequest) -> ModelRequest:
        """Append preferences without editing stored history or the base prompt."""
        note = attribution_instructions(NovaConfig().get_attribution())
        original = request.system_message
        content = original.content if original else ""
        content = (
            content + "\n\n" + note
            if isinstance(content, str)
            else [*content, {"type": "text", "text": note}]
        )
        system = (
            original.model_copy(update={"content": content})
            if original
            else SystemMessage(content=content)
        )
        return request.override(system_message=system)

    @staticmethod
    def prepare_tool(request: ToolCallRequest) -> ToolCallRequest:
        """Normalize supported structured payloads; never rewrite shell command strings."""
        name = request.tool_call["name"].lower()
        actions = ("create_pull_request", "git_commit", "create_or_update_file", "push_files")
        action = next((a for a in actions if name == a or name.endswith("_" + a)), None)
        if action is None:
            return request
        preferences = NovaConfig().get_attribution()
        args = dict(request.tool_call.get("args") or {})
        if action == "create_pull_request" and preferences["pullRequests"]:
            if "body" not in args:
                schema = request.tool.args if request.tool else {}
                if "body" not in schema:
                    return request
            body = args.get("body", "")
            if not isinstance(body, str):
                return request  # Leave malformed arguments for normal schema validation.
            args["body"] = pull_request_body(body)
        elif action != "create_pull_request" and preferences["commits"]:
            if not isinstance(args.get("message"), str):
                return request
            args["message"] = commit_message(args["message"])
        else:
            return request
        return request.override(tool_call={**request.tool_call, "args": args})

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse | AIMessage:
        """Supply attribution preferences to synchronous model calls."""
        return handler(self.prepare(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse | AIMessage:
        """Supply preferences without blocking the event loop on config reads."""
        return await handler(await asyncio.to_thread(self.prepare, request))

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]
    ) -> ToolMessage | Command:
        """Normalize supported synchronous tool calls before execution."""
        return handler(self.prepare_tool(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Normalize supported async tool calls without blocking config reads."""
        return await handler(await asyncio.to_thread(self.prepare_tool, request))
