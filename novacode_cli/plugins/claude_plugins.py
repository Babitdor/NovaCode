"""Standard directory plugins with scoped activation and immutable cached bundles."""

# ruff: noqa: ANN401, TRY004 -- heterogeneous JSON; schema failures use ValueError.

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from novacode_cli.plugins.components import Plugin, contained, object_file, safe_name
from novacode_cli.plugins.storage import locked, write_json

logger = logging.getLogger(__name__)
PLUGINS_DIR = Path.home() / ".nova" / "plugins"
MANIFEST = PLUGINS_DIR / "installed.json"
_SCOPES = ("user", "project", "local")
SESSION_PLUGINS: list[Path] = []


def _force_rmtree(path: Path) -> None:
    """Remove snapshots containing read-only Git pack files.

    Windows git packs are read-only,
    so a plain ``rmtree(ignore_errors=True)`` silently leaves ``.git`` behind.
    """
    if not path.exists():
        return

    def _onerror(func: Any, p: str, _exc: Any) -> None:
        Path(p).chmod(stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=_onerror)  # ponytail: 3.11 uses onerror (3.12+ onexc)


def _resolve_source(source: str) -> tuple[str, str]:
    """Return ``(kind, ref)`` — kind is "local" or "git", ref is the fetch ref."""
    if Path(source).expanduser().is_dir():
        return "local", str(Path(source).expanduser().resolve())
    if source.startswith(("http://", "https://", "git@", "ssh://")):
        return "git", source
    if re.fullmatch(r"[\w.-]+/[\w.-]+", source):  # owner/repo
        return "git", f"https://github.com/{source}"
    msg = f"Unrecognized plugin source: {source!r} (want a dir, git URL, or owner/repo)"
    raise ValueError(msg)


def _safe_name(name: str) -> str:
    return safe_name(name)


def _storage_path(root: Path, name: str) -> Path:
    destination = root / safe_name(name)
    if destination.is_symlink() or destination.resolve().parent != root.resolve():
        msg = "Plugin destination escapes storage directory"
        raise ValueError(msg)
    return destination


def project_root() -> Path:
    """Return the current Nova project for scope resolution."""
    from novacode_cli.config.config import settings

    return (settings.project_root or Path.cwd()).resolve()


def set_session_plugins(directories: list[str]) -> None:
    """Validate development bundles loaded in place, without installing them."""
    paths = [Path(directory).expanduser().resolve() for directory in directories]
    for path in paths:
        Plugin(path, path.name).validate()
    SESSION_PLUGINS[:] = paths


def _settings(scope: str, project: Path | None = None) -> Path:
    if scope not in _SCOPES:
        msg = "Expected user, project or local plugin scope"
        raise ValueError(msg)
    if scope == "user":
        return PLUGINS_DIR.parent / "settings.json"
    root = project or project_root()
    path = root / ".nova" / ("settings.local.json" if scope == "local" else "settings.json")
    if not path.resolve().is_relative_to(root.resolve()):
        message = "Plugin settings path escapes the selected project"
        raise ValueError(message)
    return path


def _load_manifest() -> dict[str, dict]:
    data = object_file(MANIFEST, optional=True)
    if any(not isinstance(meta, dict) for meta in data.values()):
        msg = "Malformed installed plugin registry"
        raise ValueError(msg)
    return data


def _save_manifest(data: dict[str, dict]) -> None:
    write_json(MANIFEST, data)


def _setting(identity: str, *, enabled: bool, scope: str) -> None:
    path = _settings(scope)
    data = object_file(path, optional=True)
    values = data.setdefault("enabledPlugins", {})
    if not isinstance(values, dict):
        msg = "enabledPlugins must be an object"
        raise ValueError(msg)
    values[identity] = enabled
    write_json(path, data)


def _active_record(identity: str, meta: dict) -> dict | None:
    records = meta.get("installations")
    if records is None:
        return {
            **meta,
            "scope": "user",
            "path": str(_storage_path(PLUGINS_DIR, identity)),
            "legacy": True,
        }
    if not isinstance(records, list):
        msg = "Malformed plugin installations"
        raise ValueError(msg)
    root = str(project_root())
    applicable = [
        r
        for r in records
        if isinstance(r, dict) and (r.get("scope") == "user" or r.get("project") == root)
    ]
    return max(applicable, key=lambda r: _SCOPES.index(r["scope"]), default=None)


def list_plugins() -> list[dict]:
    """List effective installations; settings precedence is local > project > user."""
    overrides = {}
    for scope in _SCOPES:
        settings = object_file(_settings(scope), optional=True)
        values = settings.get("enabledPlugins", {})
        if not isinstance(values, dict):
            msg = "enabledPlugins must be an object"
            raise ValueError(msg)
        overrides.update(values)
    result = []
    for identity, meta in _load_manifest().items():
        if meta.get("uninstalled"):
            continue
        record = _active_record(identity, meta)
        if not record:
            continue
        name = safe_name(meta.get("name", identity))
        path = contained(PLUGINS_DIR, os.path.relpath(record["path"], PLUGINS_DIR), exists=False)
        enabled = overrides.get(identity, record.get("enabled", meta.get("enabled", True))) is True
        result.append(
            {
                **meta,
                **record,
                "id": identity,
                "name": name,
                "path": str(path),
                "enabled": enabled and path.is_dir(),
            }
        )
    return result


def _lookup(identity: str) -> dict:
    matches = [p for p in list_plugins() if identity in {p["id"], p["name"]}]
    if len(matches) != 1:
        msg = f"Plugin {identity!r} is missing or ambiguous; use name@marketplace"
        raise ValueError(msg)
    return matches[0]


def fetch(source: str, dest: Path, *, ref: str | None = None) -> str:
    """Copy or clone only; no install scripts, recursive submodules or hooks."""
    kind, resolved = _resolve_source(source)
    if kind == "local":
        origin = Path(resolved).resolve()
        if dest.resolve().is_relative_to(origin):
            msg = "Cannot install a directory containing plugin storage"
            raise ValueError(msg)
        shutil.copytree(origin, dest, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    else:
        command = ["git", "-c", "core.hooksPath=", "clone", "--depth", "1"]
        if ref:
            if ref.startswith("-"):
                msg = "Invalid Git ref"
                raise ValueError(msg)
            command += ["--branch", ref]
        try:
            process = subprocess.run(  # noqa: S603 -- Git argv; no shell or install scripts.
                [*command, "--", resolved, str(dest)],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            message = "Plugin Git fetch timed out; the previous installation was preserved"
            raise RuntimeError(message) from exc
        if process.returncode:
            raise RuntimeError("Plugin Git fetch failed: " + process.stderr.strip())
        _force_rmtree(dest / ".git")
    for path in dest.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(dest.resolve()):
            msg = "Plugin contains a link outside its bundle"
            raise ValueError(msg)
    return resolved


def _bundle_digest(root: Path) -> str:
    """Hash framed paths, file contents, permissions and link targets."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if path.is_symlink():
            value = str(path.readlink())
        elif path.is_file():
            with path.open("rb") as stream:
                value = hashlib.file_digest(stream, "sha256").hexdigest()
        else:
            value = ""
        record = json.dumps(
            [path.relative_to(root).as_posix(), mode, value], ensure_ascii=True
        ).encode()
        digest.update(len(record).to_bytes(8, "big"))
        digest.update(record)
    return digest.hexdigest()[:24]


def install(
    source: str,
    *,
    scope: str = "user",
    marketplace: str | None = None,
    expected_name: str | None = None,
    ref: str | None = None,
    subdirectory: str | None = None,
    enable: bool | None = None,
    entry: dict | None = None,
) -> str:
    """Stage, validate, and publish a bundle without changing owner MCP/hooks."""
    _settings(scope)  # validate before fetching
    if marketplace:
        safe_name(marketplace)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=PLUGINS_DIR) as temporary:
        stage = Path(temporary) / "bundle"
        resolved = fetch(source, stage, ref=ref)
        root = contained(stage, subdirectory) if subdirectory else stage
        fallback = expected_name or Path(resolved.rstrip("/")).name.removesuffix(".git")
        plugin = Plugin(root, fallback, entry)
        diagnostics = plugin.validate()
        identity = safe_name(expected_name or plugin.name) + (
            "@" + marketplace if marketplace else ""
        )
        version_key = _bundle_digest(root)
        parent = PLUGINS_DIR / "cache" / (marketplace or "local") / plugin.name
        destination = contained(
            PLUGINS_DIR, str((parent / version_key).relative_to(PLUGINS_DIR)), exists=False
        )
        record = {
            "scope": scope,
            "project": str(project_root()) if scope != "user" else None,
            "path": str(destination),
            "version": plugin.manifest.get("version", version_key),
            "enabled": plugin.manifest.get("defaultEnabled", True) if enable is None else enable,
        }
        with locked(PLUGINS_DIR):
            data = _load_manifest()
            meta = data.get(identity, {})
            # Validate settings before publishing an installation.
            settings_path = _settings(scope)
            settings_data = object_file(settings_path, optional=True)
            settings_data.setdefault("enabledPlugins", {})
            if not isinstance(settings_data["enabledPlugins"], dict):
                msg = "enabledPlugins must be an object"
                raise ValueError(msg)
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                root.rename(destination)
            records = [
                r
                for r in meta.get("installations", [])
                if (r["scope"], r.get("project")) != (scope, record["project"])
            ]
            data[identity] = {
                "name": plugin.name,
                "source": resolved,
                "entry": entry,
                "marketplace": marketplace,
                "ref": ref,
                "subdirectory": subdirectory,
                "diagnostics": diagnostics,
                "installations": [*records, record],
                "legacy_path": meta.get(
                    "legacy_path",
                    str(PLUGINS_DIR / plugin.name)
                    if meta and "installations" not in meta
                    else None,
                ),
            }
            prior = settings_data["enabledPlugins"].get(identity)
            settings_data["enabledPlugins"][identity] = (
                prior if isinstance(prior, bool) and enable is None else record["enabled"]
            )
            previous_settings = object_file(settings_path, optional=True)
            write_json(settings_path, settings_data)
            try:
                _save_manifest(data)
            except OSError:
                write_json(settings_path, previous_settings)
                raise
    return identity


def set_enabled(identity: str, *, enabled: bool, scope: str | None = None) -> None:
    """Change activation without executing code or copying component config."""
    with locked(PLUGINS_DIR):
        item = _lookup(identity)
        if scope is None:
            scope = next(
                (
                    s
                    for s in reversed(_SCOPES)
                    if item["id"]
                    in object_file(_settings(s), optional=True).get("enabledPlugins", {})
                ),
                item["scope"],
            )
        _setting(item["id"], enabled=enabled, scope=scope)


def update(identity: str, *, scope: str | None = None) -> str:
    """Publish a new validated snapshot; preserve activation and prior cache."""
    item = _lookup(identity)
    if item.get("marketplace"):
        from novacode_cli.plugins.marketplaces import install_plugin

        return install_plugin(item["id"], scope=scope or item["scope"])
    return install(
        item["source"],
        scope=scope or item["scope"],
        marketplace=item.get("marketplace"),
        expected_name=item["name"],
        ref=item.get("ref"),
        subdirectory=item.get("subdirectory"),
    )


def remove(identity: str, *, scope: str = "user") -> bool:
    """Unregister the selected scope; retained snapshots protect active sessions."""
    _settings(scope)
    with locked(PLUGINS_DIR):
        try:
            item = _lookup(identity)
        except ValueError:
            return False
        data = _load_manifest()
        meta = data[item["id"]]
        records = meta.get("installations", [])
        kept = [
            r
            for r in records
            if (r["scope"], r.get("project"))
            != (scope, str(project_root()) if scope != "user" else None)
        ]
        if records and len(kept) == len(records):
            return False
        _setting(item["id"], enabled=False, scope=scope)
        if kept:
            meta["installations"] = kept
        elif item.get("legacy") or meta.get("legacy_path"):
            meta["uninstalled"] = True
        else:
            del data[item["id"]]
        _save_manifest(data)
    return True


def enabled_plugins() -> list[Plugin]:
    """Load enabled bundles independently so an invalid plugin is diagnosed."""
    out = []
    names = set()
    for directory in SESSION_PLUGINS:
        plugin = Plugin(directory, directory.name)
        names.add(plugin.name)
        out.append(plugin)
    for item in list_plugins():
        if not item["enabled"]:
            continue
        try:
            plugin = Plugin(Path(item["path"]), item["name"], item.get("entry"))
            if any(plugin.name == p.name for p in out if p.root in SESSION_PLUGINS):
                continue
            if plugin.name in names:
                msg = "Two enabled plugins have the same component namespace"
                raise ValueError(msg)  # noqa: TRY301 -- diagnosed per bundle below.
            names.add(plugin.name)
            out.append(plugin)
        except (OSError, ValueError) as exc:
            logger.warning("Plugin %s could not load: %s", item["id"], exc)
    return out


def _enabled_dirs() -> list[tuple[str, Path]]:
    return [(p.name, p.root) for p in enabled_plugins()]


def plugin_skill_dirs() -> list[tuple[str, Path]]:
    """Return distinct backend mount IDs and their skill scan directories."""
    return [(item["id"], item["directory"]) for item in plugin_skill_sources()]


def plugin_skill_sources() -> list[dict]:
    """Describe scan roots with an explicit allowlist of declared skill folders."""
    result = []
    for plugin in enabled_plugins():
        parents: dict[Path, set[str]] = {}
        for folder in plugin.skills():
            parents.setdefault(folder.parent, set()).add(folder.name)
        for index, (directory, allowed) in enumerate(parents.items()):
            source_id = plugin.name if index == 0 else f"{plugin.name}--{index}"
            result.append(
                {
                    "id": source_id,
                    "name": plugin.name,
                    "directory": directory,
                    "allowed": allowed,
                    "plugin_root": str(plugin.root),
                }
            )
    return result


def qualify_skills(skills: list, source: dict) -> list:
    """Filter manifest-selected folders and namespace metadata before deduplication."""
    result = []
    for skill in skills:
        folder = str(skill["path"]).replace("\\", "/").rstrip("/").split("/")[-2]
        if folder in source["allowed"]:
            result.append(
                {
                    **skill,
                    "name": f"{source['name']}:{folder}",
                    "plugin_root": source["plugin_root"],
                }
            )
    return result


def plugin_commands() -> list[tuple[str, str, str]]:
    """Read namespaced callable commands from enabled bundles."""
    return [command for p in enabled_plugins() for command in p.commands()]


def plugin_agent_specs() -> list[dict]:
    """Read namespaced agent definitions from enabled bundles."""
    return [agent for p in enabled_plugins() for agent in p.agents()]


def plugin_mcp_servers() -> dict[str, dict]:
    """Resolve enabled MCP components and their environment references."""

    def environment(value: Any) -> Any:
        if isinstance(value, str):

            def replace(match: re.Match[str]) -> str:
                name, _, default = match[1].partition(":-")
                if name not in os.environ and not default:
                    msg = f"Missing plugin environment variable: {name}"
                    raise ValueError(msg)
                return os.environ.get(name, default)

            return re.sub(r"\$\{([A-Za-z_][A-Za-z_0-9]*(?::-[^}]+)?)\}", replace, value)
        if isinstance(value, list):
            return [environment(item) for item in value]
        if isinstance(value, dict):
            return {key: environment(item) for key, item in value.items()}
        return value

    return {
        f"{p.name}__{name}": environment(value)
        for p in enabled_plugins()
        for name, value in p.mcp().items()
    }


def legacy_mcp_servers() -> dict[str, dict]:
    """Describe only the configurations the previous installer used to copy."""
    result = {}
    for identity, meta in _load_manifest().items():
        if "installations" not in meta or meta.get("legacy_path"):
            directory = (
                Path(meta["legacy_path"])
                if meta.get("legacy_path")
                else _storage_path(PLUGINS_DIR, identity)
            )
            if not directory.is_dir():
                continue
            for name, server in Plugin(directory, identity).mcp().items():
                result[f"{meta.get('name', identity)}__{name}"] = {
                    "transport": "stdio" if server.get("command") else "http",
                    "url": server.get("url"),
                    "command": server.get("command"),
                    "args": server.get("args", []),
                    "env": server.get("env", {}),
                }
    return result


def plugin_components(identity: str) -> dict[str, list[str]]:
    """Describe the effective bundle and its compatibility diagnostics."""
    item = _lookup(identity)
    plugin = Plugin(Path(item["path"]), item["name"], item.get("entry"))
    return {
        "skills": [f"{plugin.name}:{p.name}" for p in plugin.skills()],
        "commands": [n for n, _, _ in plugin.commands()],
        "agents": [a["name"] for a in plugin.agents()],
        "mcp": list(plugin.mcp()),
        "hooks": list({event for c in plugin.hook_configs() for event in c.get("hooks", {})}),
        "diagnostics": plugin.validate(),
    }


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    from novacode_cli.plugins.components import frontmatter

    return frontmatter(text)
