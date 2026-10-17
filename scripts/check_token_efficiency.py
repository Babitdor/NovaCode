"""Run pytest offline with an isolated user profile and persist its actual result."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

NETWORK_GUARD = """import socket
original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex
def allowed(address):
    return not isinstance(address, tuple) or address[0] in ("localhost", "127.0.0.1", "::1")
def connect(self, address):
    if not allowed(address):
        raise RuntimeError("External network disabled by offline verification")
    return original_connect(self, address)
def connect_ex(self, address):
    if not allowed(address):
        raise RuntimeError("External network disabled by offline verification")
    return original_connect_ex(self, address)
socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
"""

PROGRESS_PLUGIN = """import json
import os
from pathlib import Path
def emit(value):
    with Path(os.environ["NOVA_TEST_PROGRESS"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\\n")
def pytest_runtest_logstart(nodeid, location):
    emit({"event": "start", "nodeid": nodeid})
def pytest_runtest_logreport(report):
    if report.failed or report.when == "call":
        emit({"event": "report", "nodeid": report.nodeid, "phase": report.when,
              "outcome": report.outcome, "duration": report.duration,
              "error": str(report.longrepr) if report.failed else None})
def pytest_collection_modifyitems(config, items):
    after = os.environ.get("NOVA_TEST_START_AFTER_NODE")
    if after:
        index = [item.nodeid for item in items].index(after) + 1
        config.hook.pytest_deselected(items=items[:index])
        items[:] = items[index:]
"""


def main() -> int:
    """Run a scoped or full suite; failures remain failures in both report and exit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--label", default="full-final")
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--start-after")
    parser.add_argument("targets", nargs="*", default=["tests/"])
    args = parser.parse_args()
    report = Path("reports/token-efficiency").resolve()
    report.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        *args.targets,
        "-q",
        "--timeout=300",
        "-p",
        "no:cacheprovider",
        "--tb=short",
        "-p",
        "offline_progress",
    ]
    if args.collect_only:
        command.append("--collect-only")
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="nova-offline-") as directory:
        profile = Path(directory)
        (profile / "sitecustomize.py").write_text(NETWORK_GUARD, encoding="utf-8")
        (profile / "offline_progress.py").write_text(PROGRESS_PLUGIN, encoding="utf-8")
        env = dict(os.environ)
        for key in list(env):
            if key.endswith(("_API_KEY", "_TOKEN")) or key in {
                "OPENAI_BASE_URL",
                "LANGSMITH_ENDPOINT",
            }:
                env.pop(key)
        env.update(
            {
                "USERPROFILE": directory,
                "HOME": directory,
                "NOVA_LOCAL_METRICS": "0",
                "PYTHONPATH": os.pathsep.join([directory, str(Path(args.root).resolve())]),
                "LANGSMITH_TRACING": "false",
                "PYTHONIOENCODING": "utf-8",
                "NOVA_TEST_PROGRESS": str(report / f"{args.label}-progress.jsonl"),
            }
        )
        if args.start_after:
            env["NOVA_TEST_START_AFTER_NODE"] = args.start_after
        with (report / f"{args.label}.txt").open("w", encoding="utf-8") as output:
            process = subprocess.run(  # noqa: S603 - local pytest targets, no shell
                command,
                cwd=args.root,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
    result = {
        "command": command,
        "root": str(Path(args.root).resolve()),
        "exit_code": process.returncode,
        "duration_seconds": time.perf_counter() - started,
        "external_network": "blocked",
        "profile": "temporary",
    }
    (report / f"{args.label}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
