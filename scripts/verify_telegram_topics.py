"""Reproducible Telegram session checks and isolated, in-memory mutations."""

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutations", action="store_true")
    args = parser.parse_args()
    tests = [
        "tests/test_telegram_hub.py",
        "tests/test_telegram_session_topics.py",
        "tests/test_tui_remote_subcommands.py",
        "tests/test_remote_sessions.py",
        "tests/test_remote_streaming.py",
        "tests/test_remote_status.py",
        "tests/test_remote_question.py",
        "tests/test_remote_tool_digest.py",
        "tests/test_remote_voice_notes.py",
        "tests/test_telegram_voice.py",
        "tests/test_telegram_format.py",
    ]
    command = [
        sys.executable,
        "-m",
        "pytest",
        *tests,
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp=.tmp/telegram-verify",
        "--timeout=60",
        "--tb=short",
    ]
    subprocess.run(command, check=True)
    subprocess.run([sys.executable, "scripts/verify_telegram_processes.py"], check=True)
    if not args.mutations:
        return
    mutants = [
        (
            "novacode_cli.remote.telegram_hub",
            "if not hmac.compare_digest(",
            "if False and hmac.compare_digest(",
            "tests/test_telegram_hub.py::test_hub_authentication_rejects_other_clients",
        ),
        (
            "novacode_cli.remote.telegram_hub",
            "client = self.topics.get(key)",
            "client = next(iter(self.clients), None)",
            "tests/test_telegram_hub.py::test_shared_polling_routes_two_sessions_and_hands_off",
        ),
        (
            "novacode_cli.remote.telegram_bridge",
            'tid = saved.value.get("topic") if saved else None',
            "tid = None",
            "tests/test_telegram_session_topics.py::test_topic_creation_is_idempotent_concurrent_and_durable",
        ),
        (
            "novacode_cli.remote.telegram_hub",
            "if any(key in self.sessions",
            "if False and any(key in self.sessions",
            "tests/test_telegram_hub.py::test_same_session_cannot_be_claimed_by_two_windows",
        ),
    ]
    for index, (name, old, new, test) in enumerate(mutants):
        spec = importlib.util.find_spec(name)
        assert spec and spec.origin
        source = Path(spec.origin).read_text(encoding="utf-8")
        assert source.count(old) == 1, (name, old)
        mutated = source.replace(old, new)
        code = f"import importlib,pytest; m=importlib.import_module({name!r}); exec(compile({mutated!r},m.__file__,'exec'),m.__dict__); raise SystemExit(pytest.main([{test!r},'-q','-p','no:cacheprovider','--timeout=12','--tb=short','--basetemp=.tmp/telegram-mutant-{index}']))"
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        if result.returncode != 1 or "FAILED" not in result.stdout:
            raise RuntimeError(
                f"Mutant {index + 1} was not killed by an assertion: {result.stdout}\n{result.stderr}"
            )
        print(f"PASS: mutant {index + 1} killed")


if __name__ == "__main__":
    main()
