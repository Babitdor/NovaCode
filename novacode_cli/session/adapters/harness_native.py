"""Adapter for Nova's existing snapshot storage."""

import json
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

    from novacode_cli.session.session_persistence import SessionManager

from . import HarnessMessage, ImportedSession, SessionInfo, text_content


class NativeAdapter:
    """Expose Nova sessions through the same provider independent interface."""

    provider = "nova"

    def __init__(self, manager: "SessionManager | None" = None) -> None:
        """Use the provided store or Nova's default session store."""
        from novacode_cli.session.session_persistence import SessionManager

        self.manager = manager or SessionManager()

    def discover(self) -> list[SessionInfo]:
        """Describe recent saved Nova conversations."""
        return [
            SessionInfo(
                self.provider,
                meta.session_id,
                meta.project_root,
                meta.current_task or meta.session_id[:12],
                meta.message_count,
                datetime.fromisoformat(meta.last_active).timestamp(),
                meta.session_id,
            )
            for meta in self.manager.list_sessions(limit=200)
        ]

    def load(self, session_id: str) -> ImportedSession:
        """Convert existing LangChain messages to inert historical records."""
        data = self.manager.load_session(session_id)
        if data is None:
            message = "Nova session was not found."
            raise ValueError(message)
        return ImportedSession(
            self.provider, session_id, data.meta.project_root, normalize_messages(data.messages)
        )


def normalize_messages(
    source: list["BaseMessage"], *, pair_tools: bool = False
) -> list[HarnessMessage]:
    """Project saved messages as read-only transcript rows."""
    roles: dict[str, Literal["user", "assistant", "tool", "system"]] = {
        "human": "user",
        "ai": "assistant",
        "tool": "tool",
        "system": "system",
    }
    messages = []
    names: dict[str, str] = {}
    results = {}
    paired = set()
    if pair_tools:
        results = {
            message.tool_call_id: message
            for message in source
            if message.type == "tool" and getattr(message, "tool_call_id", None)
        }
        paired = {
            call["id"]
            for message in source
            for call in getattr(message, "tool_calls", [])
            if call["id"] in results
        }
    for message in source:
        content = text_content(message.content)
        call_id = getattr(message, "tool_call_id", None)
        if message.type == "tool" and call_id in paired:
            continue
        calls = getattr(message, "tool_calls", [])
        if pair_tools:
            messages.extend(tool_row(call, results.get(call["id"])) for call in calls)
        if content or message.type == "tool":
            messages.append(
                HarnessMessage(
                    roles.get(message.type, "system"),
                    content,
                    tool_name=getattr(message, "name", None) or names.get(call_id or ""),
                    metadata={
                        **({"call_id": call_id} if call_id else {}),
                        **({"is_error": True} if getattr(message, "status", None) == "error" else {}),
                    },
                )
            )
        if pair_tools:
            continue
        for call in calls:
            names[call["id"]] = call["name"]
            messages.append(
                HarnessMessage(
                    "assistant",
                    json.dumps(call["args"], ensure_ascii=False),
                    tool_name=call["name"],
                    metadata={"kind": "tool_call", "call_id": call["id"]},
                )
            )
    return messages


def tool_row(call: dict, result: "BaseMessage | None") -> HarnessMessage:
    """Keep a saved invocation and its result together across page boundaries."""
    return HarnessMessage(
        "tool",
        text_content(result.content) if result is not None else "No recorded result.",
        tool_name=call.get("name") or "tool",
        metadata={
            "arguments": call.get("args") or {},
            "is_error": result is not None and getattr(result, "status", "success") != "success",
            "interrupted": result is None,
        },
    )
