"""Read standard plugin components without executing or importing plugin code."""

# ruff: noqa: ANN401, TRY004 -- JSON schema errors use one ValueError contract.

from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

MAX_HOOK_TIMEOUT = 60


def object_file(path: Path, *, optional: bool = False) -> dict[str, Any]:
    """Read a JSON object; malformed files must never become empty configuration."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if optional:
            return {}
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        msg = f"Invalid JSON in {path.name}: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(data, dict):
        msg = f"Expected an object in {path.name}"
        raise ValueError(msg)
    return data


def safe_name(name: str) -> str:
    """Reject ambiguous IDs, traversal and Windows device destinations."""
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
        or ".." in name
        or name.endswith(".")
        or re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", name, re.IGNORECASE)
        or name in {"installed.json", "marketplaces.json", "cache"}
    ):
        msg = "Invalid plugin or marketplace name"
        raise ValueError(msg)
    return name


def contained(root: Path, value: str, *, exists: bool = True) -> Path:
    """Resolve only relative paths that remain inside their owning bundle."""
    if not isinstance(value, str) or Path(value).is_absolute() or PureWindowsPath(value).drive:
        msg = "Component path must be relative to the plugin root"
        raise ValueError(msg)
    if ".." in value.replace("\\", "/").split("/") or value.startswith("\\"):
        msg = "Component path escapes plugin root"
        raise ValueError(msg)
    path = root / value
    if not path.resolve().is_relative_to(root.resolve()):
        msg = "Component path escapes plugin root through a link"
        raise ValueError(msg)
    if exists and not path.exists():
        msg = f"Missing plugin component: {value}"
        raise ValueError(msg)
    return path


def frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse real YAML metadata, including block descriptions and tool lists."""
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\n|$)", text, re.DOTALL)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match[1]) or {}
    except yaml.YAMLError as exc:
        message = "Invalid YAML in plugin frontmatter"
        raise ValueError(message) from exc
    if not isinstance(data, dict):
        msg = "Plugin frontmatter must be a YAML object"
        raise ValueError(msg)
    return data, text[match.end() :].lstrip("\n")


class Plugin:
    """One validated bundle and its standard/manifest component locations."""

    def __init__(self, root: Path, fallback: str, entry: dict[str, Any] | None = None) -> None:
        """Open a bundle after checking its manifest and root boundaries."""
        self.root = root.resolve()
        if not self.root.is_dir():
            message = "Plugin directory does not exist"
            raise ValueError(message)
        if (root / ".claude-plugin/plugin.json").exists():
            contained(root, ".claude-plugin/plugin.json")
        self.manifest = object_file(root / ".claude-plugin/plugin.json", optional=True)
        if (root / ".claude-plugin/plugin.json").exists() and "name" not in self.manifest:
            msg = "A plugin manifest must declare its name"
            raise ValueError(msg)
        self.name = safe_name(self.manifest.get("name", fallback))
        self.entry = entry or {}
        self._merge_entry()
        for key in ("version", "description", "displayName"):
            if key in self.manifest and not isinstance(self.manifest[key], str):
                msg = f"Plugin {key} must be a string"
                raise ValueError(msg)
        if "defaultEnabled" in self.manifest and not isinstance(
            self.manifest["defaultEnabled"], bool
        ):
            msg = "defaultEnabled must be boolean"
            raise ValueError(msg)

    def _merge_entry(self) -> None:  # noqa: PLR0912 -- documented precedence for each component kind.
        """Combine catalog declarations using the documented strict/metadata rules."""
        if not self.entry:
            return
        component_keys = ("commands", "agents", "skills", "hooks", "outputStyles", "themes")
        has_manifest = (self.root / ".claude-plugin/plugin.json").is_file()
        if (
            has_manifest
            and self.entry.get("strict", True) is False
            and any(key in self.entry for key in component_keys)
        ):
            message = "Plugin has conflicting manifests (strict: false)"
            raise ValueError(message)
        if not has_manifest:
            self.manifest = dict(self.entry)
            return
        manifest = dict(self.manifest)
        for key in ("defaultEnabled", "displayName"):
            if key in self.entry:
                manifest[key] = self.entry[key]
        for key in ("version", "description"):
            if key not in manifest and key in self.entry:
                manifest[key] = self.entry[key]
        for key in ("commands", "agents", "skills"):
            if key not in self.entry:
                continue
            left = manifest.get(key, [])
            right = self.entry[key]
            if isinstance(left, dict) or isinstance(right, dict):
                if (left and not isinstance(left, dict)) or not isinstance(right, dict):
                    message = "Cannot combine path and inline command declarations"
                    raise ValueError(message)
                manifest[key] = {**(left or {}), **right}
                continue
            left = [left] if isinstance(left, str) else left
            right = [right] if isinstance(right, str) else right
            if not isinstance(left, list) or not isinstance(right, list):
                message = "Catalog component paths must be strings or lists"
                raise ValueError(message)
            default = self.root / key
            if key not in manifest and default.is_dir() and key != "skills":
                left = ["./" + key]
            manifest[key] = [*left, *right]
        self.manifest = manifest

    def paths(self, key: str, default: str, *, additive: bool = False) -> list[Path]:
        """Resolve explicit component paths, with each component's merge rules."""
        values = self.manifest.get(key, [])
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            msg = f"{key} must be a path or list of paths"
            raise ValueError(msg)
        result = [contained(self.root, value) for value in values]
        default_path = self.root / default
        if (additive or key not in self.manifest) and default_path.exists():
            contained(self.root, default)
            result.insert(0, default_path)
        return list(dict.fromkeys(result))

    def skills(self) -> list[Path]:
        """Find skill folders; explicit skill directories supplement defaults."""
        folders = []
        entry_only = (
            self.entry.get("source") in {".", "./"}
            if isinstance(self.entry.get("source"), str)
            else False
        )
        for path in self.paths(
            "skills", "skills", additive=not (entry_only and "skills" in self.entry)
        ):
            candidates = [path] if (path / "SKILL.md").is_file() else sorted(path.iterdir())
            for candidate in candidates:
                if (candidate / "SKILL.md").is_file():
                    contained(self.root, str(candidate.relative_to(self.root)))
                    contained(self.root, str((candidate / "SKILL.md").relative_to(self.root)))
                    folders.append(candidate)
        return list(dict.fromkeys(folders))

    def commands(self) -> list[tuple[str, str, str]]:  # noqa: PLR0912 -- standard manifest shapes and legacy TOML support.
        """Load namespaced legacy commands and skills as callable commands."""
        result = {}
        declared = self.manifest.get("commands")
        if isinstance(declared, dict):
            for name, entry in declared.items():
                safe_name(name)
                if not isinstance(entry, dict):
                    msg = "Inline command must declare source or content"
                    raise ValueError(msg)
                if "source" in entry:
                    path = contained(self.root, entry["source"])
                    fm, body = frontmatter(path.read_text(encoding="utf-8"))
                else:
                    fm, body = entry, entry.get("content", "")
                if not isinstance(body, str):
                    msg = "Command content must be a string"
                    raise ValueError(msg)
                result[name] = (str(fm.get("description", name)), body)
        else:
            for path in self.paths("commands", "commands"):
                files = (
                    sorted(path.rglob("*.md")) + sorted(path.rglob("*.toml"))
                    if path.is_dir()
                    else [path]
                )
                for file in files:
                    contained(self.root, str(file.relative_to(self.root)))
                    if file.suffix == ".toml":
                        import tomllib

                        data = tomllib.loads(file.read_text(encoding="utf-8"))
                        fm, body = data, data.get("prompt", "")
                    else:
                        fm, body = frontmatter(file.read_text(encoding="utf-8"))
                    result[file.stem] = (str(fm.get("description", file.stem)), body)
        for folder in self.skills():
            fm, body = frontmatter((folder / "SKILL.md").read_text(encoding="utf-8"))
            if fm.get("user-invocable", True) is not False:
                result[folder.name] = (str(fm.get("description", folder.name)), body)
        return [
            (f"{self.name}:{safe_name(name)}", desc, self.expand(body))
            for name, (desc, body) in result.items()
        ]

    def configs(self, key: str, default: str) -> list[dict[str, Any]]:
        """Merge default JSON plus inline objects and explicit JSON file paths."""
        result = []
        if (self.root / default).is_file():
            result.append(object_file(contained(self.root, default)))
        values = self.manifest.get(key, [])
        if (
            key == "mcpServers"
            and key in self.entry
            and (self.root / ".claude-plugin/plugin.json").is_file()
        ):
            entry_values = self.entry[key]
            values = (values if isinstance(values, list) else [values]) + (
                entry_values if isinstance(entry_values, list) else [entry_values]
            )
        if not isinstance(values, list):
            values = [values]
        for value in values:
            if isinstance(value, dict):
                result.append(value)
            elif isinstance(value, str) and value.endswith(".json"):
                path = contained(self.root, value)
                if path.resolve() != (self.root / default).resolve():
                    result.append(object_file(path))
            else:
                msg = f"Unsupported {key} component: expected JSON file or object"
                raise ValueError(msg)
        return result

    def mcp(self) -> dict[str, dict[str, Any]]:
        """Return server configuration while retaining environment placeholders."""
        out = {}
        for config in self.configs("mcpServers", ".mcp.json"):
            servers = config.get("mcpServers", config)
            if not isinstance(servers, dict):
                msg = "mcpServers must be an object"
                raise ValueError(msg)
            for name, value in servers.items():
                safe_name(name)
                if not isinstance(value, dict):
                    msg = "MCP server must be an object"
                    raise ValueError(msg)
                out[name] = self.expand(value)
        return out

    def hook_configs(self) -> list[dict[str, Any]]:
        """Read standard command hooks without dropping matchers or options."""
        configurations = self.configs("hooks", "hooks/hooks.json")
        if "hooks" not in self.entry:
            return configurations
        if not isinstance(self.entry["hooks"], dict):
            message = "Only inline hooks are supported in marketplace entries"
            raise ValueError(message)
        events: dict[str, list] = {}
        for config in configurations:
            for event, entries in config.get("hooks", {}).items():
                events.setdefault(event, []).extend(entries)
        events.update(self.entry["hooks"].get("hooks", {}))
        return [{"hooks": events}]

    def agents(self) -> list[dict[str, Any]]:
        """Read subagent definitions with component names scoped to the plugin."""
        result = []
        for path in self.paths("agents", "agents"):
            for file in sorted(path.glob("*.md")) if path.is_dir() else [path]:
                contained(self.root, str(file.relative_to(self.root)))
                text = file.read_text(encoding="utf-8")
                fm, body = frontmatter(text)
                name = safe_name(fm.get("name", file.stem))
                from novacode_cli.agents.agent_file import ALWAYS_INCLUDED, agent_tools

                declared_tools = fm.get("tools")
                if declared_tools is not None and not (
                    isinstance(declared_tools, str)
                    or (
                        isinstance(declared_tools, list)
                        and all(isinstance(tool, str) for tool in declared_tools)
                    )
                ):
                    message = "Agent tools must be a string or a list of strings"
                    raise ValueError(message)
                chosen = agent_tools(text)
                result.append(
                    {
                        "name": f"{self.name}:{name}",
                        "description": str(fm.get("description", name)),
                        "system_prompt": self.expand(body),
                        "tools": [],
                        **(
                            {
                                "_nova_tool_names": chosen,
                                "_nova_fs_allow": set(chosen) & set(ALWAYS_INCLUDED),
                            }
                            if chosen is not None
                            else {}
                        ),
                    }
                )
        return result

    def expand(self, value: Any) -> Any:
        """Replace plugin-root references structurally, never through a shell."""
        if isinstance(value, str):
            return value.replace("${CLAUDE_PLUGIN_ROOT}", str(self.root))
        if isinstance(value, list):
            return [self.expand(v) for v in value]
        if isinstance(value, dict):
            return {k: self.expand(v) for k, v in value.items()}
        return value

    def validate(self) -> list[str]:
        """Validate all supported paths and report host-specific components."""
        self.skills(), self.commands(), self.agents(), self.mcp()
        for configuration in self.hook_configs():
            _validate_hooks(configuration)
        unsupported = [
            key
            for key in (
                "lspServers",
                "channels",
                "workflows",
                "outputStyles",
                "userConfig",
                "dependencies",
            )
            if key in self.manifest
        ]
        if (self.root / ".lsp.json").exists():
            unsupported.append(".lsp.json")
        if (self.root / "hooks/hooks.js").exists():
            unsupported.append("JavaScript hooks module")
        return [f"Unsupported in Nova: {key}" for key in unsupported]


def _validate_hooks(configuration: dict[str, Any]) -> None:  # noqa: PLR0912 -- validate each protocol field before publication.
    """Reject malformed hook entries before the bundle can be enabled."""
    events = configuration.get("hooks")
    if not isinstance(events, dict):
        msg = "Plugin hooks must contain an events object"
        raise ValueError(msg)
    for entries in events.values():
        if not isinstance(entries, list):
            msg = "Hook event entries must be a list"
            raise ValueError(msg)
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                msg = "Hook entry must contain a hooks list"
                raise ValueError(msg)
            matcher = entry.get("matcher", "")
            if not isinstance(matcher, str):
                msg = "Hook matcher must be a string"
                raise ValueError(msg)
            if matcher not in {"", "*"}:
                try:
                    re.compile(matcher)
                except re.error as exc:
                    msg = "Invalid plugin hook matcher"
                    raise ValueError(msg) from exc
            for hook in entry["hooks"]:
                if not isinstance(hook, dict) or hook.get("type") != "command":
                    msg = "Nova currently supports command hooks only"
                    raise ValueError(msg)
                if not isinstance(hook.get("command"), str):
                    msg = "Hook command must be a string"
                    raise ValueError(msg)
                if "args" in hook and (
                    not isinstance(hook["args"], list)
                    or any(not isinstance(arg, str) for arg in hook["args"])
                ):
                    msg = "Hook args must be a string list"
                    raise ValueError(msg)
                timeout = hook.get("timeout", 10)
                if (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float))
                    or not 0 < timeout <= MAX_HOOK_TIMEOUT
                ):
                    msg = "Hook timeout must be between 0 and 60 seconds"
                    raise ValueError(msg)
