"""Opt-in terse response style, applied only to the model's request copy."""
# ruff: noqa: INP001, ANN401 -- existing namespace and heterogeneous LangGraph state.

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, NotRequired

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ModelRequest
from langchain_core.messages import SystemMessage, ToolMessage

from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.prompts import TEMPLATES_DIR

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelResponse

CAVEMAN_RULES = """Answer first. Stop once complete. Compress prose, not technical substance.
Drop filler, pleasantries, redundant hedging and repeated summaries. Use short synonyms.
No tool-call narration, decorative tables/emoji or long raw logs unless asked;
quote the shortest decisive error exactly. Standard tech acronyms are fine;
never invent abbreviations. Preserve the user's dominant language, negations,
numbers, units, qualifications and technical terms. Never abbreviate real code
symbols, function/API names, commands, paths, commit-type keywords or errors.
Never announce or name this style, use caveman self-reference, or append a
second recap. Keep this style each response until disabled; honor requests for
normal mode or to stop caveman. User requests for detail still get needed detail.
"""
CAVEMAN_CLARITY = """These boundaries override intensity. Do not invent facts or rationale.
Language choice overrides intensity: preserve the user's dominant language
unless the user explicitly requests another language. Selecting Wenyan does not request translation.
Keep factual uncertainty. Code stays exact; commit messages and PR descriptions use normal prose.
Surrounding explanations still use the selected terse style.
Use full, clear sentences for security warnings, irreversible confirmations,
multi-step procedures whose order could be misread, when compression itself creates
technical ambiguity, or a request to clarify/repeated question.
Resume terse prose after that clear part.
Never shorten implementations, skip verification or drop necessary evidence to
save tokens. All security and approval rules remain in force.
"""
CAVEMAN_STYLES = {
    "lite": "Keep articles and full sentences. Professional, tight prose.",
    "full": (
        "Drop articles (a/an/the). Fragments OK. Short synonyms; no invented abbreviations. "
        "Example prose: 'New object ref each render. Wrap in useMemo.'"
    ),
    "ultra": (
        "Abbreviate prose words only (DB/auth/config/req/res/fn/impl). "
        "Strip conjunctions; use arrows for clear causality. One word when sufficient. "
        "Never abbreviate real code symbols or technical identifiers."
    ),
    "wenyan-lite": (
        "For Chinese replies only, use semi-classical Chinese, retaining grammatical "
        "structure; drop filler."
    ),
    "wenyan-full": (
        "For Chinese replies only, use terse classical Chinese, sentence patterns and particles; "
        "omit subjects when clear, verbs before objects. Technical terms stay verbatim."
    ),
    "wenyan-ultra": (
        "For Chinese replies only, use maximally terse classical Chinese, preserving clarity "
        "and exact technical terms."
    ),
}


def caveman_instructions(level: str = "full") -> str:
    """Build only the selected intensity, keeping unused levels out of context."""
    level = level if level in CAVEMAN_STYLES else "full"
    intensity = CAVEMAN_STYLES[level]
    if level.startswith("wenyan-"):
        intensity += (
            " For replies in any other language, use this compression in that language: "
            + CAVEMAN_STYLES[level.removeprefix("wenyan-")]
        )
    return (
        f'<nova_caveman_style level="{level}">\n'
        "Selected user prose preference; overrides default response styling only.\n"
        + CAVEMAN_RULES
        + "\nIntensity: "
        + intensity
        + "\n\n"
        + CAVEMAN_CLARITY
        + "</nova_caveman_style>"
    )


CAVEMAN_INSTRUCTIONS = caveman_instructions()
_DEFAULT_STYLE = (TEMPLATES_DIR / "response_style.jinja").read_text(encoding="utf-8").strip()

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

    def __init__(self, *, style_only: bool = False) -> None:
        """Let subagents share prose preferences without routing or recovery tools."""
        self._style_only = style_only

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any]:  # noqa: ARG002
        """Choose a configured model/effort route once per run, without double routing."""
        config = NovaConfig()
        route = None
        if (
            self._style_only
            or not config.get_caveman_features()["auto"]
            or config.get_router_enabled()
        ):
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
        if self._style_only:
            return None
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

        config = NovaConfig()
        if config.get_compression_mode() == "ponytail":
            from novacode_cli.agents.ponytail import ponytail_instructions

            return self.prepare_instructions(
                request, ponytail_instructions(config.get_ponytail_level())
            )
        features = config.get_caveman_features()
        if self._style_only:
            return self.prepare(request, enabled=features["say"], level=config.get_caveman_level())
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
        request = self.prepare(request, enabled=features["say"], level=config.get_caveman_level())
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
    def prepare(request: ModelRequest, *, enabled: bool, level: str = "full") -> ModelRequest:
        """Apply the selected style to a copy, replacing only Nova's bundled default."""
        if not enabled:
            return request
        return CavemanMiddleware.prepare_instructions(request, caveman_instructions(level))

    @staticmethod
    def prepare_instructions(request: ModelRequest, instructions: str) -> ModelRequest:
        """Replace the bundled style on a request copy, preserving block metadata."""
        original = request.system_message
        content = original.content if original else ""
        if isinstance(content, str):
            if _DEFAULT_STYLE in content:
                content = content.replace(_DEFAULT_STYLE, instructions, 1)
            else:
                content += "\n\n" + instructions
        else:
            blocks = []
            replaced = False
            for block in content:
                prepared_block = block
                if (
                    isinstance(block, dict)
                    and isinstance(block.get("text"), str)
                    and not replaced
                    and _DEFAULT_STYLE in block["text"]
                ):
                    prepared_block = {
                        **block,
                        "text": block["text"].replace(_DEFAULT_STYLE, instructions, 1),
                    }
                    replaced = True
                blocks.append(prepared_block)
            if not replaced:
                blocks.append({"type": "text", "text": instructions})
            content = blocks
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
