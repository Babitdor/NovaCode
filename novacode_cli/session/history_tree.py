"""Tree-structured session history behind ``/tree``, ``/export`` and ``/share``.

Every message a session has ever held is a node in ``tree.jsonl`` (in the
session dir), parented to the message before it. Going back to an earlier point
and continuing grows a new branch; the old one stays. The file is append-only,
one JSON object per line:

``{"t": "node", "id", "parent", "msg", "ts"}``
    A message (``msg`` is SessionManager's serialized form).
``{"t": "label", "id", "label"}``
    A bookmark on a node; an empty label removes it. The last one wins.
``{"t": "leaf", "id"}``
    Where the conversation currently is. The last one wins.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

TREE_FILE = "tree.jsonl"


@dataclass
class Node:
    """One message in the tree."""

    id: str
    parent: str | None
    msg: dict[str, Any]
    ts: str
    children: list[str] = field(default_factory=list)
    label: str | None = None

    @property
    def kind(self) -> str:
        """``user``, ``assistant``, ``tool`` (calls and results) or ``system``."""
        t = self.msg.get("type", "")
        if t == "HumanMessage":
            return "user"
        if t == "ToolMessage" or (t == "AIMessage" and self.msg.get("tool_calls")):
            return "tool"
        if t == "AIMessage":
            return "assistant"
        return "system"

    @property
    def text(self) -> str:
        """The message's text (image and other non-text blocks are noted)."""
        return message_text(self.msg)


def message_text(msg: dict[str, Any]) -> str:
    """Plain text of a serialized message's content."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") in {"image", "image_url"}:
                parts.append("[image]")
    return "\n".join(parts)


def node_id(msg: dict[str, Any], parent: str | None) -> str:
    """The message id, or a content hash for the rare message without one."""
    if msg.get("id"):
        return str(msg["id"])
    raw = json.dumps([parent, msg.get("type"), msg.get("content")], sort_keys=True, default=str)
    return "h-" + hashlib.sha1(raw.encode()).hexdigest()[:16]  # noqa: S324 — not security


class HistoryTree:
    """The in-memory tree of one session, backed by its ``tree.jsonl``."""

    def __init__(self, session_dir: Path) -> None:  # noqa: D107
        self.path = session_dir / TREE_FILE
        self.nodes: dict[str, Node] = {}
        self.roots: list[str] = []
        self.leaf: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # a torn last line from a crash
            self._apply(rec)

    def _apply(self, rec: dict[str, Any]) -> None:
        kind = rec.get("t")
        if kind == "node" and rec["id"] not in self.nodes:
            node = Node(rec["id"], rec.get("parent"), rec.get("msg") or {}, rec.get("ts", ""))
            self.nodes[node.id] = node
            parent = self.nodes.get(node.parent) if node.parent else None
            (parent.children if parent else self.roots).append(node.id)
        elif kind == "label" and rec.get("id") in self.nodes:
            self.nodes[rec["id"]].label = rec.get("label") or None
        elif kind == "leaf":
            self.leaf = rec.get("id")

    def _append(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        for rec in records:
            self._apply(rec)

    def sync(self, messages: list[dict[str, Any]]) -> None:
        """Record the conversation's current message list as a path from a root."""
        now = datetime.now(UTC).isoformat()
        records: list[dict[str, Any]] = []
        known = set(self.nodes)
        parent: str | None = None
        for msg in messages:
            nid = node_id(msg, parent)
            if nid not in known:
                records.append({"t": "node", "id": nid, "parent": parent, "msg": msg, "ts": now})
                known.add(nid)
            parent = nid
        if parent != self.leaf:
            records.append({"t": "leaf", "id": parent})
        self._append(records)

    def set_label(self, nid: str, label: str) -> None:
        """Bookmark ``nid`` (an empty label removes the bookmark)."""
        if nid in self.nodes:
            self._append([{"t": "label", "id": nid, "label": label.strip()}])

    def path_to(self, nid: str | None) -> list[Node]:
        """Nodes from the root down to ``nid`` (empty for ``None``)."""
        out: list[Node] = []
        while nid is not None and nid in self.nodes:
            out.append(self.nodes[nid])
            nid = self.nodes[nid].parent
        return out[::-1]

    def continue_point(self, nid: str) -> tuple[list[Node], str]:
        """Where choosing ``nid`` resumes: the kept path and text for the prompt.

        A user message goes back to just before it, with its text returned to
        the prompt to edit and resend. An assistant answer keeps everything up
        to and including it. A tool step can't be continued mid-way (its calls
        would be left unanswered), so it resolves to the nearest point above it.
        """
        while nid is not None:
            node = self.nodes[nid]
            if node.kind == "user":
                return self.path_to(node.parent), node.text
            if node.kind == "assistant":
                return self.path_to(nid), ""
            nid = node.parent
        return [], ""
