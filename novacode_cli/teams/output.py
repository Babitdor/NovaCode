"""Keep exact large text results outside model-facing previews."""

from __future__ import annotations

import hashlib
import tempfile
import threading
from typing import TYPE_CHECKING

from novacode_cli.teams.models import TeamError

if TYPE_CHECKING:
    from pathlib import Path

SUMMARY_BYTES = 60_000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_LOCK = threading.Lock()


def persist_output(directory: Path, text: str) -> dict[str, str | int]:
    """Write an immutable content-addressed result, with a per-root disk bound."""
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        message = "Result exceeds the artifact limit; retain deliverables in the workspace"
        raise TeamError(message)
    digest = hashlib.sha256(encoded).hexdigest()
    with _LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{digest}.txt"
        if target.exists():
            if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                message = "Existing result artifact failed verification; it was preserved"
                raise TeamError(message)
        else:
            used = sum(path.stat().st_size for path in directory.iterdir())
            if used + len(encoded) > MAX_ARTIFACT_BYTES:
                message = "Root result artifact budget reached; existing results were preserved"
                raise TeamError(message)
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary:
                path = type(directory)(temporary.name)
                temporary.write(encoded)
            try:
                path.replace(target)
            finally:
                path.unlink(missing_ok=True)
    return {"path": str(target), "sha256": digest, "bytes": len(encoded)}
