"""Run Jev regression checks and optional in-memory manual mutations.

Usage: python scripts/verify_jev.py [--mutations]
No source files are modified. Each mutant runs in a fresh interpreter, using
compile/exec directly so stale bytecode cannot substitute another mutation.
"""

from __future__ import annotations

# Standalone verification script; its results intentionally go to stdout.
# ruff: noqa: INP001, T201
import argparse
import ast
import hashlib
import importlib
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coverage import Coverage

ROOT = Path(__file__).resolve().parents[1]
TESTS = [
    "tests/test_jev_auth.py",
    "tests/test_auth_screens.py",
    "tests/test_provider_auth.py",
    "tests/test_credentials.py",
    "tests/test_tool_verdicts.py",
    "tests/test_verdict_tool_uses_edit.py",
    "tests/test_decision_settings.py",
    "tests/test_model_selector.py",
    "tests/test_model_screen_custom.py",
    "tests/test_model_screen_roles.py",
]
MUTATIONS = {
    "wrong_host": ("if endpoint == NovaConfig.TOOL_VERDICT_JEV_ENDPOINT:", "if True:"),
    "missing_key": ("if not self.api_key:", "if False:"),
    "invalid_probability": (
        "if not math.isfinite(float(value)) or not 0 <= value <= 1:",
        "if False:",
    ),
    "shared_cache": (
        "identity = _dumps([config.get_tool_verdict_endpoint(), config.get_tool_verdict_model()])",
        'identity = "same-for-all-models"',
    ),
}


def run_mutations() -> int:
    """Require assertion failures for each executed mutant."""
    for name in MUTATIONS:
        # The executable, script and mutation names are all locally defined.
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(Path(__file__).resolve()), "--mutant", name], check=False
        )
        if result.returncode != 1:
            message = f"Mutation {name} was not killed by test assertions"
            raise RuntimeError(message)
        print(f"Killed: {name}", flush=True)
    return 0


def apply_mutation(name: str) -> None:
    """Compile one exact source mutation directly, avoiding cached bytecode."""
    module = importlib.import_module("novacode_cli.agents.tool_verdicts")
    source = Path(module.__file__).read_text(encoding="utf-8")
    old, new = MUTATIONS[name]
    if source.count(old) != 1:
        message = "Mutation target must match exactly once"
        raise RuntimeError(message)
    mutated = source.replace(old, new, 1)
    filename = f"<jev-mutant-{name}>"
    exec(compile(mutated, filename, "exec"), module.__dict__)  # noqa: S102 — local test mutation
    if module.parse_answers.__code__.co_filename != filename:
        message = "Mutated module was not executed"
        raise RuntimeError(message)
    print(f"Executed {name}: {hashlib.sha256(mutated.encode()).hexdigest()}", flush=True)


def check_helper_coverage(collector: Coverage) -> None:
    """Gate executable-line coverage for the three newly added helpers."""
    count = 0
    for relative, names in {
        "novacode_cli/agents/tool_verdicts.py": {"create_system_one_client", "verdict_cache_path"},
        "novacode_cli/config/nova_config.py": {"set_tool_verdict_settings"},
    }.items():
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"))
        selected = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in names:
                selected.update(range(node.lineno, node.end_lineno + 1))
        if not selected:
            message = "Coverage target not found"
            raise RuntimeError(message)
        _, statements, _, missing, _ = collector.analysis2(str(path))
        measured = selected.intersection(statements)
        uncovered = measured.intersection(missing)
        if not measured or uncovered:
            message = f"Uncovered new helper lines in {relative}: {sorted(uncovered)}"
            raise RuntimeError(message)
        count += len(measured)
    print(f"New helper coverage: {count}/{count} executable lines", flush=True)


def main() -> int:
    """Execute regression, mutation or helper coverage checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutations", action="store_true")
    parser.add_argument("--mutant", choices=MUTATIONS)
    parser.add_argument("--coverage", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    os.environ["NOVA_DISABLE_UPDATE_CHECK"] = "1"
    os.environ.pop("NO_COLOR", None)
    if args.mutations:
        return run_mutations()
    if args.mutant:
        apply_mutation(args.mutant)
    collector = None
    if args.coverage:
        import coverage

        collector = coverage.Coverage(branch=True, data_file=None, source=["novacode_cli"])
        collector.start()
    import pytest

    tests = TESTS
    if args.mutant:
        tests = TESTS[:1]
    elif args.coverage:
        # File-heavy legacy cache stress tests are unrelated to these helpers
        # and time out under Windows coverage tracing; run them in normal mode.
        tests = [TESTS[0], "tests/test_decision_settings.py"]
    result = int(
        pytest.main(
            [
                *tests,
                "-q",
                "--tb=short",
                "--timeout=60",
                "--show-capture=no",
                "-p",
                "no:cacheprovider",
            ]
        )
    )
    if collector is not None:
        collector.stop()
        check_helper_coverage(collector)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
