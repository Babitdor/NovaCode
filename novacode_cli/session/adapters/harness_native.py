"""Adapter for Nova's existing snapshot storage."""

import json
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
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
        roles: dict[str, Literal["user", "assistant", "tool", "system"]] = {
            "human": "user",
            "ai": "assistant",
            "tool": "tool",
            "system": "system",
        }
        messages = []
        names: dict[str, str] = {}
        for message in data.messages:
            content = text_content(message.content)
            call_id = getattr(message, "tool_call_id", None)
            if content or message.type == "tool":
                messages.append(
                    HarnessMessage(
                        roles.get(message.type, "system"),
                        content,
                    tool_name=getattr(message, "name", None) or names.get(call_id or ""),
                        metadata={"call_id": call_id} if call_id else {},
                    )
                )
            for call in getattr(message, "tool_calls", []):
                names[call["id"]] = call["name"]
                messages.append(
                    HarnessMessage(
                        "assistant",
                        json.dumps(call["args"], ensure_ascii=False),
                        tool_name=call["name"],
                        metadata={"kind": "tool_call", "call_id": call["id"]},
                    )
                )
        return ImportedSession(self.provider, session_id, data.meta.project_root, messages)
