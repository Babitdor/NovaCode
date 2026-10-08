"""Reproduce OpenCode acceptance tests, helper coverage and in-memory mutations.

Usage: python scripts/verify_opencode_zen.py [--mutations]
Existing project/test dependencies only. Source files are never mutated.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUTANTS = {
    "missing_session": ('"x-opencode-session": session_id', '"wrong-session": session_id'),
    "wrong_protocol": ('return "responses"', 'return "chat"'),
    "decision_as_chat": ('return "unsupported"', 'return "chat"'),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutations", action="store_true")
    parser.add_argument("--mutant", choices=MUTANTS)
    args = parser.parse_args()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    os.environ["NOVA_DISABLE_UPDATE_CHECK"] = "1"
    if args.mutations:
        for name in MUTANTS:
            run = subprocess.run([sys.executable, __file__, "--mutant", name], check=False)
            if run.returncode != 1:
                raise RuntimeError(f"Mutation {name} did not fail assertions: {run.returncode}")
            print(f"Killed {name}", flush=True)
        return 0
    if args.mutant:
        module = importlib.import_module("novacode_cli.config.opencode_gateway")
        source = Path(module.__file__).read_text(encoding="utf-8")
        old, new = MUTANTS[args.mutant]
        if source.count(old) != 1:
            raise RuntimeError("Mutation target must match exactly once")
        source = source.replace(old, new, 1)
        filename = f"<zen-mutant-{args.mutant}>"
        exec(compile(source, filename, "exec"), module.__dict__)
        if module.protocol.__code__.co_filename != filename:
            raise RuntimeError("Mutation did not execute")
        print(f"Executed {args.mutant}: {hashlib.sha256(source.encode()).hexdigest()}", flush=True)

    import coverage
    import pytest

    collector = coverage.Coverage(data_file=None, source=["novacode_cli.config.opencode_gateway"])
    collector.start()
    result = int(
        pytest.main(
            [
                "tests/test_opencode_zen.py",
                *(
                    []
                    if args.mutant
                    else ["tests/test_opencode_jev.py", "tests/test_exit_summary.py"]
                ),
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp=.tmp/zen-verifier",
                "--timeout=60",
                "--tb=short",
            ]
        )
    )
    collector.stop()
    if not result and not args.mutant:
        # This is a gate: missing coverage fails, rather than merely printing a percentage.
        if collector.report() < 100:
            raise RuntimeError("OpenCode protocol helper coverage is below 100%")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
