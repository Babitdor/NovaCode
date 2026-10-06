"""Run UI/recovery checks without network checks or native voice-model warmup.

Run `python scripts/verify_ui_recovery.py`; add --mutations for three deliberate
snapshot regressions. No dependencies are installed by this script.
"""

from __future__ import annotations

# Standalone verification entry point, not an importable package.
# ruff: noqa: INP001
import argparse
import ast
import importlib
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TESTS = [
    "tests/test_ui_harness.py",
    "tests/test_tui_crash_autosave.py",
    "tests/test_session_crash_recovery.py",
    "tests/test_session_clear.py",
    "tests/test_session_model_restore.py",
    "tests/test_session_provider_restore.py",
    "tests/test_session_retention.py",
    "tests/test_subagent_tasks.py",
    "tests/test_tui_subagent_panel.py",
    "tests/test_tui_sessions.py",
    "tests/test_session_import.py",
]


class Isolation:
    """Isolate native audio, update services and saved layouts at the boundary."""

    @pytest.fixture(autouse=True)
    def isolated_ui(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Keep native audio warmup and saved user profiles out of UI tests."""
        from langgraph.store.memory import InMemoryStore

        from novacode_cli.tui.app import NovaApp

        async def no_warmup(_self: object) -> None:
            pass

        monkeypatch.setattr(NovaApp, "_eager_voice_warmup", no_warmup)
        # Mock streams still exercise lease acquisition/renewal/release, but
        # must not contend with live Nova sessions in the user's SQLite store.
        store = InMemoryStore()
        monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
        monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(
            "novacode_cli.tui.harness.profile_path", lambda _workspace: tmp_path / "layout.json"
        )


def mutations() -> int:
    """Each mutant must run and cause its targeted behavioral test to fail."""
    path = ROOT / "novacode_cli/session/session_persistence.py"
    original = path.read_text(encoding="utf-8")
    cases = [
        (
            "non-atomic write",
            "temporary.replace(path)",
            "path.write_text(content, encoding='utf-8')",
            "test_snapshot_survives_failed_replace",
        ),
        (
            "ignore snapshot",
            'if snapshot.get("version") == 1:',
            "if False:",
            "test_snapshot_ignores_torn_compatibility_files",
        ),
        (
            "empty crash overwrite",
            'if not messages and task_status == "crashed":',
            "if False:",
            "test_empty_crash_save_keeps_last_conversation",
        ),
    ]
    for name, before, after, test in cases:
        if original.count(before) != 1:
            message = f"Mutation anchor is missing or ambiguous: {name}"
            raise RuntimeError(message)
        try:
            path.write_text(original.replace(before, after), encoding="utf-8")
            with tempfile.TemporaryDirectory() as directory:
                report = Path(directory) / "result.xml"
                result = subprocess.run(  # noqa: S603 — fixed test program and selectors
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--test",
                        f"tests/test_session_crash_recovery.py::{test}",
                        "--junitxml",
                        str(report),
                    ],
                    cwd=ROOT,
                    check=False,
                )
                # This report is produced by our fixed pytest child, not imported input.
                cases_run = list(ET.parse(report).iter("testcase")) if report.exists() else []  # noqa: S314
                valid = len(cases_run) == 1 and cases_run[0].find("error") is None
                failure = cases_run[0].find("failure") if valid else None
                details = "" if failure is None else (failure.text or "")
            if (
                result.returncode != 1
                or not any(
                    marker in details
                    for marker in ("DID NOT RAISE", "AssertionError", "IndexError")
                )
                or "Timeout" in details
            ):
                message = f"Mutant was not killed by an assertion: {name} ({result.returncode})"
                raise RuntimeError(message)
        finally:
            path.write_text(original, encoding="utf-8")
        print(f"Killed: {name}")  # noqa: T201 — verification CLI output
    return 0


def main() -> int:
    """Run the regression suite or its deliberate mutation controls."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutations", action="store_true")
    parser.add_argument("--coverage-gate", type=Path)
    parser.add_argument("--gate-negative-control", action="store_true")
    parser.add_argument("--test", action="append")
    parser.add_argument("--junitxml", type=Path)
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.coverage_gate:
        return coverage_gate(args.coverage_gate, negative=args.gate_negative_control)
    if args.mutations:
        return mutations()
    # Normal Nova startup builds its tools before handing off to Textual. Do
    # the same here so cold provider SDK imports don't consume turn deadlines.
    importlib.import_module("novacode_cli.tools.plan_mode_tools")
    importlib.import_module("novacode_cli.agents.core_agent")
    return int(
        pytest.main(
            [
                *(args.test or TESTS),
                *([f"--junitxml={args.junitxml}"] if args.junitxml else []),
                "-q",
                "--tb=short",
                "--timeout=60",
                "--show-capture=no",
                "-p",
                "no:cacheprovider",
            ],
            plugins=[Isolation()],
        )
    )


def coverage_gate(data_file: Path, *, negative: bool = False) -> int:
    """Require every statement in the atomic snapshot core to have executed."""
    import coverage

    path = ROOT / "novacode_cli/session/session_persistence.py"
    names = {
        "atomic_write",
        "serialized_save",
        "_save_snapshot",
        "_load_snapshot_data",
        "_read_snapshot",
    }
    nodes = [
        node
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if {node.name for node in nodes} != names:
        message = "Coverage gate could not identify all snapshot functions"
        raise RuntimeError(message)
    measured = coverage.Coverage(data_file=str(data_file))
    measured.load()
    _, statements, _, missing, _ = measured.analysis2(str(path))
    critical = {
        line for node in nodes for line in statements if node.lineno <= line <= node.end_lineno
    }
    if not critical:
        message = "Coverage gate found no executable statements"
        raise RuntimeError(message)
    uncovered = critical & set(missing)
    if negative:
        uncovered.add(min(critical))
    if uncovered:
        print(f"Snapshot coverage gate failed: {sorted(uncovered)}")  # noqa: T201 — verification CLI
        return 1
    print(f"Snapshot coverage gate passed: {len(critical)}/{len(critical)} statements")  # noqa: T201 — verification CLI
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
