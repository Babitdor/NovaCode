"""Claude Code local JSONL and text/Markdown export ingestion."""

import re
from pathlib import Path

from . import (
    FileAdapter,
    HarnessMessage,
    ImportedSession,
    read_document,
    read_records,
    text_content,
)


class ClaudeCodeAdapter(FileAdapter):
    """Read main conversation records without sidechain or reasoning blocks."""

    provider = "claude"

    def parse(self, path: Path) -> ImportedSession:  # noqa: PLR0912 — provider record variants
        """Load native history or a conversation exported with /export."""
        if path.suffix.lower() in {".md", ".txt"}:
            raw = read_document(path)
            # Export headings vary by version. Preserve the complete document
            # as one historical block when its roles cannot be identified.
            parts = re.split(r"(?im)^(?:#{1,3}\s+)?(user|human|assistant|claude)\s*:?\s*$", raw)
            messages = [
                HarnessMessage(
                    "user" if parts[index].lower() in {"user", "human"} else "assistant",
                    parts[index + 1].strip(),
                )
                for index in range(1, len(parts) - 1, 2)
            ]
            return ImportedSession(
                self.provider,
                path.stem,
                None,
                messages or [HarnessMessage("user", raw)],
                {"source": "export", "path": str(path)},
            )
        records = read_records(path)
        session = ImportedSession(self.provider, path.stem, None, [])
        seen = set()
        names = {}
        for record in records:
            if record.get("isSidechain"):
                continue
            session.session_id = record.get("sessionId", session.session_id)
            session.cwd = record.get("cwd", session.cwd)
            message = record.get("message", record)
            if not isinstance(message, dict):
                continue
            role = message.get("role", record.get("type"))
            if role not in {"user", "assistant", "system"}:
                continue
            identity = record.get("uuid")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            content = message.get("content", "")
            timestamp = record.get("timestamp")
            if isinstance(content, str):
                session.messages.append(HarnessMessage(role, content, timestamp))
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    session.messages.append(HarnessMessage(role, block.get("text", ""), timestamp))
                elif kind == "tool_use":
                    name = block.get("name", "unknown")
                    names[block.get("id")] = name
                    session.messages.append(
                        HarnessMessage(
                            "assistant",
                            str(block.get("input", "")),
                            timestamp,
                            name,
                            {"kind": "tool_call", "call_id": block.get("id")},
                        )
                    )
                elif kind == "tool_result":
                    call_id = block.get("tool_use_id")
                    session.messages.append(
                        HarnessMessage(
                            "tool",
                            text_content(block.get("content")),
                            timestamp,
                            names.get(call_id, "unknown"),
                            {"call_id": call_id},
                        )
                    )
        if not session.messages:
            message = "No supported Claude conversation messages were found."
            raise ValueError(message)
        return session
