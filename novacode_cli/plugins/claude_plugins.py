"""Claude-compatible plugin installer.

A plugin is a directory (usually a git repo) laid out the Claude Code way::

    .claude-plugin/plugin.json   # {"name", "description", "version"}
    skills/<name>/SKILL.md       # skills   -> mounted for the agent
    .mcp.json                    # MCP servers -> merged into Nova's MCP config
    commands/*.md  agents/*.md  hooks/hooks.json   # (loaders: next pass)

Install clones/copies the plugin under ``~/.nova/plugins/<name>/`` and records
it in ``installed.json``. Skills are wired at agent build (see
``plugin_skill_dirs``); MCP servers are merged into Nova's persisted MCP config
at install time (existing MCP load picks them up next start).

Commands / agents / hooks are discovered (``plugin_components``) but not yet
loaded — Nova has no markdown-command/agent loader today. ponytail: wire those
when the md-loaders land; skills+MCP cover the common plugin.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

PLUGINS_DIR = Path.home() / ".nova" / "plugins"
MANIFEST = PLUGINS_DIR / "installed.json"


def _force_rmtree(path: Path) -> None:
    """rmtree that also deletes read-only files — Windows git packs are read-only,
    so a plain ``rmtree(ignore_errors=True)`` silently leaves ``.git`` behind."""
    if not path.exists():
        return

    def _onerror(func, p, _exc):  # noqa: ANN001, ANN202
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=_onerror)  # ponytail: 3.11 uses onerror (3.12+ onexc)


# ── manifest ────────────────────────────────────────────────────────────────
def _load_manifest() -> dict[str, dict]:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_manifest(m: dict[str, dict]) -> None:
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=2), encoding="utf-8")


def list_plugins() -> list[dict]:
    """Installed plugins: ``[{name, source, enabled, path}]``."""
    out = []
    for name, meta in _load_manifest().items():
        out.append({"name": name, "path": str(PLUGINS_DIR / name), **meta})
    return out


# ── install / remove ─────────────────────────────────────────────────────────
def _resolve_source(source: str) -> tuple[str, str]:
    """Return ``(kind, ref)`` — kind is "local" or "git", ref is the fetch ref."""
    if Path(source).expanduser().is_dir():
        return "local", str(Path(source).expanduser())
    if source.startswith(("http://", "https://", "git@", "ssh://")):
        return "git", source
    if re.fullmatch(r"[\w.-]+/[\w.-]+", source):  # owner/repo
        return "git", f"https://github.com/{source}"
    msg = f"Unrecognized plugin source: {source!r} (want a dir, git URL, or owner/repo)"
    raise ValueError(msg)


def _plugin_name(plugin_dir: Path, fallback: str) -> str:
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    try:
        name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
        if name:
            return _safe_name(str(name))
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return _safe_name(fallback)


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.-]", "-", name)
    if cleaned.casefold() in {"", ".", "..", ".staging", "installed.json", "marketplaces.json"} or cleaned.endswith(".") or re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", cleaned, re.I):
        raise ValueError("Invalid plugin or marketplace name")
    return cleaned


def _storage_path(root: Path, name: str) -> Path:
    """Validate destructive destinations, including existing symlink targets."""
    if _safe_name(name) != name:
        raise ValueError("Invalid stored plugin name")
    dest = root / name
    if dest.resolve().parent != root.resolve() or dest.is_symlink():
        raise ValueError("Plugin destination escapes storage directory")
    return dest


def fetch(source: str, dest: Path) -> str:
    """Clone (git) or copy (local) ``source`` into ``dest``; return the fetch ref.

    Shared by plugin install and marketplace add. ``dest`` must not exist.
    """
    kind, ref = _resolve_source(source)
    if kind == "local":
        shutil.copytree(ref, dest)
    else:
        # ponytail: shallow clone, no submodules — plugins are self-contained.
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", ref, str(dest)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            _force_rmtree(dest)
            msg = f"git clone failed: {proc.stderr.strip() or proc.stdout.strip()}"
            raise RuntimeError(msg)
    _force_rmtree(dest / ".git")  # plugins are snapshots — drop VCS metadata
    return ref


def install(source: str) -> str:
    """Install a plugin from a local dir, git URL, or ``owner/repo``. Returns name."""
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    staging = PLUGINS_DIR / ".staging"
    _force_rmtree(staging)

    ref = fetch(source, staging)
    fallback = Path(ref.rstrip("/")).name.removesuffix(".git")
    name = _plugin_name(staging, fallback)
    dest = _storage_path(PLUGINS_DIR, name)
    _force_rmtree(dest)
    staging.rename(dest)

    m = _load_manifest()
    m[name] = {"source": source, "enabled": True}
    _save_manifest(m)
    _apply_mcp(name)
    _apply_hooks(name)
    return name


def remove(name: str) -> bool:
    """Uninstall a plugin: drop its MCP servers, dir, and manifest entry."""
    m = _load_manifest()
    if name not in m:
        return False
    destination = _storage_path(PLUGINS_DIR, name)
    _remove_mcp(name)
    _remove_hooks(name)
    _force_rmtree(destination)
    del m[name]
    _save_manifest(m)
    return True


# ── component discovery ──────────────────────────────────────────────────────
def _enabled_dirs() -> list[tuple[str, Path]]:
    return [
        (name, PLUGINS_DIR / name)
        for name, meta in _load_manifest().items()
        if meta.get("enabled", True) and (PLUGINS_DIR / name).is_dir()
    ]


def plugin_skill_dirs() -> list[tuple[str, Path]]:
    """``(plugin_name, skills_dir)`` for enabled plugins that ship skills.

    Consumed by core_agent to mount each as a ``/plugin-skills-<name>/`` route.
    """
    return [(n, d / "skills") for n, d in _enabled_dirs() if (d / "skills").is_dir()]


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``---`` YAML-ish frontmatter (flat key: value) from the body."""
    fm: dict[str, str] = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip().strip("\"'")
            return fm, text[end + 4:].lstrip("\n")
    return fm, text


def _command_files(cmd_dir: Path):
    yield from sorted(cmd_dir.glob("*.md"))
    yield from sorted(cmd_dir.glob("*.toml"))  # Codex command format


def _read_command(f: Path) -> tuple[str, str]:
    """Return ``(description, prompt_body)`` for a .md or .toml command file."""
    text = f.read_text(encoding="utf-8", errors="replace")
    if f.suffix == ".toml":
        import tomllib

        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return f"Plugin command /{f.stem}", ""
        return str(data.get("description", f"Plugin command /{f.stem}")), str(data.get("prompt", ""))
    fm, body = _parse_frontmatter(text)
    return fm.get("description", f"Plugin command /{f.stem}"), body


def plugin_commands() -> list[tuple[str, str, str]]:
    """``(command_name, description, body)`` for enabled plugins' commands.

    Supports Claude ``*.md`` and Codex ``*.toml`` commands. At call time the
    body's ``$ARGUMENTS`` (Claude) and ``{{args}}`` (Codex) are replaced with
    the invocation args (see the command registrar).
    """
    out = []
    for _name, d in _enabled_dirs():
        for f in _command_files(d / "commands"):
            desc, body = _read_command(f)
            out.append((f.stem, desc, body))
    return out


def plugin_agent_specs() -> list[dict]:
    """SubAgent specs parsed from enabled plugins' agents/*.md."""
    specs = []
    for _name, d in _enabled_dirs():
        for f in sorted((d / "agents").glob("*.md")):
            fm, body = _parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            specs.append(
                {
                    "name": fm.get("name", f.stem),
                    "description": fm.get("description", f"Plugin agent {f.stem}"),
                    # deepagents' subagent specs key the prompt as "system_prompt"
                    # (see core_agent build_named_subagents); "prompt" KeyErrors.
                    "system_prompt": body,
                    "tools": [],  # ponytail: default toolset; map Claude tool names if needed
                }
            )
    return specs


def plugin_components(name: str) -> dict[str, list[str]]:
    """What a plugin provides (for `/plugins` display); deferred loaders noted."""
    d = PLUGINS_DIR / name
    return {
        "skills": [p.name for p in (d / "skills").glob("*") if p.is_dir()],
        "commands": [f.stem for f in _command_files(d / "commands")],
        "agents": [p.name for p in (d / "agents").glob("*.md")],
        "mcp": list(_read_mcp_servers(d)),
        "hooks": [f.name for f in _hook_files(d)],
    }


# ── MCP wiring (merge into Nova's persisted config) ───────────────────────────
def _read_mcp_servers(plugin_dir: Path) -> dict[str, dict]:
    f = plugin_dir / ".mcp.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data.get("mcpServers", data) if isinstance(data, dict) else {}


def _prefixed(name: str, server: str) -> str:
    return f"{name}__{server}"  # namespace so plugins can't clobber each other


def _apply_mcp(name: str) -> None:
    servers = _read_mcp_servers(PLUGINS_DIR / name)
    if not servers:
        return
    from novacode_cli.mcp.config import MCPConfig, MCPServerConfig

    cfg = MCPConfig()
    for sname, s in servers.items():
        if not isinstance(s, dict):
            continue
        transport = "stdio" if s.get("command") else "http"
        try:
            cfg.add_server(
                _prefixed(name, sname),
                MCPServerConfig(
                    transport=transport,
                    url=s.get("url"),
                    command=s.get("command"),
                    args=s.get("args", []),
                    env=s.get("env", {}),
                ),
            )
        except Exception:  # noqa: BLE001 — a bad server entry shouldn't fail install
            continue


def _remove_mcp(name: str) -> None:
    servers = _read_mcp_servers(PLUGINS_DIR / name)
    if not servers:
        return
    from novacode_cli.mcp.config import MCPConfig

    cfg = MCPConfig()
    for sname in servers:
        try:
            cfg.remove_server(_prefixed(name, sname))
        except Exception:  # noqa: BLE001
            continue


# ── hooks wiring (translate Claude hooks → Nova ~/.nova/hooks.json) ───────────
# Claude events → Nova events. Unmapped Claude events (Notification, etc.) are
# skipped. ponytail: matchers are dropped — Nova hooks fire per-event, not
# per-tool; add matcher support if a plugin needs tool-scoped hooks.
_CLAUDE_TO_NOVA_EVENT = {
    "PreToolUse": "tool.call",
    "PostToolUse": "tool.result",
    "UserPromptSubmit": "user.message",
    "SessionStart": "session.start",
    "SessionEnd": "session.end",
    "Stop": "agent.message",
}


def _hook_files(plugin_dir: Path) -> list[Path]:
    """Claude-schema hook files (Nova convention + Codex-bundled name)."""
    return [
        p
        for p in (plugin_dir / "hooks" / "hooks.json", plugin_dir / "hooks" / "claude-codex-hooks.json")
        if p.is_file()
    ]


def _plugin_hooks(plugin_dir: Path, name: str) -> list[dict]:
    """Translate a plugin's Claude-schema hooks into Nova hook entries, tagged.

    ponytail: best-effort — ``${CLAUDE_PLUGIN_ROOT}`` is substituted with the
    plugin dir and the Windows variant is preferred on nt, but these are
    Claude-runtime hooks (often node scripts / events Nova doesn't emit), so
    unmapped events are skipped and a missing interpreter just no-ops.
    """
    import os

    root = str(plugin_dir)
    out = []
    for hf in _hook_files(plugin_dir):
        try:
            data = json.loads(hf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
        for cevent, entries in (hooks.items() if isinstance(hooks, dict) else []):
            nova_ev = _CLAUDE_TO_NOVA_EVENT.get(cevent)
            if not nova_ev:
                continue
            for entry in entries or []:
                for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
                    if h.get("type") != "command":
                        continue
                    if os.name == "nt" and h.get("commandWindows"):
                        cmd = h["commandWindows"].replace("$env:CLAUDE_PLUGIN_ROOT", root)
                        wrapped = ["powershell", "-NoProfile", "-Command", cmd]
                    elif h.get("command"):
                        cmd = h["command"].replace("${CLAUDE_PLUGIN_ROOT}", root)
                        wrapped = ["bash", "-c", cmd]
                    else:
                        continue
                    out.append({"command": wrapped, "events": [nova_ev], "_plugin": name})
    return out


def _apply_hooks(name: str) -> None:
    new = _plugin_hooks(PLUGINS_DIR / name, name)
    if not new:
        return
    from novacode_cli.hooks import HOOKS_FILE

    try:
        cfg = json.loads(HOOKS_FILE.read_text(encoding="utf-8")) if HOOKS_FILE.exists() else {}
    except (OSError, json.JSONDecodeError):
        cfg = {}
    cfg.setdefault("hooks", [])
    cfg["hooks"] = [h for h in cfg["hooks"] if h.get("_plugin") != name] + new
    HOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOOKS_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _remove_hooks(name: str) -> None:
    from novacode_cli.hooks import HOOKS_FILE

    if not HOOKS_FILE.exists():
        return
    try:
        cfg = json.loads(HOOKS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    cfg["hooks"] = [h for h in cfg.get("hooks", []) if h.get("_plugin") != name]
    HOOKS_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


if __name__ == "__main__":
    # ponytail: self-check with a local dummy plugin (no git, no MCP config write).
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "demo-plugin"
        (src / ".claude-plugin").mkdir(parents=True)
        (src / ".claude-plugin" / "plugin.json").write_text('{"name": "demo"}')
        (src / "skills" / "hello").mkdir(parents=True)
        (src / "skills" / "hello" / "SKILL.md").write_text("# hello")
        assert _resolve_source(str(src)) == ("local", str(src))
        assert _resolve_source("owner/repo") == ("git", "https://github.com/owner/repo")
        assert _plugin_name(src, "x") == "demo"

        fm, body = _parse_frontmatter("---\nname: r\ndescription: d\n---\nBODY $ARGUMENTS")
        assert fm == {"name": "r", "description": "d"} and body == "BODY $ARGUMENTS"

        (src / "commands").mkdir()
        (src / "commands" / "hi.toml").write_text('description = "d"\nprompt = "run {{args}}"')
        assert _read_command(src / "commands" / "hi.toml") == ("d", "run {{args}}")

        claude_hooks = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
        (src / "hooks").mkdir()
        (src / "hooks" / "hooks.json").write_text(json.dumps(claude_hooks))
        h = _plugin_hooks(src, "demo")
        assert h == [{"command": ["bash", "-c", "echo hi"], "events": ["tool.call"], "_plugin": "demo"}]
    print("claude_plugins self-check ok")
