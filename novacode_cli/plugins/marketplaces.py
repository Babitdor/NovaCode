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

from __future__ import annotations

import json
from pathlib import Path

from novacode_cli.plugins import claude_plugins as cp

MARKETPLACES_DIR = Path.home() / ".nova" / "marketplaces"
MANIFEST = MARKETPLACES_DIR / "marketplaces.json"


# ── manifest ────────────────────────────────────────────────────────────────
def _load() -> dict[str, dict]:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(m: dict[str, dict]) -> None:
    MARKETPLACES_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=2), encoding="utf-8")


def _marketplace_json(mkt_dir: Path) -> dict:
    try:
        return json.loads((mkt_dir / ".claude-plugin" / "marketplace.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ── add / list / remove ──────────────────────────────────────────────────────
def add(source: str) -> str:
    """Clone a marketplace repo; register it. Returns the marketplace name."""
    MARKETPLACES_DIR.mkdir(parents=True, exist_ok=True)
    staging = MARKETPLACES_DIR / ".staging"
    cp._force_rmtree(staging)

    ref = cp.fetch(source, staging)
    fallback = Path(ref.rstrip("/")).name.removesuffix(".git")
    name = _marketplace_json(staging).get("name") or fallback
    name = cp._safe_name(str(name))

    dest = cp._storage_path(MARKETPLACES_DIR, name)
    cp._force_rmtree(dest)
    staging.rename(dest)

    m = _load()
    m[name] = {"source": source}
    _save(m)
    return name


def list_marketplaces() -> list[dict]:
    return [{"name": n, **meta} for n, meta in _load().items()]


def remove_marketplace(name: str) -> bool:
    m = _load()
    if name not in m:
        return False
    cp._force_rmtree(cp._storage_path(MARKETPLACES_DIR, name))
    del m[name]
    _save(m)
    return True


# ── plugin listing + install ──────────────────────────────────────────────────
def list_marketplace_plugins() -> list[dict]:
    """``[{marketplace, name, description}]`` across all registered marketplaces."""
    out = []
    for mname in _load():
        for entry in _marketplace_json(MARKETPLACES_DIR / mname).get("plugins", []):
            if isinstance(entry, dict) and entry.get("name"):
                out.append(
                    {
                        "marketplace": mname,
                        "name": entry["name"],
                        "description": entry.get("description", ""),
                    }
                )
    return out


def _resolve_entry_source(entry: dict, mkt_dir: Path) -> str:
    """Resolve a plugin entry's ``source`` to a dir/ref ``cp.install`` accepts."""
    s = entry.get("source")
    if s is None:  # convention: a subdir named after the plugin
        return str(mkt_dir / entry["name"])
    if isinstance(s, dict):
        # ponytail: only the common {"source":"github","repo":"o/r"} form.
        repo = s.get("repo")
        if repo:
            return str(repo)
        msg = f"Unsupported plugin source object: {s}"
        raise ValueError(msg)
    s = str(s)
    local = (mkt_dir / s)
    if s.startswith((".", "/")) or local.exists():  # relative path inside the marketplace
        return str(local.resolve())
    return s  # a git URL / owner/repo — install() handles it


def install_plugin(spec: str) -> str:
    """Install ``plugin@marketplace`` (or just ``plugin`` if unambiguous)."""
    if "@" in spec:
        plugin, marketplace = spec.split("@", 1)
    else:
        plugin, marketplace = spec, ""
    plugin, marketplace = plugin.strip(), marketplace.strip()

    matches = [
        p for p in list_marketplace_plugins()
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
    mkt_dir = MARKETPLACES_DIR / hit["marketplace"]
    entry = next(
        e for e in _marketplace_json(mkt_dir).get("plugins", []) if e.get("name") == plugin
    )
    return cp.install(_resolve_entry_source(entry, mkt_dir))


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        mkt = Path(tmp) / "mkt"
        (mkt / ".claude-plugin").mkdir(parents=True)
        (mkt / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"name": "m", "plugins": [{"name": "foo", "source": "./foo"}]})
        )
        assert _marketplace_json(mkt)["name"] == "m"
        entry = {"name": "foo", "source": "./foo"}
        assert _resolve_entry_source(entry, mkt) == str((mkt / "./foo").resolve())
        assert _resolve_entry_source({"name": "bar"}, mkt) == str(mkt / "bar")
        assert _resolve_entry_source({"name": "b", "source": {"repo": "o/r"}}, mkt) == "o/r"
    print("marketplaces self-check ok")
