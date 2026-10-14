"""Reviewed artifact activation, using immutable files and an atomic manifest."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextvars import ContextVar
from pathlib import Path

# JSON and LangGraph interfaces carry heterogeneous values.
# ruff: noqa: ANN401
from typing import Any

import yaml
from jinja2 import Environment, TemplateError, meta, select_autoescape

from novacode_cli.hermes.ledger import LearningLedger

# A turn reads only its captured manifest, even if a decision arrives mid-turn.
active_artifacts: ContextVar[tuple[tuple[str, str, str], ...]] = ContextVar(
    "nova_active_artifacts", default=()
)


def content_hash(content: bytes) -> str:
    """Identify immutable artifact bytes."""
    return hashlib.sha256(content).hexdigest()


def publish(path: Path, content: bytes) -> None:
    """Flush before replacement; failed publication retains the prior file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


class ActivationManager:
    """Idempotent recovery from a crash between manifest and ledger publication."""

    def __init__(self, agent_dir: Path, project: str) -> None:
        """Initialize local state without acquiring invocation ownership."""
        self.root = Path(agent_dir) / "learning-artifacts"
        self.project = project
        self.ledger = LearningLedger(Path(agent_dir) / "learning.sqlite3")
        self.path = self.root / "activation.json"

    def manifest(self) -> Any:
        """Read the latest complete activation manifest."""
        if not self.path.exists():
            return {"version": 1, "entries": {}, "operations": []}
        manifest = json.loads(self.path.read_text(encoding="utf-8"))
        if manifest.get("version") != 1:
            message = "Unsupported activation manifest"
            raise ValueError(message)
        return manifest

    def _key(self, record: Any) -> str:
        scope = "shared" if record.scope == "shared" else content_hash(record.project.encode())
        return f"{scope}/{record.kind}/{record.target or record.id}"

    def _validate(self, record: Any) -> None:
        if record.kind not in {"lesson", "habit", "skill", "prompt", "user_model"}:
            message = "Unsupported generated artifact kind"
            raise ValueError(message)
        if record.kind in {"skill", "prompt"} and not re.fullmatch(
            r"[a-zA-Z0-9_-]+(?:\.jinja)?", record.target
        ):
            message = "Unsafe or missing artifact target"
            raise ValueError(message)
        if record.kind == "prompt":
            from novacode_cli.prompts import TEMPLATES_DIR

            source = TEMPLATES_DIR / record.target
            if not source.is_file() or content_hash(source.read_bytes()) != record.source_hash:
                message = "Prompt target hash changed; review a new proposal"
                raise ValueError(message)
            environment = Environment(autoescape=select_autoescape(default=False))
            parsed = environment.parse(record.content)
            required = meta.find_undeclared_variables(
                environment.parse(source.read_text(encoding="utf-8"))
            )
            supplied = meta.find_undeclared_variables(parsed)
            if not required.issubset(supplied) or not supplied.issubset(required):
                message = "Prompt required variables changed"
                raise ValueError(message)
            if not set(record.required_variables).issubset(supplied):
                message = "Prompt required variables missing"
                raise ValueError(message)
        if record.kind == "skill":
            self._validate_skill(record)
        if not record.approval_reference:
            message = "Artifact lacks revision-specific approval"
            raise ValueError(message)

    def _validate_skill(self, record: Any) -> None:
        """Require loadable frontmatter and an unchanged destination skill."""
        from deepagents.middleware.skills import _parse_skill_metadata

        frontmatter = re.match(
            r"\A---[ \t]*\r?\n(.*?)\r?\n---(?:[ \t]*\r?\n|[ \t]*$)", record.content, re.DOTALL
        )
        try:
            metadata = yaml.safe_load(frontmatter.group(1)) if frontmatter else None
        except yaml.YAMLError as exc:
            message = "Generated skill frontmatter is invalid YAML"
            raise ValueError(message) from exc
        if not isinstance(metadata, dict) or any(
            not isinstance(metadata.get(field), str) or not metadata[field].strip()
            for field in ("name", "description")
        ):
            message = "Generated skill requires string name and description"
            raise ValueError(message)
        parsed = _parse_skill_metadata(record.content, f"{record.target}/SKILL.md", record.target)
        if parsed is None or parsed["name"] != record.target:
            message = "Generated skill frontmatter is invalid or names another target"
            raise ValueError(message)
        source = self.root.parent / "skills" / record.target / "SKILL.md"
        if record.source_hash:
            if not source.is_file() or content_hash(source.read_bytes()) != record.source_hash:
                message = "Skill target hash changed; review a new proposal"
                raise ValueError(message)
        elif source.exists():
            message = "Skill target already exists; its content hash requires review"
            raise ValueError(message)

    def apply_pending(self) -> Any:
        """Called once between turns; writer lock serializes all manifests."""
        # Serialize publication with ledger mutations across processes. A separate
        # connection is used for transitions only after this transaction ends.
        transitions = []
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            manifest = self.manifest()
            for record in self.ledger.list(project=self.project):
                if record.status not in {"approved", "rollback_pending"}:
                    continue
                if record.reference in manifest["operations"]:
                    transitions.append(
                        (
                            record.reference,
                            "apply" if record.status == "approved" else "finish_rollback",
                        )
                    )
                    continue
                key = self._key(record)
                prior = manifest["entries"].get(key)
                if record.status == "rollback_pending":
                    if prior and prior["id"] == record.id:
                        artifact = Path(prior["path"])
                        if (
                            not artifact.is_file()
                            or content_hash(artifact.read_bytes()) != prior["hash"]
                        ):
                            message = (
                                f"Rollback conflict for {record.reference}; manual edits preserved"
                            )
                            raise ValueError(message)
                        self._restore_prior(manifest, key, prior)
                    action = "finish_rollback"
                else:
                    try:
                        self._validate(record)
                    except (ValueError, OSError, TemplateError):
                        transitions.append((record.reference, "fail"))
                        continue
                    encoded = record.content.encode()
                    digest = content_hash(encoded)
                    suffix = "SKILL.md" if record.kind == "skill" else "content"
                    artifact = self.root / "versions" / record.id / str(record.revision) / suffix
                    if artifact.exists() and artifact.read_bytes() != encoded:
                        message = "Immutable artifact conflict; manual edits preserved"
                        raise ValueError(message)
                    if not artifact.exists():
                        publish(artifact, encoded)
                    manifest["entries"][key] = {
                        "id": record.id,
                        "reference": record.reference,
                        "scope": record.scope,
                        "project": record.project,
                        "kind": record.kind,
                        "target": record.target,
                        "path": str(artifact.resolve()),
                        "hash": digest,
                        "source_hash": record.source_hash,
                        "previous": prior,
                    }
                    action = "apply"
                manifest["operations"].append(record.reference)
                transitions.append((record.reference, action))
            publish(self.path, json.dumps(manifest, sort_keys=True).encode())
            for reference, action in transitions:
                self.ledger.transition(reference, action, _db=db)
        return self.capture()

    @staticmethod
    def _restore_prior(manifest: dict, key: str, prior: dict) -> None:
        previous = prior.get("previous")
        if previous:
            manifest["entries"][key] = previous
        else:
            manifest["entries"].pop(key)

    def capture(self) -> Any:
        """Freeze applicable artifact contents for one turn."""
        captured = []
        for entry in self.manifest()["entries"].values():
            if entry["scope"] != "shared" and entry["project"] != self.project:
                continue
            content = Path(entry["path"]).read_bytes()
            if content_hash(content) != entry["hash"]:
                message = f"Activation conflict for {entry['reference']}; manual edits preserved"
                raise ValueError(message)
            if entry["kind"] == "prompt":
                from novacode_cli.prompts import TEMPLATES_DIR

                source = TEMPLATES_DIR / entry["target"]
                if content_hash(source.read_bytes()) != entry.get("source_hash"):
                    message = "Activated prompt target changed; review a new revision"
                    raise ValueError(message)
            captured.append((entry["kind"], entry["target"], content.decode("utf-8")))
        return tuple(captured)
