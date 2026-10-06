"""Check that deliberate resource regressions fail their behavioral tests.

Run in the locked environment: python scripts/mutate_resource_efficiency.py
Mutations exist only in child-process memory; workspace source is never rewritten.
"""

# Standalone command. Reports from our pytest children are trusted XML.
# ruff: noqa: INP001, S314
from __future__ import annotations

import argparse
import inspect
import re
import runpy
import subprocess
import sys
import tempfile
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MUTANTS = [
    (
        "buffer",
        "OutputTail",
        "append",
        "while self._size > MAX_PENDING_CHARS:",
        "while False:",
        "test_tool_backlog_is_bounded_before_flush",
    ),
    (
        "buffer",
        "OutputTail",
        "append",
        "text[-MAX_PENDING_CHARS:]",
        "text[:MAX_PENDING_CHARS]",
        "test_tool_backlog_bounds_call_count_and_keeps_newest_tail",
    ),
    (
        "app",
        "NovaApp",
        "_schedule_status_tick",
        "else 0.5",
        "else 0.05",
        "test_idle_session_has_no_continuous_banner_animation",
    ),
    (
        "pipeline",
        "VoicePipeline",
        "speak",
        "self._ensure_tts()",
        "self._ensure_components()",
        "test_speech_only_does_not_build_input_stack",
    ),
]


def child(index: int, report: str) -> int:
    """Replace one actual method with its modified source, then run its test."""
    from novacode_cli.audio import pipeline
    from novacode_cli.tui import app, output_buffer

    modules = {"app": app, "pipeline": pipeline, "buffer": output_buffer}
    module_name, class_name, method_name, before, after, test = MUTANTS[index]
    module = modules[module_name]
    target = getattr(module, class_name)
    source = textwrap.dedent(inspect.getsource(getattr(target, method_name)))
    if source.count(before) != 1:
        message = f"Missing/ambiguous mutation anchor: {before}"
        raise RuntimeError(message)
    namespace: dict = {}
    # Execute this repository method with one deliberate bug, using its real
    # module globals. This is verification code, never model-generated input.
    exec(  # noqa: S102 — deliberate test-only mutation of repository code
        compile(source.replace(before, after), inspect.getfile(target), "exec"),
        vars(module),
        namespace,
    )
    setattr(target, method_name, namespace[method_name])
    sys.argv = [
        "verify_resource_efficiency.py",
        "--test",
        f"tests/test_resource_efficiency.py::{test}",
        f"--junitxml={report}",
    ]
    verifier = runpy.run_path(str(ROOT / "scripts/verify_resource_efficiency.py"))
    return verifier["main"]()


def main() -> int:
    """Require every mutant to run and fail an assertion, not setup or timeout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", type=int, choices=range(len(MUTANTS)))
    parser.add_argument("--report")
    args = parser.parse_args()
    if args.child is not None:
        if not args.report:
            parser.error("--child requires --report")
        return child(args.child, args.report)
    with tempfile.TemporaryDirectory(prefix="nova-mutants-") as directory:
        for index, mutant in enumerate(MUTANTS):
            report = Path(directory) / f"{index}.xml"
            result = subprocess.run(  # noqa: S603 — fixed child program and selectors
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child",
                    str(index),
                    "--report",
                    str(report),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            cases = list(ET.parse(report).iter("testcase")) if report.exists() else []
            valid = len(cases) == 1 and cases[0].find("error") is None
            failure = cases[0].find("failure") if valid else None
            details = "" if failure is None else (failure.text or "")
            assertion_failed = "AssertionError" in details or re.search(r"\nE\s+assert\b", details)
            if result.returncode != 1 or not assertion_failed or "Timeout" in details:
                sys.stderr.write(result.stdout + result.stderr)
                message = f"Mutant {index} did not fail its behavioral assertion"
                raise RuntimeError(message)
            sys.stdout.write(f"Killed {index + 1}/{len(MUTANTS)}: {mutant[2]} — {mutant[5]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
