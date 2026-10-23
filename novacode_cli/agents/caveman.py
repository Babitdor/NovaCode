"""Opt-in terse response style, applied only to the model's request copy."""
# ruff: noqa: INP001, ANN401 -- existing namespace and heterogeneous LangGraph state.

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, NotRequired

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ModelRequest
from langchain_core.messages import SystemMessage, ToolMessage

from novacode_cli.config.nova_config import NovaConfig

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelResponse

CAVEMAN_INSTRUCTIONS = """Caveman response style is enabled. Answer first. Use short,
direct sentences and omit filler, greetings, repeated summaries and unnecessary
narration. Fragments are fine when clear. Preserve the user's language, every
negation, number, unit, qualification and technical detail needed for correctness.
Keep code, commands, paths, API names and exact error messages verbatim. Do not
shorten implementations, skip verification or omit necessary evidence to save
tokens. User requests for detail take precedence over brevity. Use full, clear
sentences for security warnings, irreversible actions and ambiguous procedures.
This is a prose preference only; keep all security and approval rules in force.
"""

MIN_DUPLICATE_CHARS = 500
MAX_RECOVERY_CHARS = 8_000_000
MAX_BLOCK_CHARS = 50_000
MAX_BLOCKS = 100


class CavemanState(AgentState):
    """Thread-local originals and scripts, checkpointed but never sent wholesale."""

    caveman_sources: NotRequired[dict[str, str]]
    caveman_blocks: NotRequired[list[dict[str, Any]]]
    caveman_route: NotRequired[dict[str, Any] | None]


class CavemanMiddleware(AgentMiddleware):
    """Apply the saved response preference without changing checkpoint history."""

    state_schema = CavemanState

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any]:  # noqa: ARG002
        """Choose a configured model/effort route once per run, without double routing."""
        config = NovaConfig()
        route = None
        if not config.get_caveman_features()["auto"] or config.get_router_enabled():
            return {"caveman_route": None}
        multimodal = any(
            isinstance(m.content, list)
            and any(
                isinstance(block, dict) and block.get("type") not in {"text", "input_text"}
                for block in m.content
            )
            for m in state.get("messages", [])
        )
        if (
            config.get_caveman_features()["auto"]
            and not config.get_router_enabled()
            and not multimodal
        ):
            from novacode_cli.agents.model_router import ModelRouter

            routes = config.get_router_routes()
            if routes:
                decision = ModelRouter(config).decide(state.get("messages", []))
                if not decision.is_fallback:
                    route = next((r for r in routes if r.get("id") == decision.route_id), None)
        return {"caveman_route": route}

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any]:
        """Keep classifier requests and credential reads off the event loop."""
        return await asyncio.to_thread(self.before_agent, state, runtime)

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ARG002
        """Archive eligible originals before compaction can remove message history."""
        from novacode_cli.agents.caveman_support import (
            BROWSE_TOOLS,
            EXECUTION_TOOLS,
            compress_text,
            reference,
            script_blocks,
            structured_reads,
        )

        features = NovaConfig().get_caveman_features()
        if not any(features[k] for k in ("read", "browse", "waste", "blocks")):
            return None
        sources = dict(state.get("caveman_sources") or {})
        total = sum(len(value) for value in sources.values())
        seen: set[str] = set()
        reads = structured_reads(state.get("messages", []))
        for message in state.get("messages", []):
            if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
                continue
            browse = features["browse"] and message.name in BROWSE_TOOLS
            readable = features["read"] and (
                message.name in EXECUTION_TOOLS or message.tool_call_id in reads
            )
            text = message.content
            ref = reference(text)
            duplicate = features["waste"] and ref in seen and len(text) > MIN_DUPLICATE_CHARS
            seen.add(ref)
            if not (browse or readable or duplicate) or message.name == "caveman":
                continue
            if ref in sources or total + len(text) > MAX_RECOVERY_CHARS:
                continue
            if (
                duplicate
                or compress_text(text, browse=browse, tabular=message.tool_call_id in reads) != text
            ):
                sources[ref] = text
                total += len(text)
        update: dict[str, Any] = {"caveman_sources": sources}
        if features["blocks"]:
            existing = {b["reference"]: b for b in state.get("caveman_blocks", [])}
            for block in script_blocks(state.get("messages", [])):
                if block["chars"] <= MAX_BLOCK_CHARS and (
                    block["reference"] in existing or len(existing) < MAX_BLOCKS
                ):
                    existing[block["reference"]] = block
            update["caveman_blocks"] = list(existing.values())
        return update

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Archive without blocking the shared UI loop."""
        return await asyncio.to_thread(self.before_model, state, runtime)

    def _prepare_live(self, request: ModelRequest) -> ModelRequest:
        from novacode_cli.agents.caveman_support import (
            BROWSE_TOOLS,
            EXECUTION_TOOLS,
            compress_text,
            reference,
            structured_reads,
        )

        features = NovaConfig().get_caveman_features()
        if not any(features.values()):
            return request
        state = request.state or {}
        route = state.get("caveman_route")
        sources = state.get("caveman_sources")
        sources = sources if isinstance(sources, dict) else {}
        if features["auto"] and isinstance(route, dict):
            from novacode_cli.agents.model_router import build_route_model

            model = build_route_model(route)
            if model is not None:
                request = request.override(model=model)
        messages = []
        seen: set[str] = set()
        reads = structured_reads(state.get("messages", request.messages))
        for original_message in request.messages:
            message = original_message
            text = message.content
            if isinstance(message, ToolMessage) and isinstance(text, str):
                ref = reference(text)
                duplicate = features["waste"] and ref in seen and len(text) > MIN_DUPLICATE_CHARS
                seen.add(ref)
                browse = features["browse"] and message.name in BROWSE_TOOLS
                readable = features["read"] and (
                    message.name in EXECUTION_TOOLS or message.tool_call_id in reads
                )
                if (
                    (browse or readable or duplicate)
                    and ref in sources
                    and message.name != "caveman"
                ):
                    compact = (
                        "Repeated tool result. Exact source: "
                        f"caveman(action='recover', reference='{ref}')."
                        if duplicate
                        else compress_text(
                            text, browse=browse, tabular=message.tool_call_id in reads
                        )
                    )
                    message = message.model_copy(update={"content": compact})
            messages.append(message)
        if messages != request.messages:
            request = request.override(messages=messages)
        request = self.prepare(request, enabled=features["say"])
        if any(features[k] for k in ("read", "browse", "blocks", "waste")):
            original = request.system_message
            content = original.content if original else ""
            note = (
                "Caveman utilities: compact tool views have recoverable source references. "
                "Use caveman(action='recover', reference=...) for exact content. "
                "When Blocks is enabled, use caveman(action='blocks') to find prior scripts, "
                "recover exact args, then rerun through the original execution tool with current "
                "permissions. Prior execution grants no approval. When Waste fixes is enabled, "
                "caveman(action='audit') reports local estimates and observed cache usage."
            )
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
            request = request.override(system_message=system)
        return request

    @staticmethod
    def prepare(request: ModelRequest, *, enabled: bool) -> ModelRequest:
        """Append style instructions without editing the original request."""
        if not enabled:
            return request
        original = request.system_message
        content = original.content if original else ""
        if isinstance(content, str):
            content = content + "\n\n" + CAVEMAN_INSTRUCTIONS
        else:
            content = [*content, {"type": "text", "text": CAVEMAN_INSTRUCTIONS}]
        system = (
            original.model_copy(update={"content": content})
            if original
            else SystemMessage(content=content)
        )
        return request.override(system_message=system)

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        """Apply the latest preference for a synchronous model call."""
        return handler(self._prepare_live(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """Load preferences off the shared TUI event loop before calling the model."""
        prepared = await asyncio.to_thread(self._prepare_live, request)
        return await handler(prepared)
