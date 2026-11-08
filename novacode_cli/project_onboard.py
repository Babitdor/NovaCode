"""Workspace-local structural onboarding, using the installed Graphify parsers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

MAX_FILES = 5000
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_GRAPH_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_NODES = 40
MAX_EVIDENCE_NODES_PER_FILE = 2
PARSER_TIMEOUT_SECONDS = 120
MAX_PARSER_TIMEOUT_SECONDS = 600
_SKIP = frozenset(
    {
        ".git",
        ".nova",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
        "vendor",
        "graphify-out",
        "target",
    }
)
_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".scala",
        ".sh",
        ".ps1",
        ".sql",
        ".lua",
        ".ex",
        ".exs",
        ".vue",
        ".svelte",
        ".dart",
        ".tf",
        ".md",
        ".toml",
    }
)
_WORKER = """
import json, pathlib, sys
from graphify.extract import extract
root, output = map(pathlib.Path, sys.argv[1:])
root = root.resolve()
paths = [root / p for p in json.loads((root / 'inputs.json').read_text())]
result = extract(paths, cache_root=root, parallel=False)
output.write_text(json.dumps(result), encoding='utf-8')
"""


def _is_link(path: Path) -> bool:
    """Include Windows junctions/reparse points on Python 3.11."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def storage(root: Path) -> Path:
    """Reject redirects through generated storage before reading or writing it."""
    directory = root / ".nova" / "onboard"
    for path in (root / ".nova", directory):
        if _is_link(path):
            msg = f"Onboarding storage cannot contain links: {path}"
            raise ValueError(msg)
    for path in directory.glob("*"):
        if _is_link(path):
            msg = f"Onboarding storage cannot contain links: {path}"
            raise ValueError(msg)
    return directory


def source_files(root: Path) -> list[Path]:
    """Select bounded, non-secret source files, respecting Git's ignore rules."""
    try:
        result = subprocess.run(  # noqa: S603 - Fixed Git operation; no shell.
            [
                shutil.which("git") or "git",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                ".",
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        candidates = [root / os.fsdecode(name) for name in result.stdout.split(b"\0") if name]
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        if (root / ".git").exists() or (
            isinstance(exc, subprocess.CalledProcessError)
            and b"not a git repository" not in exc.stderr.lower()
        ):
            msg = "Cannot enumerate Git files safely; check Git access before onboarding."
            raise ValueError(msg) from exc
        candidates = []
        for parent, directories, names in os.walk(root, followlinks=False):
            directories[:] = [
                name
                for name in directories
                if name not in _SKIP and not _is_link(Path(parent) / name)
            ]
            candidates.extend(Path(parent) / name for name in names)
    selected = []
    for path in sorted(set(candidates)):
        relative = path.relative_to(root)
        if (
            any(part in _SKIP or part.startswith(".env") for part in relative.parts)
            or path.suffix.lower() not in _EXTENSIONS
            or any(word in path.name.lower() for word in ("secret", "credential", "private_key"))
        ):
            continue
        if not path.resolve().is_relative_to(root) or _is_link(path):
            continue
        if any(
            _is_link(parent)
            for parent in path.parents
            if parent != root and parent.is_relative_to(root)
        ):
            continue
        if path.is_file():
            selected.append(path)
        if len(selected) > MAX_FILES:
            msg = (
                f"Project exceeds the {MAX_FILES}-file onboarding limit. "
                "Select a smaller workspace."
            )
            raise ValueError(msg)
    return selected


def fingerprints(root: Path) -> dict[str, str]:
    """Hash selected content so dirty, new and deleted files all invalidate analysis."""
    hashes = {}
    total = 0
    for path in source_files(root):
        with path.open("rb") as stream:
            content = stream.read(MAX_FILE_BYTES + 1)
        size = len(content)
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            msg = (
                "Onboarding input exceeds 1 MiB per file or 64 MiB total; "
                "select a smaller workspace."
            )
            raise ValueError(msg)
        hashes[path.relative_to(root).as_posix()] = hashlib.sha256(content).hexdigest()
    return hashes


def load_graph(root: Path) -> dict[str, Any]:
    """Load only this workspace's supported graph, with explicit corruption errors."""
    path = storage(root) / "graph.json"
    if path.stat().st_size > MAX_GRAPH_BYTES:
        msg = "Saved onboarding graph exceeds the 32 MiB limit."
        raise ValueError(msg)
    graph = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(graph, dict)
        or graph.get("version") != 1
        or not isinstance(graph.get("nodes"), list)
        or not isinstance(graph.get("edges"), list)
        or not isinstance(graph.get("fingerprints"), dict)
        or not all(isinstance(graph.get(key), list) for key in ("layers", "tour", "hotspots"))
        or not all(isinstance(graph.get(key), str) for key in ("project", "analyzed_at", "limits"))
    ):
        msg = "Invalid onboarding graph. Run /onboard refresh to rebuild it."
        raise ValueError(msg)
    ids = set()
    for node in graph["nodes"]:
        if (
            not isinstance(node, dict)
            or not all(
                isinstance(node.get(key), str) for key in ("id", "name", "path", "kind", "line")
            )
            or node["path"] not in graph["fingerprints"]
            or node["id"] in ids
        ):
            msg = "Invalid onboarding node. Run /onboard refresh to rebuild it."
            raise ValueError(msg)
        ids.add(node["id"])
    for edge in graph["edges"]:
        if (
            not isinstance(edge, dict)
            or edge.get("source") not in ids
            or edge.get("target") not in ids
            or not isinstance(edge.get("type"), str)
        ):
            msg = "Invalid onboarding relationship. Run /onboard refresh to rebuild it."
            raise ValueError(msg)
    return graph


def changed_files(root: Path, graph: dict[str, Any]) -> list[str]:
    """Compare content rather than relying on commits or file timestamps."""
    current, previous = fingerprints(root), graph["fingerprints"]
    return sorted(
        name for name in current.keys() | previous.keys() if current.get(name) != previous.get(name)
    )


def _atomic_write(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def parsing_deadline(file_count: int, input_bytes: int) -> float:
    """Allow 0.5 seconds/file or 10 seconds/MiB, bounded to two through ten minutes."""
    return min(
        MAX_PARSER_TIMEOUT_SECONDS,
        max(PARSER_TIMEOUT_SECONDS, file_count * 0.5, input_bytes / 1024**2 * 10),
    )


async def analyze(root: Path) -> dict[str, Any]:
    """Parse an isolated snapshot; kill extraction on timeout or cancellation."""
    root = await asyncio.to_thread(root.resolve)
    storage(root)
    hashes = await asyncio.to_thread(fingerprints, root)
    if not hashes:
        msg = "No supported source or Markdown files found in this workspace."
        raise ValueError(msg)
    with tempfile.TemporaryDirectory(prefix="nova-onboard-") as temporary:
        snapshot = Path(temporary)
        input_bytes = 0
        for name, expected in hashes.items():
            content = (root / name).read_bytes()
            if hashlib.sha256(content).hexdigest() != expected:
                msg = "Workspace changed during onboarding; run /onboard refresh again."
                raise ValueError(msg)
            input_bytes += len(content)
            destination = snapshot / "source" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        source = snapshot / "source"
        (source / "inputs.json").write_text(json.dumps(list(hashes)), encoding="utf-8")
        output = snapshot / "extraction.json"
        deadline = parsing_deadline(len(hashes), input_bytes)
        environment = {**os.environ, "GRAPHIFY_OUT": str(snapshot / "cache")}
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            _WORKER,
            str(source),
            str(output),
            cwd=temporary,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=deadline)
        except TimeoutError as exc:
            msg = (
                f"Graphify exceeded its {deadline:g}-second parsing deadline "
                f"for {len(hashes)} files; "
                "saved analysis was preserved."
            )
            raise TimeoutError(msg) from exc
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode or not output.is_file():
            msg = "Graphify extraction failed; previous onboarding data was preserved."
            raise ValueError(msg)
        if output.stat().st_size > MAX_GRAPH_BYTES:
            msg = "Extracted graph exceeds the 32 MiB limit; select a smaller workspace."
            raise ValueError(msg)
        extracted = json.loads(output.read_text(encoding="utf-8"))
    graph = _build_graph(root, hashes, extracted)
    if await asyncio.to_thread(fingerprints, root) != hashes:
        msg = "Workspace changed during extraction; previous onboarding data was preserved."
        raise ValueError(msg)
    await asyncio.to_thread(_save_graph, root, graph)
    return graph


def _save_graph(root: Path, graph: dict[str, Any]) -> None:
    content = json.dumps(graph, ensure_ascii=True)
    if len(content.encode("utf-8")) > MAX_GRAPH_BYTES:
        msg = "Onboarding graph exceeds the 32 MiB limit; select a smaller workspace."
        raise ValueError(msg)
    directory = storage(root)
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_write(directory / "graph.json", content)
    _atomic_write(directory / "guide.md", guide(graph))


def _build_graph(root: Path, hashes: dict[str, str], extracted: dict[str, Any]) -> dict[str, Any]:
    """Normalize parser evidence into a portable graph and structural learning path."""
    nodes = []
    aliases = {}
    for node in extracted["nodes"]:
        location = str(node.get("source_file", "")).replace("\\", "/")
        if location not in hashes:
            continue
        name = str(node.get("label", node["id"]))
        is_file = name == Path(location).name
        identity = "file:" + location if is_file else str(node["id"])
        aliases[node["id"]] = identity
        nodes.append(
            {
                "id": identity,
                "name": location if is_file else name,
                "path": location,
                "line": str(node.get("source_location") or ""),
                "kind": "file" if is_file else str(node.get("file_type", "symbol")),
            }
        )
    ids = {node["id"] for node in nodes}
    edges = [
        {
            "source": aliases[edge["source"]],
            "target": aliases[edge["target"]],
            "type": edge.get("relation", "depends_on"),
        }
        for edge in extracted["edges"]
        if edge.get("source") in aliases and edge.get("target") in aliases
    ]
    # Every input remains visible, including languages without structural extraction.
    for name in hashes:
        identity = "file:" + name
        if identity not in ids:
            nodes.append({"id": identity, "name": name, "path": name, "line": "", "kind": "file"})
    edges.extend(
        {"source": "file:" + node["path"], "target": node["id"], "type": "contains"}
        for node in nodes
        if node["kind"] != "file"
    )
    edges = [
        dict(zip(("source", "target", "type"), edge, strict=True))
        for edge in dict.fromkeys((edge["source"], edge["target"], edge["type"]) for edge in edges)
    ]
    degree = Counter(
        edge[key] for edge in edges if edge["type"] != "contains" for key in ("source", "target")
    )
    hotspots = sorted(nodes, key=lambda node: (-degree[node["id"]], node["path"]))[:12]
    layers = sorted({name.split("/")[0] if "/" in name else "Project root" for name in hashes})
    tour_paths = list(
        dict.fromkeys(
            [
                name
                for name in hashes
                if Path(name).stem.lower() in {"readme", "main", "app", "index"}
            ]
            + [node["path"] for node in hotspots]
        )
    )[:12]
    return {
        "version": 1,
        "project": root.name,
        "analyzed_at": datetime.now(UTC).isoformat(),
        "fingerprints": hashes,
        "nodes": nodes,
        "edges": edges,
        "layers": layers,
        "tour": tour_paths,
        "hotspots": [node["id"] for node in hotspots],
        "limits": (
            "Static parser evidence; dynamic dispatch and business meaning need source review."
        ),
    }


def guide(graph: dict[str, Any]) -> str:
    """Create a usable structural guide before optional model explanation."""
    return "\n".join(
        [
            f"# {graph['project']} — onboarding",
            "",
            f"Analyzed: {graph['analyzed_at']}",
            f"{len(graph['fingerprints'])} files · "
            f"{len(graph['nodes'])} nodes · {len(graph['edges'])} edges",
            "",
            "## Architecture map",
            *[f"- {layer}" for layer in graph["layers"]],
            "",
            "These are directory groups, not verified semantic architecture layers.",
            "",
            "## Guided tour",
            *[f"{i}. `{name}`" for i, name in enumerate(graph["tour"], 1)],
            "",
            "## Explore",
            "Use the dashboard search, select a node to inspect relationships,",
            "and use Impact to trace incoming dependencies. Ask Nova `/onboard ask <question>`.",
            "Use `/onboard impact <symbol or path>` before a change "
            "and `/onboard refresh` after edits.",
            "",
            "## Evidence limits",
            graph["limits"],
            "",
        ]
    )


def select_nodes(graph: dict[str, Any], query: str) -> list[dict[str, Any]]:
    """Select diverse source evidence instead of a parser's first document sections."""
    terms = query.lower().split()
    priorities = dict.fromkeys(
        [
            *graph["tour"],
            *(node["path"] for node in graph["nodes"] if node["id"] in graph["hotspots"]),
        ]
    )
    rank = {path: index for index, path in enumerate(priorities)}
    candidates = sorted(
        graph["nodes"],
        key=lambda node: (
            -sum(term in (node["name"] + " " + node["path"]).lower() for term in terms),
            rank.get(node["path"], len(rank)),
            node["kind"] != "file",
        ),
    )
    selected = []
    counts: Counter[str] = Counter()
    for node in candidates:
        if counts[node["path"]] >= MAX_EVIDENCE_NODES_PER_FILE:
            continue
        selected.append(node)
        counts[node["path"]] += 1
        if len(selected) == MAX_EVIDENCE_NODES:
            break
    return selected


def impact(graph: dict[str, Any], query: str) -> str:
    """Trace incoming calls/imports, excluding containment from dependency claims."""
    matches = [
        node
        for node in graph["nodes"]
        if query.lower()
        in {
            node["id"].lower(),
            node["name"].lower(),
            node["path"].lower(),
            node["name"].split("(")[0].lower(),
        }
    ]
    if not matches:
        return "No exact symbol or path match. Use dashboard search to find a name or path."
    seen = {node["id"] for node in matches}
    frontier = set(seen)
    for _ in range(4):
        found = {
            edge["source"]
            for edge in graph["edges"]
            if edge["target"] in frontier and edge["type"] != "contains"
        } - seen
        seen.update(found)
        frontier = found
    nodes = [node for node in graph["nodes"] if node["id"] in seen]
    return "Potential impact (static dependencies, up to 4 hops):\n" + "\n".join(
        f"- {node['name']} — {node['path']}:{node['line']}" for node in nodes[:80]
    )


def dashboard(root: Path, graph: dict[str, Any], changes: list[str]) -> str:
    """Render an offline dashboard with escaped graph data and raw HTML disabled in guides."""
    from novacode_cli.artifacts.server import _render_markdown
    from novacode_cli.brand import wordmark

    template = (
        files("novacode_cli.prompts")
        .joinpath("onboard_dashboard.jinja")
        .read_text(encoding="utf-8")
    )
    with (storage(root) / "guide.md").open(encoding="utf-8") as stream:
        guide_text = stream.read(100_000)
    payload = {
        **graph,
        "changes": changes,
        "guide": guide_text,
        "wordmark": wordmark(),
    }
    encoded = (
        json.dumps(payload, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    rendered = template.replace("__NOVA_ONBOARD_DATA__", encoded, 1)
    # Replace the earlier guide slot once, leaving template markers in user data literal.
    return rendered.replace("__NOVA_ONBOARD_GUIDE__", _render_markdown(guide_text), 1)
