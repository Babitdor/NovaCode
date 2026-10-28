"""Run plugin compatibility checks and reversible negative controls locally.

Usage: uv run python scripts/verify_plugin_compatibility.py
Uses installed development tools; never downloads plugins or calls a model.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "test-results" / "plugins"
MODULES = [
    "novacode_cli/plugins/claude_plugins.py",
    "novacode_cli/plugins/components.py",
    "novacode_cli/plugins/hooks.py",
    "novacode_cli/plugins/storage.py",
    "novacode_cli/plugins/cli.py",
    "novacode_cli/plugins/marketplaces.py",
]
TESTS = [
    "tests/test_plugin_compatibility.py",
    "tests/test_plugins.py",
    "tests/test_hooks.py",
    "tests/test_skills_runtime.py",
    "tests/test_subagent_resilience.py",
    "tests/test_deepagents_compat.py",
]


def run(arguments: list[str]) -> None:
    """Fail the gate on a failed command; inherited output shows actual results."""
    subprocess.run(arguments, check=True, cwd=ROOT)  # noqa: S603 -- fixed development commands.


def mutate(name: str, file: str, transform: Callable[[str], str], test: str) -> dict:
    """Require an observed test failure, then restore exactly the original bytes."""
    path = ROOT / file
    original = path.read_bytes()
    changed = transform(original.decode("utf-8")).encode("utf-8")
    if changed == original:
        msg = f"Negative control was not applied: {name}"
        raise RuntimeError(msg)
    ast.parse(changed)
    path.write_bytes(changed)
    try:
        completed = subprocess.run(  # noqa: S603 -- selected local pytest assertions.
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--tb=short",
                "tests/test_plugin_compatibility.py::" + test,
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        (OUTPUT / (name + ".log")).write_text(completed.stdout + completed.stderr, encoding="utf-8")
        if completed.returncode != 1 or "FAILED" not in completed.stdout:
            msg = f"Negative control survived or did not reach assertions: {name}"
            raise RuntimeError(msg)
        return {"name": name, "test": test, "exit_code": completed.returncode, "killed": True}
    finally:
        if path.read_bytes() != changed:
            msg = f"Concurrent source edit; cannot safely restore {file}"
            raise RuntimeError(msg)
        path.write_bytes(original)


def remove_containment(text: str) -> str:
    """Recreate the unsafe path resolver as a concrete attack negative control."""
    lines = text.splitlines(keepends=True)
    function = next(
        n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == "contained"
    )
    lines[function.lineno - 1 : function.end_lineno] = [
        "def contained(root: Path, value: str, *, exists: bool = True) -> Path:\n"
        "    return root / value\n",
    ]
    return "".join(lines)


def remove_tool_denial(text: str) -> str:
    """Break only the synchronous pre-execution denial gate."""
    start = text.index("    def wrap_tool_call(")
    return text[:start] + text[start:].replace(
        "if before.denial:", "if False and before.denial:", 1
    )


def main() -> None:
    """Verify behavior, coverage, types and test sensitivity in one entry point."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    run([sys.executable, "-m", "coverage", "--version"])
    run(
        [
            "uv",
            "run",
            "ruff",
            "check",
            *MODULES,
            "novacode_cli/commands/plugin_install_handler.py",
            "tests/test_plugin_compatibility.py",
        ]
    )
    run(
        [
            "uv",
            "run",
            "ruff",
            "format",
            "--check",
            *MODULES,
            "novacode_cli/commands/plugin_install_handler.py",
            "tests/test_plugin_compatibility.py",
        ]
    )
    run(["uv", "run", "mypy", *MODULES, "--follow-imports", "silent"])
    controls = [
        mutate(
            "path-containment",
            MODULES[1],
            remove_containment,
            "test_existing_external_component_is_never_read",
        ),
        mutate(
            "activation",
            MODULES[0],
            lambda text: text.replace("values[identity] = enabled", "values[identity] = True", 1),
            "test_namespaces_and_disable_remove_every_component",
        ),
        mutate(
            "hook-denial",
            MODULES[2],
            remove_tool_denial,
            "test_hook_denial_prevents_sync_and_async_tool_execution",
        ),
        mutate(
            "rollback",
            MODULES[0],
            lambda text: text.replace("write_json(settings_path, previous_settings)", "pass", 1),
            "test_failed_registry_publish_restores_activation_settings",
        ),
    ]
    data = str(OUTPUT / ".coverage")
    run(
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "--source=novacode_cli.plugins",
            "--data-file=" + data,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            *TESTS,
        ]
    )
    run([sys.executable, "-m", "coverage", "report", "--data-file=" + data, "--fail-under=80"])
    run(
        [
            sys.executable,
            "-m",
            "coverage",
            "json",
            "--data-file=" + data,
            "-o",
            str(OUTPUT / "coverage.json"),
        ]
    )
    run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "--random-order",
            "--random-order-seed=428",
            *TESTS[:2],
        ]
    )
    run([sys.executable, "-m", "novacode_cli", "plugin", "--help"])
    with tempfile.TemporaryDirectory(prefix="nova-plugin-smoke-") as temporary:
        root = Path(temporary)
        (root / ".claude-plugin").mkdir()
        (root / ".claude-plugin/plugin.json").write_text(
            '{"name":"verification","version":"1.0","description":"Local smoke fixture"}',
            encoding="utf-8",
        )
        run([sys.executable, "-m", "novacode_cli", "plugin", "validate", str(root)])
    (OUTPUT / "negative-controls.json").write_text(json.dumps(controls, indent=2), encoding="utf-8")
    sources = [
        *MODULES,
        *TESTS,
        "novacode_cli/tui/app.py",
        "novacode_cli/tui/screens.py",
        "novacode_cli/skills/refreshing_middleware.py",
        "novacode_cli/skills/runtime.py",
        "novacode_cli/mcp/config.py",
        "novacode_cli/agents/core_agent.py",
    ]
    fingerprints = {
        file: hashlib.sha256((ROOT / file).read_bytes()).hexdigest() for file in sources
    }
    (OUTPUT / "source-state.json").write_text(json.dumps(fingerprints, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
