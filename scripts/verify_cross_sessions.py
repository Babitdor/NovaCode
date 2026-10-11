"""Reproduce cross-session regressions, coverage, baseline probes and mutants.

Run with a supported Nova development environment; no model credentials or
network calls are used. Mutation mode restores each source file in finally.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".tmp" / "cross-session-verification"
TESTS = [
    "tests/test_session_messaging.py",
    "tests/test_session_gateway.py",
    "tests/test_session_rpc.py",
    "tests/test_cross_session_tui.py",
    "tests/test_cross_session_lifecycle.py",
    "tests/test_session_edges.py",
    "tests/test_session_transport.py",
    "tests/test_session_agent_tools.py",
    "tests/test_session_reply_panels.py",
    "tests/test_tab_resume.py",
    "tests/test_session_model_restore.py",
    "tests/test_session_provider_restore.py",
    "tests/test_session_worker.py",
    "tests/test_session_supervisor.py",
    "tests/test_tui_sessions.py",
    "tests/test_remote_sessions.py",
]
MODULES = [
    "novacode_cli.sessions.messaging",
    "novacode_cli.sessions.gateway",
    "novacode_cli.sessions.tui_messaging",
    "novacode_cli.sessions.resume",
]


def run(args, name, *, cwd=ROOT, timeout=180, extra_env=None):
    env = {**os.environ, **(extra_env or {})}
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        record = {"command": args, "returncode": result.returncode, "timed_out": False}
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired as exc:
        record = {"command": args, "returncode": None, "timed_out": True}
        output = (exc.stdout or b"").decode("utf-8", "replace") + (exc.stderr or b"").decode(
            "utf-8", "replace"
        )
    (OUTPUT / f"{name}.log").write_text(output, encoding="utf-8")
    print(f"{name}: {record['returncode']} (log: {OUTPUT / (name + '.log')})", flush=True)
    return record, output


def mutations():
    cases = [
        (
            "foreign-reply",
            "messaging.py",
            "if message.recipient != sender:",
            "if False and message.recipient != sender:",
            "tests/test_session_messaging.py::test_requests_and_replies_are_bound_to_session_and_request_id",
        ),
        (
            "queue-overflow",
            "messaging.py",
            ">= self.max_pending",
            "> self.max_pending",
            "tests/test_session_messaging.py::test_limits_fail_without_losing_pending_work_and_evict_completed_only",
        ),
        (
            "ambiguous-address",
            "messaging.py",
            "if len(matches) > 1:",
            "if len(matches) > 2:",
            "tests/test_session_messaging.py::test_duplicate_titles_require_stable_id_and_do_not_silently_pick_one",
        ),
        (
            "wrong-prompt",
            "tui_messaging.py",
            "entry[1] == pid and not entry[2].done()",
            "entry[1] != pid and not entry[2].done()",
            "tests/test_cross_session_lifecycle.py::test_fast_child_reply_during_send_and_fifo",
        ),
        (
            "dropped-endpoint",
            "gateway.py",
            "gateway = endpoint.get()",
            "gateway = None",
            "tests/test_session_gateway.py::test_all_tools_forward_only_schema_arguments",
        ),
    ]
    results = []
    for name, filename, old, new, test in cases:
        source = ROOT / "novacode_cli" / "sessions" / filename
        original, metadata = source.read_bytes(), source.stat()
        text = original.decode("utf-8")
        if text.count(old) != 1:
            raise RuntimeError(f"Mutation anchor changed: {name}")
        try:
            source.write_bytes(text.replace(old, new).encode("utf-8"))
            os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 120_000_000_000))
            record, output = run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    test,
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--tb=short",
                ],
                name,
                timeout=45,
                extra_env={"PYTHONDONTWRITEBYTECODE": "1"},
            )
            killed = (
                record["returncode"] == 1
                and "FAILED " in output
                and "ERROR collecting" not in output
            )
            results.append({"name": name, "killed": killed, **record})
        finally:
            source.write_bytes(original)
            os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    return results


def baseline():
    archive = subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            "HEAD",
            "novacode_cli",
            "tests",
            "pyproject.toml",
            "README.md",
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    destination = OUTPUT / "committed-baseline"
    destination.mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for item in bundle:
            target = (destination / item.name).resolve()
            if not target.is_relative_to(destination.resolve()) or item.issym() or item.islnk():
                raise RuntimeError("Unsafe archive member")
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(item) as source:
                    target.write_bytes(source.read())
    results = []
    probes = [
        "tests/test_entrance_animation.py::test_the_fade_paints_the_buttons_own_fill_on_every_frame",
        "tests/test_tui_app.py::test_tui_bg_agent_card_sizes_to_content",
    ]
    for index, test in enumerate(probes):
        for label, cwd in (("baseline", destination), ("candidate", ROOT)):
            record, _ = run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    test,
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--tb=short",
                ],
                f"{label}-probe-{index}",
                cwd=cwd,
                timeout=60,
                extra_env={"PYTHONPATH": str(cwd)},
            )
            results.append({"label": label, "test": test, **record})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutations", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    options = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    evidence = {"python": sys.version, "tests": TESTS}
    if options.baseline:
        evidence["baseline_probes"] = baseline()
        success = True  # diagnostic probes deliberately preserve failures
    elif options.mutations:
        evidence["mutations"] = mutations()
        success = all(case["killed"] for case in evidence["mutations"])
    else:
        data = OUTPUT / "coverage.data"
        command = [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            f"--data-file={data}",
            "--source=" + ",".join(MODULES),
            "-m",
            "pytest",
            *TESTS,
            "-q",
            "-p",
            "no:cacheprovider",
            "--tb=short",
            f"--junitxml={OUTPUT / 'tests.xml'}",
        ]
        evidence["regressions"], _ = run(command, "regressions")
        evidence["coverage"], _ = run(
            [
                sys.executable,
                "-m",
                "coverage",
                "report",
                f"--data-file={data}",
                "--fail-under=90",
                "-m",
            ],
            "coverage",
        )
        run(
            [
                sys.executable,
                "-m",
                "coverage",
                "json",
                f"--data-file={data}",
                "-o",
                str(OUTPUT / "coverage.json"),
            ],
            "coverage-json",
        )
        success = (
            evidence["regressions"]["returncode"] == 0 and evidence["coverage"]["returncode"] == 0
        )
    suffix = "baseline" if options.baseline else "mutations" if options.mutations else "regressions"
    (OUTPUT / f"{suffix}.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
