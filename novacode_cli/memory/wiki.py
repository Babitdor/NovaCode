"""Maintain a linked, recoverable wiki over an agent's existing topic memory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

MANAGED = "<!-- nova-memory-wiki:managed -->"
START = "<!-- nova-memory-wiki:start -->"
END = "<!-- nova-memory-wiki:end -->"
HUBS = {
    "nova": "NovaCode runtime, UI and agent architecture",
    "web": "Web, design and visual verification",
    "research": "Research, harnesses and thesis work",
    "projects": "Other projects and project-scoped lessons",
    "operations": "Operating practices, tools and memory management",
}


def _read(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _safe(root: Path, path: Path) -> None:
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        msg = f"Memory path escapes the active agent directory: {path}"
        raise ValueError(msg)


def _group(path: Path) -> str:
    name = path.stem
    if len(path.parts) > 1:
        return "projects"
    if name.startswith(("nova", "tui-", "deepagents", "session-", "opencode-", "model-provider")):
        return "nova"
    if name.startswith(
        (
            "amy-",
            "ethereal",
            "frontend",
            "css-",
            "nextjs",
            "hero-",
            "image-",
            "mascot",
            "content",
            "ascii-",
            "babitdor-",
            "performance-",
            "playwright",
        )
    ):
        return "web"
    if name.startswith(("harness", "eval-", "mbse-", "thesis-", "sysforge", "rag-")):
        return "research"
    if name.startswith(("oracle", "serenedb", "hackathon", "ada-", "rust-agent", "linkedin")):
        return "projects"
    return "operations"


def _title(text: str, fallback: str) -> str:
    title = next(
        (line[2:].strip() for line in text.splitlines() if line.startswith("# ")), fallback
    )
    return title.replace("[", "").replace("]", "").replace("\n", " ")[:100]


def _link(source: Path, target: Path, label: str) -> str:
    relative = Path(os.path.relpath(target, source.parent)).as_posix()
    return f"[{label}]({quote(relative, safe='/.-_')})"


def _body(text: str) -> str:
    if START not in text:
        return text
    if text.count(START) != 1 or text.count(END) != 1:
        msg = "Malformed wiki navigation markers; preserve and repair the page manually."
        raise ValueError(msg)
    start, end = text.index(START), text.index(END) + len(END)
    if re.search(r"(?m)^## (?!Wiki navigation\s*$)", text[start:end]):
        msg = "A lesson was inserted inside wiki navigation; move it outside the markers first."
        raise ValueError(msg)
    return text[:start] + text[end:].lstrip("\n")


def _topics(agent_dir: Path) -> dict[Path, bytes]:
    memories = agent_dir / "memories"
    paths = list(memories.glob("*.md"))
    paths.extend((memories / "projects").glob("*/*.md"))
    result = {}
    for path in sorted(paths):
        _safe(agent_dir, path)
        if path.is_file():
            result[path] = path.read_bytes()
    return result


def _commit(
    agent_dir: Path, originals: dict[Path, bytes | None], updates: dict[Path, str]
) -> str | None:
    changed = {
        p: text.encode("utf-8")
        for p, text in updates.items()
        if text.encode("utf-8") != originals.get(p)
    }
    if not changed:
        return None
    # Check the entire read set before the first write, not only changed pages.
    for path, original in originals.items():
        _safe(agent_dir, path)
        if _read(path) != original:
            msg = f"Memory changed during maintenance; retry without overwriting: {path.name}"
            raise RuntimeError(msg)
    backup = _backup(agent_dir, originals)
    for path, content in changed.items():
        _safe(agent_dir, path)
        if _read(path) != originals.get(path):
            msg = f"Memory changed during maintenance; backup retained, retry: {path.name}"
            raise RuntimeError(msg)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".wiki-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            Path(temp).replace(path)
        finally:
            Path(temp).unlink(missing_ok=True)
    return str(backup)


def _backup(agent_dir: Path, originals: dict[Path, bytes | None]) -> Path:
    backup = (
        agent_dir
        / "history"
        / "wiki-backups"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:10])
    )
    _safe(agent_dir, backup)
    backup.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for path, original in originals.items():
        if original is not None:
            dest = backup / path.relative_to(agent_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(original)
            manifest[path.relative_to(agent_dir).as_posix()] = hashlib.sha256(original).hexdigest()
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return backup


def snapshot_wiki(agent_dir: Path) -> Path:
    """Save full source files before a model-driven semantic consolidation pass."""
    originals: dict[Path, bytes | None] = dict(_topics(agent_dir))
    for name in ("agent.md", "HABITS.md", "learning-approved.md"):
        path = agent_dir / name
        _safe(agent_dir, path)
        originals[path] = _read(path)
    return _backup(agent_dir, originals)


def validate_wiki(agent_dir: Path) -> list[str]:
    """Report unresolved local Markdown links outside fenced/inline code."""
    broken = []
    for path, raw in _topics(agent_dir).items():
        text = re.sub(r"(?ms)^(```|~~~).*?^\1[^\n]*$", "", raw.decode("utf-8"))
        text = re.sub(r"`[^`\n]*`", "", text)
        for target in re.findall(r"\[[^\]\n]+\]\(([^)\n]+)\)", text):
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
                continue
            local = path.parent / unquote(url.path)
            if not local.resolve().is_relative_to(agent_dir.resolve()) or not local.is_file():
                broken.append(f"{path.relative_to(agent_dir).as_posix()}: {target}")
    return broken


def _navigation(path: Path, topics: dict[Path, str], memories: Path) -> str:
    group = _group(path.relative_to(memories))
    peers = [
        p
        for p in topics
        if p != path and p.parent == path.parent and _group(p.relative_to(memories)) == group
    ]
    words = set(path.stem.split("-"))
    peers.sort(key=lambda p: (-len(words & set(p.stem.split("-"))), p.name))
    related = [p for p in peers if words & set(p.stem.split("-"))][:2]
    links = [
        _link(path, memories / "INDEX.md", "Wiki home"),
        _link(path, memories / f"wiki-{group}.md", "Topic map"),
    ]
    links.extend(_link(path, p, _title(topics[p], p.stem)) for p in related)
    # A section heading here would attract record_lesson's prepend operation
    # inside this replaceable block, losing that lesson on the next refresh.
    return f"{START}\n**Wiki navigation**\n\n" + " · ".join(links) + f"\n{END}\n"


def maintain_wiki(agent_dir: Path) -> dict[str, Any]:
    """Rebuild wiki navigation while retaining source bodies and exact backups."""
    agent_dir = agent_dir.resolve()
    memories = agent_dir / "memories"
    originals: dict[Path, bytes | None] = dict(_topics(agent_dir))
    for name in ("agent.md", "HABITS.md", "learning-approved.md"):
        path = agent_dir / name
        _safe(agent_dir, path)
        originals[path] = _read(path)
    topics = {
        p: raw.decode("utf-8")
        for p, raw in originals.items()
        if p.parent != agent_dir
        and p.name != "INDEX.md"
        and raw is not None
        and MANAGED not in raw.decode("utf-8")
    }
    for group in HUBS:
        path = memories / f"wiki-{group}.md"
        _safe(agent_dir, path)
        original = originals.setdefault(path, _read(path))
        if original is not None and MANAGED not in original.decode("utf-8"):
            msg = f"Refusing to replace user-authored map: {path.name}"
            raise ValueError(msg)
    updates = {}
    for path, text in topics.items():
        body = _body(text)
        # Preserve the body bytes as text, including its final newline.
        if START not in text and not body.endswith("\n"):
            body += "\n"
        updates[path] = body + _navigation(path, topics, memories)
    home = [
        f"# Memory Wiki\n\n{MANAGED}\n",
        "Start with a relevant map or `memory_search(query)`; read the full page, "
        "follow useful links, then verify dated claims against the active project. "
        "Relative links resolve from the page's directory. Avoid loading the whole wiki.\n",
    ]
    for group, label in HUBS.items():
        hub = memories / f"wiki-{group}.md"
        home.append(f"- [{label}](wiki-{group}.md)")
        members = [p for p in topics if _group(p.relative_to(memories)) == group]
        lines = [
            f"# {label}\n\n{MANAGED}\n\n[Wiki home](INDEX.md)\n",
            "Project names and dated observations limit where these notes apply. "
            "Related links are navigation suggestions, not proof that two claims "
            "have the same scope.\n",
        ]
        lines.extend(f"- {_link(hub, p, _title(topics[p], p.stem))}" for p in sorted(members))
        updates[hub] = "\n".join(lines) + "\n"
    for name, label in (("agent.md", "User instructions"), ("HABITS.md", "Habits checklist")):
        if originals[agent_dir / name] is not None:
            home.append(f"- [{label}](../{name})")
    home.append(
        '\nMaintain with `/dream` or `memory_wiki(action="maintain")`. '
        'Use `memory_wiki(action="check")` to report broken links. '
        "Preserve unique facts and explicit user corrections; unresolved contradictions "
        "need evidence, not a timestamp guess.\n"
    )
    index = memories / "INDEX.md"
    originals.setdefault(index, _read(index))
    updates[index] = "\n".join(home)
    backup = _commit(agent_dir, originals, updates)
    return {
        "changed": sum(text.encode("utf-8") != originals.get(p) for p, text in updates.items()),
        "topics": len(topics),
        "backup": backup,
        "broken_links": validate_wiki(agent_dir),
    }
