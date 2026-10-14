"""Versioned, checksummed, atomically published full-message recovery archives."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

# JSON and LangGraph interfaces carry heterogeneous values.
# ruff: noqa: ANN401
from typing import Any

from langchain_core.messages import messages_from_dict, messages_to_dict

from novacode_cli.hermes.activation import content_hash, publish

MANIFEST_VERSION = 2


class RecoveryArchiveError(ValueError):
    """An archive is incomplete or its checksum does not match."""


class RecoveryStore:
    """Keep immutable archives and identify older incomplete transcript files."""

    def __init__(self, root: Path) -> None:
        """Initialize local state without acquiring invocation ownership."""
        self.root = Path(root)

    def _blob(self, data: bytes, blobs: dict) -> Any:
        digest = content_hash(data)
        path = self.root / "blobs" / digest
        if path.exists():
            if path.read_bytes() != data:
                message = "Immutable recovery blob checksum conflict"
                raise RecoveryArchiveError(message)
        else:
            publish(path, data)
        blobs[digest] = len(data)
        return {"__nova_binary__": digest}

    def _encode(self, value: Any, blobs: Any) -> Any:
        if isinstance(value, bytes):
            return self._blob(value, blobs)
        if isinstance(value, dict):
            if set(value) in ({"__nova_binary__"}, {"__nova_mapping__"}):
                return {
                    "__nova_mapping__": [
                        [key, self._encode(item, blobs)] for key, item in value.items()
                    ]
                }
            return {key: self._encode(item, blobs) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [self._encode(item, blobs) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        message = f"Unsupported archive value: {type(value).__name__}"
        raise RecoveryArchiveError(message)

    def _read_blob(self, digest: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            message = "Invalid blob reference"
            raise RecoveryArchiveError(message)
        data = (self.root / "blobs" / digest).read_bytes()
        if content_hash(data) != digest:
            message = "Recovery blob checksum mismatch"
            raise RecoveryArchiveError(message)
        return data

    def _decode(self, value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"__nova_mapping__"}:
                return {key: self._decode(item) for key, item in value["__nova_mapping__"]}
            if set(value) == {"__nova_binary__"}:
                return self._read_blob(value["__nova_binary__"])
            return {key: self._decode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._decode(item) for item in value]
        return value

    def archive(
        self,
        thread: str,
        messages: list,
        *,
        approvals: Any = (),
        state: Any = None,
        source: str = "manual",
        checkpoint: Any = None,
    ) -> Path:
        """Publish a complete manifest after all message blobs are durable."""
        blobs: dict[str, int] = {}
        payload = self._encode(
            {
                "messages": messages_to_dict(messages),
                "approvals": approvals,
                "state": state or {},
                "checkpoint": checkpoint,
            },
            blobs,
        )
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        digest = content_hash(encoded)
        publish(self.root / "blobs" / digest, encoded)
        manifest = {
            "version": MANIFEST_VERSION,
            "id": uuid.uuid4().hex,
            "thread": thread,
            "source": source,
            "message_count": len(messages),
            "payload": digest,
            "blobs": blobs,
            "complete": True,
        }
        manifest["checksum"] = content_hash(json.dumps(manifest, sort_keys=True).encode())
        destination = self.root / "manifests" / f"{manifest['id']}.json"
        publish(destination, json.dumps(manifest, sort_keys=True).encode())
        return destination

    def inspect(self, reference: str) -> Any:
        """Validate an archive and all its referenced blobs before returning it."""
        identity = Path(reference).stem
        if not re.fullmatch(r"[0-9a-f]{32}", identity):
            message = "Use a recovery ID from /context recovery"
            raise RecoveryArchiveError(message)
        manifest = json.loads((self.root / "manifests" / f"{identity}.json").read_text())
        if manifest.get("version") not in {1, 2} or not manifest.get("complete"):
            message = "Incomplete or unsupported recovery manifest"
            raise RecoveryArchiveError(message)
        if manifest["version"] == MANIFEST_VERSION:
            checked = dict(manifest)
            checksum = checked.pop("checksum", None)
            if checksum != content_hash(json.dumps(checked, sort_keys=True).encode()):
                message = "Recovery manifest checksum mismatch"
                raise RecoveryArchiveError(message)
        else:
            manifest["integrity"] = "legacy: payload verified; manifest checksum unavailable"
        if not isinstance(manifest.get("blobs"), dict) or not isinstance(
            manifest.get("payload"), str
        ):
            message = "Incomplete recovery manifest references"
            raise RecoveryArchiveError(message)
        payload = self._decode(json.loads(self._read_blob(manifest["payload"])))
        for digest, size in manifest["blobs"].items():
            if len(self._read_blob(digest)) != size:
                message = "Recovery blob length mismatch"
                raise RecoveryArchiveError(message)
        return manifest, payload

    def list(self, thread: str | None = None) -> Any:
        """List recoverable archives and label damaged references."""
        entries = []
        for path in sorted((self.root / "manifests").glob("*.json")):
            try:
                manifest, _ = self.inspect(path.stem)
                if thread is None or manifest["thread"] == thread:
                    entries.append(manifest)
            except (OSError, ValueError, KeyError, TypeError) as error:
                entries.append({"id": path.stem, "complete": False, "error": str(error)})
        sessions = self.root.parent / "sessions"
        if sessions.is_dir():
            for session in sorted(sessions.iterdir()):
                if not session.is_dir() or (thread is not None and session.name != thread):
                    continue
                for pattern, source in (
                    ("pre-compact-*.jsonl", "manual"),
                    ("conversation_history/*.md", "automatic"),
                ):
                    entries.extend(
                        {
                            "id": f"legacy:{legacy.relative_to(sessions).as_posix()}",
                            "thread": session.name,
                            "source": source,
                            "path": str(legacy),
                            "complete": False,
                            "restorable": False,
                            "error": "Legacy transcript lacks a complete checkpoint manifest",
                        }
                        for legacy in sorted(session.glob(pattern))
                        if legacy.is_file()
                    )
        return entries

    async def restore(self, reference: str, agent: Any) -> str:
        """Restore messages and graph approvals into a fresh checkpoint thread."""
        import asyncio

        _manifest, payload = await asyncio.to_thread(self.inspect, reference)
        thread = uuid.uuid4().hex
        config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
        state = dict(payload["state"])
        state["messages"] = messages_from_dict(payload["messages"])
        # Persist approval interrupts through the saver, rather than advancing
        # the graph's model node and losing the pending task/approval structure.
        if payload.get("checkpoint"):
            from novacode_cli.session.checkpoint_coordinator import CoordinatedGraph

            if not isinstance(agent, CoordinatedGraph):
                message = "Checkpoint restoration requires a coordinated graph"
                raise RecoveryArchiveError(message)
            saver = agent.coordinator.saver
            packed = payload["checkpoint"]
            checkpoint = saver.serde.loads_typed(tuple(packed["checkpoint"]))
            pending = saver.serde.loads_typed(tuple(packed["pending"]))
            async with agent.coordinator.mutation(config, agent.project):
                saved = await saver.aput(
                    config, checkpoint, packed["metadata"], checkpoint["channel_versions"]
                )
                grouped: dict[str, list[tuple[str, Any]]] = {}
                for task_id, channel, value in pending:
                    grouped.setdefault(task_id, []).append((channel, value))
                for task_id, writes in grouped.items():
                    await saver.aput_writes(saved, writes, task_id)
            return thread
        if payload["approvals"]:
            message = "Archive contains pending approvals; use checkpoint-preserving restore"
            raise RecoveryArchiveError(message)
        await agent.aupdate_state(config, state)
        return thread


def default_recovery_store() -> RecoveryStore:
    """Resolve the shared local archive store."""
    from novacode_cli.config import config

    return RecoveryStore(config.HOME_DIR / "recovery")


async def handle_recovery_command(args: str, *, agent: Any, thread: str) -> tuple[str, str | None]:
    """Transport-independent listing, inspection, and fresh-thread restoration."""
    import asyncio

    parts = args.split()
    store = default_recovery_store()
    if not parts or parts == ["list"]:
        entries = await asyncio.to_thread(store.list, thread)
        return json.dumps(entries, indent=2), None
    command_arity = 2
    if len(parts) == command_arity and parts[0] == "inspect":
        manifest, _payload = await asyncio.to_thread(store.inspect, parts[1])
        return json.dumps(manifest, indent=2), None
    if len(parts) == command_arity and parts[0] == "restore":
        restored = await store.restore(parts[1], agent)
        return f"Restored into new thread {restored}", restored
    message = "Usage: /context recovery [list|inspect <id>|restore <id>]"
    raise ValueError(message)
