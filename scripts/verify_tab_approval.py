"""Reproduce approval regression tests, coverage gate, and three manual mutants.

Run with the project interpreter from the repository root. Mutants change only
the current Python process and never modify implementation files.
"""

import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CASES = "manual_tool_approval_fails_closed or child_tab_inherits_mode or queued_approval_mode or remote_photo_routes_to_child"
TESTS = ["tests/test_tui_sessions.py", "tests/test_session_worker.py"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutant", choices=["approve-dismissal", "force-auto-tab", "force-auto-turn"])
    parser.add_argument("--mutations", action="store_true")
    parser.add_argument("--coverage", action="store_true")
    args = parser.parse_args()
    if args.mutations:
        for name in ["approve-dismissal", "force-auto-tab", "force-auto-turn"]:
            result = subprocess.run([sys.executable, __file__, "--mutant", name], cwd=ROOT, capture_output=True, text=True)
            if result.returncode != 1 or "FAILED" not in result.stdout:
                print(result.stdout, result.stderr)
                raise SystemExit(f"Mutant {name} did not produce a behavioral test failure")
            print(f"Killed: {name}")
        print("3/3 manual mutants killed")
        return
    pytest_args = [*TESTS, "-k", CASES, "-q", "-p", "no:cacheprovider", "--basetemp", f".tmp/approval-verification-{args.mutant or 'baseline'}"]
    if args.coverage:
        data = ROOT / ".tmp/approval-coverage"
        commands = [
            [sys.executable, "-m", "coverage", "run", "--branch", f"--data-file={data}", "-m", "pytest", *pytest_args],
            [sys.executable, "-m", "coverage", "json", f"--data-file={data}", "-o", ".tmp/approval-coverage.json"],
        ]
        for command in commands:
            subprocess.run(command, cwd=ROOT, check=True)
        source = ROOT / "novacode_cli/tui/app.py"
        function = next(node for node in ast.walk(ast.parse(source.read_text("utf-8"))) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_child_prompt")
        report = json.loads((ROOT / ".tmp/approval-coverage.json").read_text("utf-8"))
        covered = next(value for name, value in report["files"].items() if Path(name).as_posix().endswith("novacode_cli/tui/app.py"))
        missing = [line for line in covered["missing_lines"] if function.lineno <= line <= function.end_lineno]
        branches = [branch for branch in covered.get("missing_branches", []) if function.lineno <= branch[0] <= function.end_lineno]
        if missing or branches:
            raise SystemExit(f"Approval transmission coverage gate failed: lines={missing}, branches={branches}")
        print("Approval transmission helper: 100% line and branch coverage (enforced)")
        return
    sys.path.insert(0, str(ROOT))
    if args.mutant:
        from novacode_cli.tui.app import NovaApp
        from novacode_cli.sessions.worker import SessionWorker
        if args.mutant == "approve-dismissal":
            original = NovaApp._handle_interrupt_inner
            async def handle(self, event):
                if event.kind == "tool":
                    event.future.set_result({"decisions": [{"type": "approve"}], "any_rejected": False})
                else:
                    await original(self, event)
            NovaApp._handle_interrupt_inner = handle
        elif args.mutant == "force-auto-tab":
            original = NovaApp.spawn_session
            async def spawn(self, *args, **kwargs):
                kwargs["auto_approve"] = True
                return await original(self, *args, **kwargs)
            NovaApp.spawn_session = spawn
        else:
            original = SessionWorker._run_turn
            async def turn(self, prompt_id, text, images=None, auto_approve=None):
                await original(self, prompt_id, text, images, True)
            SessionWorker._run_turn = turn
    import pytest
    raise SystemExit(pytest.main(pytest_args))


if __name__ == "__main__":
    main()
