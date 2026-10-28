"""Claude-compatible plugin marketplaces.

A marketplace is a git repo with ``.claude-plugin/marketplace.json`` listing
plugins::

    {"name": "ponytail",
     "plugins": [{"name": "ponytail", "source": "./plugins/ponytail", "description": "..."}]}

    /plugins marketplace add DietrichGebert/ponytail
    /plugins search
    /plugins install ponytail@ponytail

This is a thin registry + source resolver on top of ``claude_plugins``: it
clones marketplaces and resolves a plugin's ``source`` to something
``claude_plugins.install`` already accepts (a local dir or a git ref).
"""

# ruff: noqa: TRY004 -- external catalog schema failures use ValueError.

from __future__ import annotations

from pathlib import Path

from novacode_cli.plugins import claude_plugins as cp
from novacode_cli.plugins.components import contained, object_file
from novacode_cli.plugins.storage import locked, write_json

MARKETPLACES_DIR = Path.home() / ".nova" / "marketplaces"
MANIFEST = MARKETPLACES_DIR / "marketplaces.json"


# ── manifest ────────────────────────────────────────────────────────────────
def _load() -> dict[str, dict]:
    return object_file(MANIFEST, optional=True)


def _save(data: dict[str, dict]) -> None:
    write_json(MANIFEST, data)


def _marketplace_json(directory: Path) -> dict:
    data = object_file(directory / ".claude-plugin/marketplace.json")
    cp._safe_name(data.get("name", ""))
    entries = data.get("plugins")
    if not isinstance(entries, list):
        msg = "Marketplace plugins must be a list"
        raise ValueError(msg)
    names = set()
    for entry in entries:
        if not isinstance(entry, dict):
            msg = "Marketplace plugin must be an object"
            raise ValueError(msg)
        name = cp._safe_name(entry.get("name", ""))
        if name in names:
            msg = "Duplicate marketplace plugin name"
            raise ValueError(msg)
        names.add(name)
    return data


def add(source: str) -> str:
    """Fetch and atomically publish a validated marketplace snapshot."""
    import tempfile

    MARKETPLACES_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=MARKETPLACES_DIR) as temporary:
        stage = Path(temporary) / "catalog"
        cp.fetch(source, stage)
        data = _marketplace_json(stage)
        name = data["name"]
        # A catalog's label cannot grant it an official trust tier.
        if name.startswith(("claude-", "anthropic-")) and not source.startswith(
            (
                "anthropics/",
                "https://github.com/anthropics/",
                "git@github.com:anthropics/",
            )
        ):
            msg = "Official marketplace names require an Anthropic GitHub source"
            raise ValueError(msg)
        with locked(MARKETPLACES_DIR):
            manifest = _load()
            if name in manifest and manifest[name]["source"] != source:
                msg = "Marketplace name already belongs to another source"
                raise ValueError(msg)
            # Version the catalog too; failed publication leaves the prior catalog intact.
            import uuid

            snapshot = MARKETPLACES_DIR / (name + "-" + uuid.uuid4().hex)
            stage.rename(snapshot)
            manifest[name] = {"source": source, "path": str(snapshot)}
            _save(manifest)
    return name


def list_marketplaces() -> list[dict]:
    """List the registered catalogs without fetching or executing code."""
    return [{"name": name, **meta} for name, meta in _load().items()]


def _directory(name: str) -> Path:
    meta = _load()[name]
    return contained(
        MARKETPLACES_DIR,
        str(Path(meta.get("path", MARKETPLACES_DIR / name)).relative_to(MARKETPLACES_DIR)),
    )


def update_marketplace(name: str) -> str:
    """Refresh one catalog while preserving its prior snapshot."""
    return add(_load()[name]["source"])


def remove_marketplace(name: str) -> bool:
    """Remove a catalog only after its installed plugins are unregistered."""
    with locked(MARKETPLACES_DIR):
        data = _load()
        if name not in data:
            return False
        installed = [p for p in cp.list_plugins() if p.get("marketplace") == name]
        if installed:
            msg = "Uninstall this marketplace's plugins before removing its catalog"
            raise ValueError(msg)
        del data[name]
        _save(data)
    return True


# ── plugin listing + install ──────────────────────────────────────────────────
def list_marketplace_plugins() -> list[dict]:
    """``[{marketplace, name, description}]`` across all registered marketplaces."""
    out: list[dict] = []
    for mname in _load():
        out.extend(
            {
                "marketplace": mname,
                "name": entry["name"],
                "description": entry.get("description", ""),
            }
            for entry in _marketplace_json(_directory(mname))["plugins"]
        )
    return out


def _resolve_entry_source(entry: dict, mkt_dir: Path) -> str:
    """Resolve only documented source forms; relative paths stay in the catalog."""
    source = entry.get("source")
    if isinstance(source, dict):
        kind = source.get("source")
        if kind == "github" and isinstance(source.get("repo"), str):
            return "https://github.com/" + source["repo"]
        if kind == "git" and isinstance(source.get("url"), str):
            return source["url"]
        msg = "Unsupported marketplace source type"
        raise ValueError(msg)
    if not isinstance(source, str) or (not source.startswith("./") and source != "."):
        msg = "Relative marketplace source must start with ./"
        raise ValueError(msg)
    return str(contained(mkt_dir, source))


def install_plugin(spec: str, *, scope: str = "user") -> str:
    """Install ``plugin@marketplace`` (or just ``plugin`` if unambiguous)."""
    if "@" in spec:
        plugin, marketplace = spec.split("@", 1)
    else:
        plugin, marketplace = spec, ""
    plugin, marketplace = plugin.strip(), marketplace.strip()

    matches = [
        p
        for p in list_marketplace_plugins()
        if p["name"] == plugin and (not marketplace or p["marketplace"] == marketplace)
    ]
    if not matches:
        where = f" in marketplace '{marketplace}'" if marketplace else ""
        msg = f"Plugin '{plugin}' not found{where}. Try /plugins search."
        raise ValueError(msg)
    if len(matches) > 1:
        opts = ", ".join(f"{m['name']}@{m['marketplace']}" for m in matches)
        msg = f"'{plugin}' is in multiple marketplaces — pick one: {opts}"
        raise ValueError(msg)

    hit = matches[0]
    mkt_dir = _directory(hit["marketplace"])
    entry = next(
        e for e in _marketplace_json(mkt_dir).get("plugins", []) if e.get("name") == plugin
    )
    source = entry.get("source")
    options = source if isinstance(source, dict) else {}
    return cp.install(
        _resolve_entry_source(entry, mkt_dir),
        scope=scope,
        marketplace=hit["marketplace"],
        expected_name=plugin,
        ref=options.get("ref"),
        subdirectory=options.get("path"),
        entry=entry,
    )
