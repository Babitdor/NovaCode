"""Reproduce focused background-command/release checks, optionally with mutations.

Run: python scripts/verify_background_updates.py [--coverage] [--mutations]
Mutations temporarily modify source files; do not run alongside other edits/tests.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = [
    "tests/test_ctrl_b_background.py",
    "tests/test_tui_update_release.py",
    "tests/test_update_release.py",
    "tests/test_shell_output_responsiveness.py",
    "tests/test_bash_in_chat.py",
    "tests/test_updates.py",
    "tests/test_background_monitor.py",
    "tests/test_model_retry_progress.py",
    "tests/test_provider_errors.py",
    "tests/test_async_general_purpose.py",
    "tests/test_core_prompt_subagents.py",
]


def run(arguments: list[str], *, env: dict[str, str] | None = None) -> int:
    return subprocess.run(  # noqa: S603 — fixed Python executable and local checks
        [sys.executable, *arguments], cwd=ROOT, env=env, check=False, timeout=180
    ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--mutations", action="store_true")
    args = parser.parse_args()
    pytest_args = [
        *TESTS, "-q", "-p", "no:cacheprovider", "--tb=short", "--timeout=60",
        "--basetemp=.tmp/background-update-verify",
    ]
    env = {**os.environ, "COVERAGE_FILE": str(ROOT / ".tmp/background-update.coverage")}
    command = (
        ["-m", "coverage", "run", "--branch", "--source=novacode_cli", "-m", "pytest"]
        if args.coverage else ["-m", "pytest"]
    )
    if run([*command, *pytest_args], env=env):
        return 1
    if args.coverage and run([
        "-m", "coverage", "report",
        "--include=*/updates.py,*/update_notice.py,*/model_retry.py", "-m",
    ], env=env):
        return 1
    if not args.mutations:
        return 0
    mutations = [
        (
            "novacode_cli/tui/app.py",
            'Binding("ctrl+b", "run_background", "Background", priority=True)',
            'Binding("ctrl+b", "run_background", "Background", priority=False)',
            "tests/test_ctrl_b_background.py::test_focused_prompt_ctrl_b_detaches_shell_and_preserves_draft",
        ),
        (
            "novacode_cli/tui/app.py",
            "control = shell_jobs.set_current(cmd) if foreground else None",
            "control = None",
            "tests/test_ctrl_b_background.py::test_bang_command_background_handoff_keeps_process_and_finishes",
        ),
        (
            "novacode_cli/updates.py",
            "filename = max(names, key=lambda name: Version(name[11:-3]))",
            "filename = min(names, key=lambda name: Version(name[11:-3]))",
            "tests/test_update_release.py::test_git_update_gets_version_and_title_from_changelog",
        ),
        (
            "novacode_cli/tui/app.py",
            'if not force and getattr(self, "_notified_nova_update", None) == status.latest:',
            "if False:",
            "tests/test_tui_update_release.py::test_available_release_notice_is_deduplicated_and_view_more_opens_changelog",
        ),
    ]
    for index, (filename, before, after, test) in enumerate(mutations, start=1):
        path = ROOT / filename
        original = path.read_bytes()
        source = original.decode("utf-8")
        if source.count(before) != 1:
            raise ValueError(f"Mutation {index} requires exactly one matching source line")
        try:
            path.write_bytes(source.replace(before, after).encode("utf-8"))
            code = run([
                "-m", "pytest", test, "-q", "-p", "no:cacheprovider", "--tb=short", "--timeout=60",
                f"--basetemp=.tmp/background-update-mutant-{index}",
            ])
        finally:
            path.write_bytes(original)
        if code != 1:
            raise RuntimeError(f"Mutation {index}: expected test failure (exit 1), got {code}")
        print(f"Mutation {index} caught; source restored.")  # noqa: T201
    return run(["-m", "pytest", *pytest_args])


if __name__ == "__main__":
    raise SystemExit(main())
