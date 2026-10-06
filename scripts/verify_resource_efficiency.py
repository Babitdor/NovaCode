"""Verify resource contracts with isolated user configuration and UI state.

Run in the project's locked environment:
`uv run --frozen python scripts/verify_resource_efficiency.py`.
Add --full for the entire test suite or --test to select individual tests.
"""

# Standalone command rather than an importable package.
# ruff: noqa: INP001
from __future__ import annotations

import argparse
import importlib
import os
import runpy
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_BUFFER_COVERAGE = 100
sys.path.insert(0, str(ROOT))
TESTS = [
    "tests/test_resource_efficiency.py",
    "tests/test_audio",
    "tests/test_tui_app.py",
    "tests/test_tui_no_layout_storm.py",
    "tests/test_tui_no_blocking_io.py",
    "tests/test_async_offload_fixes.py",
    "tests/test_mcp_sessions.py",
    "tests/test_tui_crash_autosave.py",
    "tests/test_session_crash_recovery.py",
    "tests/test_session_clear.py",
    "tests/test_tui_subagent_panel.py",
]


def main() -> int:
    """Preload slow SDK imports, then run hermetic regression checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="run the entire tests directory")
    parser.add_argument("--test", action="append", help="override test selectors")
    parser.add_argument(
        "--coverage", action="store_true", help="require full output-buffer coverage"
    )
    args, extra = parser.parse_known_args()
    with tempfile.TemporaryDirectory(prefix="nova-resource-") as directory:
        home = Path(directory)
        # Each validation process has its own home; no live Nova process can
        # change the configuration being read or guarded by this test run.
        with (
            patch("pathlib.Path.home", return_value=home),
            patch.dict(os.environ, {"USERPROFILE": str(home)}),
        ):
            try:
                return run_tests(args, extra)
            finally:
                close_test_store(home)


def close_test_store(home: Path) -> None:
    """Close this run's SQLite singleton before deleting its temporary home."""
    module = sys.modules.get("novacode_cli.memory.store")
    store = getattr(module, "_store", None)
    path = getattr(module, "_STORE_DB_PATH", None)
    if store is None or path is None or not Path(path).is_relative_to(home):
        return
    underlying = getattr(store, "_store", None)
    connection = getattr(underlying, "conn", getattr(underlying, "_conn", None))
    if connection is not None:
        with store._lock:
            connection.close()


def run_tests(args: argparse.Namespace, extra: list[str]) -> int:
    """Run SDK imports and tests while the isolated home remains in effect."""
    isolation = runpy.run_path(str(ROOT / "scripts/verify_ui_recovery.py"))["Isolation"]
    # Cold provider imports must not consume a per-test behavioral timeout.
    importlib.import_module("novacode_cli.tools.plan_mode_tools")
    importlib.import_module("novacode_cli.agents.core_agent")
    import pytest

    cov = None
    if args.coverage:
        import coverage

        buffer = importlib.import_module("novacode_cli.tui.output_buffer")
        cov = coverage.Coverage(
            branch=True,
            source=["novacode_cli.tui.output_buffer"],
            data_file=str(ROOT / ".tmp-resource.coverage"),
        )
        cov.erase()
        cov.start()
        importlib.reload(buffer)

    selectors = args.test or (["tests"] if args.full else TESTS)
    result = pytest.main(
        [
            *selectors,
            "-q",
            "--tb=short",
            "--show-capture=no",
            "--timeout=60",
            "-p",
            "no:cacheprovider",
            *extra,
        ],
        plugins=[isolation()],
    )
    if cov is not None:
        cov.stop()
        cov.save()
        measured = cov.report(show_missing=True)
        if measured < REQUIRED_BUFFER_COVERAGE:
            return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
