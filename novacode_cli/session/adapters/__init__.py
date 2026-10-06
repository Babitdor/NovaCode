"""Provider independent local transcript adapters.

Installed packages may expose factories through ``novacode.session_adapters``.
Adapters never execute historical tools or change the source session.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)
MAX_SOURCE_BYTES = 50 * 1024 * 1024


@dataclass
class HarnessMessage:
    """A historical message, never an executable graph tool call."""

    role: Literal["user", "assistant", "tool", "system"]
    content: str
    timestamp: str | None = None
    tool_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject corrupt normalized records before using them as context."""
        if self.role not in {"user", "assistant", "tool", "system"} or not isinstance(
            self.content, str
        ):
            message = "Normalized messages require a supported role and string content."
            raise ValueError(message)


@dataclass
class ImportedSession:
    """Portable transcript including its original identity and workspace."""

    provider: str
    session_id: str
    cwd: str | None
    messages: list[HarnessMessage]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize a normalized transcript."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImportedSession:
        """Load the normalized transcript format."""
        return cls(
            provider=data["provider"],
            session_id=data["session_id"],
            cwd=data.get("cwd"),
            messages=[HarnessMessage(**message) for message in data["messages"]],
            metadata=data.get("metadata", {}),
        )


@dataclass
class SessionInfo:
    """Discovery row with a stable load selector."""

    provider: str
    session_id: str
    cwd: str | None
    title: str
    message_count: int
    updated_at: float
    path: str


class SessionAdapter(Protocol):
    """Minimum extension interface; live watching is an optional future capability."""

    provider: str

    def discover(self) -> list[SessionInfo]:
        """Find locally available sessions."""
        ...

    def load(self, session_id: str) -> ImportedSession:
        """Normalize a discovered ID or explicit file path."""
        ...


def text_content(content: Any) -> str:  # noqa: ANN401 — heterogeneous provider content blocks
    """Read text blocks while excluding images and private reasoning."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") in {"text", "input_text", "output_text"}
        )
    return ""


def read_document(path: Path) -> str:
    """Read a bounded UTF-8 file, including files growing during import."""
    with path.open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        message = "Session exceeds the 50 MiB import limit; export a smaller transcript."
        raise ValueError(message)
    return raw.decode("utf-8-sig")


def read_records(path: Path) -> list[dict[str, Any]]:
    """Read JSON or JSONL, tolerating only an incomplete final live record."""
    raw = read_document(path)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        records = []
        lines = raw.splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1 and not raw.endswith("\n"):
                    break
                message = f"Malformed session record at line {index + 1}."
                raise ValueError(message) from exc
            if isinstance(record, dict):
                records.append(record)
        return records
    if isinstance(data, list):
        return [record for record in data if isinstance(record, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("messages"), list):
            return [record for record in data["messages"] if isinstance(record, dict)]
        return [data]
    message = "Unsupported transcript document."
    raise ValueError(message)


class FileAdapter:
    """Shared bounded discovery and unambiguous ID resolution."""

    provider: str

    def __init__(self, root: Path) -> None:
        """Locate one provider's local transcript tree."""
        self.root = root
        self._paths: dict[str, Path] = {}

    def discover(self) -> list[SessionInfo]:
        """Discover recent local JSONL files without modifying them."""
        if not self.root.exists():
            return []
        self._paths.clear()
        paths = sorted(
            self.root.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True
        )
        rows = []
        for path in paths[:200]:
            if "subagents" in path.parts:
                continue
            try:
                session = self.parse(path)
                if not session.messages:
                    continue
                if session.session_id in self._paths:
                    continue
                self._paths[session.session_id] = path
                first = next((m.content for m in session.messages if m.role == "user"), "Untitled")
                rows.append(
                    SessionInfo(
                        self.provider,
                        session.session_id,
                        session.cwd,
                        (first.splitlines() or ["Untitled"])[0][:70],
                        len(session.messages),
                        path.stat().st_mtime,
                        str(path),
                    )
                )
            except (OSError, ValueError, KeyError, TypeError):
                logger.debug("Skipping unreadable %s transcript: %s", self.provider, path)
        return rows

    def load(self, session_id: str) -> ImportedSession:
        """Load explicit paths or resolve an exact/unique ID prefix."""
        path = Path(session_id).expanduser()
        if path.is_file():
            return self.parse(path)
        if not self._paths:
            self.discover()
        matches = [key for key in self._paths if key.startswith(session_id)]
        if session_id in self._paths:
            matches = [session_id]
        if len(matches) != 1:
            message = "Session ID was not found or is ambiguous; use its full ID or file path."
            raise ValueError(message)
        return self.parse(self._paths[matches[0]])

    def parse(self, path: Path) -> ImportedSession:
        """Implemented by each provider."""
        raise NotImplementedError


def adapters() -> dict[str, SessionAdapter]:
    """Build adapters, including explicitly installed extension packages."""
    from .claude_code import ClaudeCodeAdapter
    from .codex import CodexAdapter
    from .harness_native import NativeAdapter

    registry: dict[str, SessionAdapter] = {
        "codex": CodexAdapter(
            Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
        ),
        "claude": ClaudeCodeAdapter(
            Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects"
        ),
        "nova": NativeAdapter(),
    }
    for entry in entry_points(group="novacode.session_adapters"):
        try:
            adapter = entry.load()()
            if adapter.provider in registry:
                message = "Adapter provider conflicts with an existing provider."
                raise ValueError(message)  # noqa: TRY301 — isolate invalid plugins
            if not callable(adapter.discover) or not callable(adapter.load):
                message = "Adapter must implement discover and load."
                raise TypeError(message)  # noqa: TRY301 — isolate invalid plugins
            registry[adapter.provider] = adapter
        except Exception:  # noqa: BLE001 — one broken installed plugin must not hide other adapters
            logger.warning("Could not load session adapter %s", entry.name, exc_info=True)
    return registry
