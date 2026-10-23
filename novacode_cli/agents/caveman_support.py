"""Deterministic compression and read-only recovery from the current checkpoint."""
# ruff: noqa: INP001 -- existing namespace package.

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, ToolMessage

EXECUTION_TOOLS = frozenset({"execute", "shell", "bash", "python_kernel", "run_tests"})
BROWSE_TOOLS = frozenset({"fetch_url", "web_search", "browser_automate"})
MAX_SOURCE = 2_000_000
MIN_SOURCE = 2000
MIN_CSV_ROWS = 20
RECOVERY_PAGE_CHARS = 12000


def reference(text: str) -> str:
    """Content-address data without exposing its contents in the identifier."""
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def structured_reads(messages: list) -> set[str]:
    """Identify JSON/CSV reads by their actual call arguments, including Windows paths."""
    calls: set[str] = set()
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            path = (call.get("args") or {}).get("file_path", "")
            if (
                call.get("name") == "read_file"
                and isinstance(path, str)
                and PurePosixPath(path.replace("\\", "/")).suffix.lower() in {".json", ".csv"}
            ):
                calls.add(call.get("id") or "")
    return calls


def _without_line_gutters(text: str) -> str:
    lines: list[str] = []
    previous = -1
    continuation = 0
    for row in text.splitlines():
        match = re.fullmatch(r"\s*(\d+)(?:\.(\d+))?(?: {2}|\t)(.*)", row)
        if not match:
            return text
        number = int(match[1])
        suffix = int(match[2] or 0)
        if suffix:
            if number != previous or suffix != continuation + 1:
                return text
            lines[-1] += match[3]
        else:
            if previous >= 0 and number != previous + 1:
                return text
            lines.append(match[3])
        previous, continuation = number, suffix
    return "\n".join(lines) if lines else text


class _SemanticHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0
        self.pre = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag in {"p", "div", "tr", "li", "br", "pre", "h1", "h2", "h3"}:
            self.parts.append("\n")
        if tag == "pre":
            self.pre += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.parts.append(f" [link: {href}] ")
        if tag in {"input", "button", "select", "textarea", "img"}:
            values = dict(attrs)
            # Keep controls and image descriptions; this is not an interaction snapshot.
            self.parts.append(f" [{tag}: {values}] ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if tag == "pre" and self.pre:
            self.pre -= 1
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data if self.pre else re.sub(r"\s+", " ", data))


def _json_compact(text: str) -> str:
    # Validate, then strip only whitespace OUTSIDE strings. Never round numbers,
    # coalesce duplicate keys, rewrite escaping or change large integer values.
    json.loads(text)
    result = []
    quoted = escaped = False
    for char in text:
        if quoted:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            result.append(char)
        elif not char.isspace():
            result.append(char)
    return "".join(result)


def _browse_compact(text: str) -> str:
    if re.match(r"(?is)\s*(?:<!doctype\s+html|<html\b)", text):
        parser = _SemanticHTML()
        parser.feed(text)
        parser.close()
        return "".join(parser.parts).strip()
    # Markdown from fetch_url: remove surplus blank rows, keeping fenced code exact.
    parts = []
    fenced = False
    blanks = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        if not fenced and not line.strip():
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        parts.append(line)
    return "".join(parts)


def _browse_json(text: str) -> str:
    # Replace only content string tokens. Preserve every numeric lexeme and key,
    # including duplicate keys, in the surrounding fetch envelope.
    json.loads(text)

    def replace(match: re.Match) -> str:
        content = json.loads(match[2])
        compact = _browse_compact(content)
        return match[1] + json.dumps(compact, ensure_ascii=False)

    return re.sub(r'("(?:content|html|body|markdown)"\s*:\s*)("(?:\\.|[^"\\])*")', replace, text)


def compress_text(text: str, *, browse: bool = False, tabular: bool = False) -> str:  # noqa: PLR0912
    """Return a smaller view with exact recovery, or the original on uncertainty."""
    if len(text) < MIN_SOURCE or len(text) > MAX_SOURCE:
        return text
    compact = text
    source = _without_line_gutters(text) if tabular else text
    stripped = source.lstrip()
    try:
        if browse:
            compact = (
                _browse_json(text) if stripped.startswith(("{", "[")) else _browse_compact(text)
            )
            if compact.lstrip().startswith(("{", "[")):
                compact = _json_compact(compact)
        elif stripped.startswith(("{", "[")):
            compact = _json_compact(source)
        elif tabular:
            rows = list(csv.reader(io.StringIO(source), strict=True))
            if (
                len(rows) >= MIN_CSV_ROWS
                and len(rows[0]) > 1
                and all(len(row) == len(rows[0]) for row in rows)
            ):
                out = io.StringIO(newline="")
                csv.writer(out).writerows([*rows[:6], *rows[-3:]])
                compact = (
                    f"CSV preview: {len(rows) - 1} data rows, header + first 5 + last 3.\n"
                    + out.getvalue()
                )
        else:
            lines = text.splitlines(keepends=True)
            output: list[str] = []
            skipped = 0
            for line in lines:
                if re.match(r"^\S+::\S+\s+PASSED(?:\s|$)", line) or (output and line == output[-1]):
                    skipped += 1
                else:
                    if skipped:
                        output.append(f"[omitted {skipped} passing/repeated lines]\n")
                        skipped = 0
                    output.append(line)
            if skipped:
                output.append(f"[omitted {skipped} passing/repeated lines]\n")
            compact = "".join(output)
    except (ValueError, csv.Error, RecursionError):
        return text
    marker = (
        f"\n[Compact view; exact source: caveman(action='recover', "
        f"reference='{reference(text)}'). Source chars: {len(text)}.]"
    )
    candidate = compact + marker
    # Character reduction alone is not a tokenizer guarantee. Avoid marginal wins.
    return candidate if len(candidate) < len(text) * 0.85 else text


def script_blocks(messages: list) -> list[dict[str, Any]]:
    """Index exact prior execution payloads; never execute or approve them."""
    blocks: dict[str, dict[str, Any]] = {}
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            if call.get("name") not in EXECUTION_TOOLS:
                continue
            args = call.get("args") or {}
            script = args.get("command") or args.get("code")
            if not isinstance(script, str) or not script.strip():
                continue
            ref = reference(script)
            if ref in blocks:
                blocks[ref]["uses"] += 1
            else:
                blocks[ref] = {
                    "reference": ref,
                    "tool": call["name"],
                    "args": args,
                    "uses": 1,
                    "chars": len(script),
                }
    return list(blocks.values())


def audit_messages(messages: list) -> dict[str, Any]:
    """Content-free estimates and provider-reported cache usage; no network calls."""
    from langchain_core.messages.utils import count_tokens_approximately

    by_tool: Counter = Counter()
    duplicates: Counter = Counter()
    input_tokens = cache_read = 0
    for message in messages:
        if isinstance(message, ToolMessage):
            by_tool[message.name or "unknown"] += count_tokens_approximately([message])
            duplicates[reference(str(message.content))] += 1
        if isinstance(message, AIMessage) and message.usage_metadata:
            usage = message.usage_metadata
            input_tokens += usage.get("input_tokens", 0)
            cache_read += (usage.get("input_token_details") or {}).get("cache_read", 0)
    return {
        "basis": "estimated text tokens; cache counts are provider-reported when present",
        "messages": len(messages),
        "estimated_history_tokens": count_tokens_approximately(messages),
        "estimated_tool_tokens": dict(by_tool.most_common()),
        "duplicate_tool_results": sum(count - 1 for count in duplicates.values()),
        "reported_input_tokens": input_tokens,
        "reported_cache_read_tokens": cache_read,
        "suggestions": [
            "Use recovery references instead of repeating large tool output.",
            "Reuse prior script blocks through the original execution tool.",
            "Keep standing instructions stable to improve provider prefix-cache reuse.",
        ],
        "cache_warming": "No background requests; no speculative spending.",
    }


def inspect_session(  # noqa: PLR0911 -- explicit action dispatch and failure paths.
    messages: list,
    action: str,
    *,
    ref: str = "",
    offset: int = 0,
    limit: int = 12000,
    sources: dict[str, str] | None = None,
    saved_blocks: list | None = None,
) -> dict[str, Any]:
    """Look up only content in the invoking thread's supplied checkpoint."""
    if action == "audit":
        return audit_messages(messages)
    blocks = list(
        {b["reference"]: b for b in [*(saved_blocks or []), *script_blocks(messages)]}.values()
    )
    if action == "blocks":
        return {
            "blocks": [{k: v for k, v in b.items() if k != "args"} for b in blocks][-30:],
            "note": "Recover a reference for exact args; rerun with the original tool. "
            "Current permissions apply every time; prior execution is not approval.",
        }
    if action != "recover" or not re.fullmatch(r"[a-f0-9]{64}", ref):
        return {"error": "Invalid action or recovery reference."}
    if offset < 0 or not 1 <= limit <= RECOVERY_PAGE_CHARS:
        return {"error": "offset must be nonnegative; limit must be 1..12000 characters."}
    if sources and ref in sources:
        text = sources[ref]
        return {
            "content": text[offset : offset + limit],
            "total_chars": len(text),
            "next_offset": offset + limit if offset + limit < len(text) else None,
        }
    for message in messages:
        if (
            isinstance(message, ToolMessage)
            and isinstance(message.content, str)
            and reference(message.content) == ref
        ):
            text = message.content
            return {
                "content": text[offset : offset + limit],
                "total_chars": len(text),
                "next_offset": offset + limit if offset + limit < len(text) else None,
            }
    for block in blocks:
        if block["reference"] == ref:
            # Recovered scripts are data, not permission to run them.
            return {"tool": block["tool"], "args": block["args"], "requires_current_approval": True}
    return {"error": "Reference not found in this session's current history."}


@tool("caveman")
def caveman_tool(
    action: Literal["recover", "blocks", "audit"],
    runtime: ToolRuntime,
    reference: str = "",
    offset: int = 0,
    limit: int = 12000,
) -> dict[str, Any]:
    """Recover exact compressed output, list reusable scripts, or audit token waste.

    Read-only, current-session access. Recovery supports character pagination.
    Blocks are prior execution payloads, not cached results or permission to run.
    Rerun a recovered script using its original tool and current approval policy.
    """
    from novacode_cli.config.nova_config import NovaConfig

    config = NovaConfig()
    feature = {"blocks": "blocks", "audit": "waste"}.get(action)
    if feature and not config.get_caveman_features()[feature]:
        return {"error": f"Caveman {feature} is disabled."}
    return inspect_session(
        runtime.state.get("messages", []),
        action,
        ref=reference,
        offset=offset,
        limit=limit,
        sources=runtime.state.get("caveman_sources"),
        saved_blocks=runtime.state.get("caveman_blocks"),
    )
