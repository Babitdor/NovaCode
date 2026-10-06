"""Codex rollout JSONL adapter."""

from pathlib import Path
from typing import Any

from . import FileAdapter, HarnessMessage, ImportedSession, read_records, text_content


class CodexAdapter(FileAdapter):
    """Normalize response items, with event-only message fallback."""

    provider = "codex"

    def parse(self, path: Path) -> ImportedSession:
        """Preserve historical tool inputs/results as ordinary transcript text."""
        records = read_records(path)
        meta: dict[str, Any] = next(
            (r.get("payload", {}) for r in records if r.get("type") == "session_meta"), {}
        )
        session = ImportedSession(self.provider, meta.get("id", path.stem), meta.get("cwd"), [])
        event_messages = []
        names = {}
        for record in records:
            payload = record.get("payload", {})
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            timestamp = record.get("timestamp")
            if record.get("type") == "event_msg" and kind in {"user_message", "agent_message"}:
                event_messages.append(
                    HarnessMessage(
                        "user" if kind == "user_message" else "assistant",
                        payload.get("message", ""),
                        timestamp,
                    )
                )
            if record.get("type") != "response_item":
                continue
            if kind == "message":
                role = payload.get("role")
                content = text_content(payload.get("content"))
                if role in {"user", "assistant", "system", "developer"} and content:
                    session.messages.append(
                        HarnessMessage(
                            "system" if role == "developer" else role,
                            content,
                            timestamp,
                        )
                    )
            elif kind in {"function_call", "custom_tool_call"}:
                name = payload.get("name", "unknown")
                names[payload.get("call_id")] = name
                session.messages.append(
                    HarnessMessage(
                        "assistant",
                        str(payload.get("arguments", payload.get("input", ""))),
                        timestamp,
                        name,
                        {"kind": "tool_call", "call_id": payload.get("call_id")},
                    )
                )
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                session.messages.append(
                    HarnessMessage(
                        "tool",
                        str(payload.get("output", "")),
                        timestamp,
                        names.get(payload.get("call_id"), "unknown"),
                        {"call_id": payload.get("call_id")},
                    )
                )
        if not session.messages:
            session.messages = event_messages
        if not session.messages:
            message = "No supported Codex conversation messages were found."
            raise ValueError(message)
        return session
