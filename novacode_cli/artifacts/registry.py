"""In-memory, session-scoped registry of artifacts.

MVP: one registry per process (the active session). Thread-safe because artifact
tools run in worker threads (LangChain ``run_in_executor``) while the TUI observes
from the UI thread. Observers are notified on create/update so the persistent TUI
component and any web clients can refresh.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path

logger = logging.getLogger(__name__)

VALID_TYPES = frozenset({"html", "markdown", "dashboard"})
# ponytail: dashboard renders exactly like html (sandboxed iframe) — it's a
# semantic label for the model/UI, not a separate renderer. Split if they diverge.

# event, artifact
Observer = Callable[[str, "Artifact"], None]


@dataclass
class Artifact:
    """One artifact. ``content`` is authored by the assistant; it is NEVER executed
    on the host — only rendered in a sandboxed browser context by the viewer."""

    id: str
    title: str
    type: str
    content: str
    created_at: float
    updated_at: float
    version: int = 1
    status: str = "ready"  # ready | updated

    def public_dict(self) -> dict:
        """Metadata safe to expose to web clients (no host internals here anyway)."""
        return {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "version": self.version,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _coerce_type(t: str | None) -> str:
    t = (t or "markdown").strip().lower()
    return t if t in VALID_TYPES else "markdown"


class ArtifactRegistry:
    """Session-scoped artifact store with change observers."""

    def __init__(self, path: Path | None = None) -> None:
        self._by_id: dict[str, Artifact] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._observers: list[Observer] = []
        # Where to persist. None = in-memory only (tests, pre-session startup).
        self._path: Path | None = Path(path) if path is not None else None

    # -- persistence -------------------------------------------------------
    def bind(self, path: Path) -> None:
        """Persist to *path* from now on, loading whatever is already there."""
        self._path = Path(path)
        self.load()

    def load(self) -> int:
        """Merge artifacts from disk into this registry. Returns how many loaded.

        Anything already in memory with the same id wins (it is newer than the
        file). Best-effort: a missing or corrupt file leaves the registry as-is.
        """
        if self._path is None or not self._path.exists():
            return 0
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            logger.debug("artifacts: unreadable store at %s", self._path, exc_info=True)
            return 0

        known = {f.name for f in fields(Artifact)}
        loaded = 0
        with self._lock:
            for item in raw.get("artifacts", []) if isinstance(raw, dict) else []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                # Tolerate version skew: drop unknown fields, let defaults fill.
                data = {k: v for k, v in item.items() if k in known}
                try:
                    art = Artifact(**data)
                except TypeError:
                    continue
                if art.id in self._by_id:
                    continue  # in-memory copy is newer
                self._by_id[art.id] = art
                self._order.append(art.id)
                loaded += 1
        return loaded

    def _save(self) -> None:
        """Write the whole store atomically.

        Never raises: persistence is best-effort and must not break the tool
        call that triggered it.
        """
        if self._path is None:
            return
        with self._lock:
            payload = {"artifacts": [asdict(self._by_id[i]) for i in self._order]}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._path)  # atomic: no half-written store after a crash
        except OSError:
            logger.debug("artifacts: could not save to %s", self._path, exc_info=True)

    def create(self, title: str, type: str, content: str) -> Artifact:
        art = Artifact(
            id=uuid.uuid4().hex[:8],
            title=(title or "Untitled").strip() or "Untitled",
            type=_coerce_type(type),
            content=content or "",
            created_at=time.time(),
            updated_at=time.time(),
        )
        with self._lock:
            self._by_id[art.id] = art
            self._order.append(art.id)
        self._save()
        self._notify("created", art)
        return art

    def update(
        self,
        artifact_id: str,
        *,
        title: str | None = None,
        type: str | None = None,
        content: str | None = None,
    ) -> Artifact | None:
        with self._lock:
            art = self._by_id.get(artifact_id)
            if art is None:
                return None
            if title is not None and title.strip():
                art.title = title.strip()
            if type is not None and type.strip().lower() in VALID_TYPES:
                art.type = type.strip().lower()
            if content is not None:
                art.content = content
            art.version += 1
            art.updated_at = time.time()
            art.status = "updated"
        self._save()
        self._notify("updated", art)
        return art

    def get(self, artifact_id: str) -> Artifact | None:
        with self._lock:
            return self._by_id.get(artifact_id)

    def list(self) -> list[Artifact]:
        with self._lock:
            return [self._by_id[i] for i in self._order]

    def count(self) -> int:
        with self._lock:
            return len(self._order)

    # -- observers ---------------------------------------------------------
    def add_observer(self, cb: Observer) -> None:
        if cb not in self._observers:
            self._observers.append(cb)

    def remove_observer(self, cb: Observer) -> None:
        """Detach an observer when its UI/session owner shuts down."""
        self._observers = [observer for observer in self._observers if observer != cb]

    def _notify(self, event: str, art: Artifact) -> None:
        for cb in list(self._observers):
            try:
                cb(event, art)
            except Exception:  # noqa: BLE001 — a bad observer must not break the tool
                pass


_registry: ArtifactRegistry | None = None


def store_path(session_id: str, sessions_dir: "Path | None" = None) -> Path:
    """Where a session's artifacts live: ``<sessions_dir>/<session_id>/artifacts.json``."""
    base = sessions_dir or Path.home() / ".nova" / "sessions"
    return Path(base) / session_id / "artifacts.json"


def bind_session(session_id: str, sessions_dir: "Path | None" = None) -> int:
    """Point the global registry at *session_id*'s store and load it.

    Returns the number of artifacts restored. Safe on both a fresh and a
    resumed session; best-effort, so a bad path never blocks startup.
    """
    if not session_id:
        return 0
    try:
        reg = get_registry()
        before = reg.count()
        reg.bind(store_path(session_id, sessions_dir))
        return reg.count() - before
    except Exception:  # noqa: BLE001 - never break startup on artifact restore
        logger.debug("artifacts: bind_session failed", exc_info=True)
        return 0


def get_registry() -> ArtifactRegistry:
    """Return the process-global (session) artifact registry."""
    global _registry
    if _registry is None:
        _registry = ArtifactRegistry()
    return _registry


if __name__ == "__main__":
    # ponytail: self-check for create/update/observer semantics.
    r = ArtifactRegistry()
    events: list[tuple[str, str]] = []
    r.add_observer(lambda ev, a: events.append((ev, a.title)))
    a = r.create("PR Walkthrough", "html", "<h1>hi</h1>")
    assert r.count() == 1 and a.version == 1 and a.status == "ready"
    assert r.create("Notes", "bogus", "x").type == "markdown"  # coerced
    u = r.update(a.id, content="<h1>hi2</h1>")
    assert u.version == 2 and u.status == "updated"
    assert r.update("nope") is None
    assert [e[0] for e in events] == ["created", "created", "updated"]
    assert [x.title for x in r.list()] == ["PR Walkthrough", "Notes"]
    print("artifacts.registry self-check ok")
