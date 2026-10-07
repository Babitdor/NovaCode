"""Reproduce provider-auth tests, coverage, static checks and security mutations.

Run: python scripts/verify_provider_signin.py
Add --mutations to demonstrate that the three main security guards detect bugs.
Live account sign-in and the full application suite require their own environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTH = ROOT / "novacode_cli/config/google_oauth_auth.py"
TESTS = [
    "tests/test_google_oauth_auth.py",
    "tests/test_provider_signin_ui.py",
    "tests/test_auth_screens.py",
    "tests/test_provider_auth.py",
    "tests/test_model_build.py",
    "tests/test_openai_chatgpt_auth.py",
    "tests/test_session_model_restore.py",
    "tests/test_session_provider_restore.py",
]


def run(arguments: list[str]) -> None:
    subprocess.run([sys.executable, *arguments], cwd=ROOT, check=True)  # noqa: S603


def mutations() -> None:
    original = AUTH.read_text(encoding="utf-8")
    cases = [
        (
            "callback state",
            'hmac.compare_digest(query["state"][0], state)',
            "True",
            "test_real_loopback_state_pkce_and_no_implicit_save",
        ),
        (
            "token endpoint",
            'data.get("token_uri") == TOKEN_URI',
            "True",
            "test_corrupt_or_redirected_saved_tokens_not_usable",
        ),
        (
            "request endpoint",
            'request.url.host != "generativelanguage.googleapis.com"',
            "False",
            "test_bearer_auth_sync_async_removes_api_key_and_blocks_other_hosts",
        ),
    ]
    for name, before, after, test in cases:
        if original.count(before) != 1:
            raise RuntimeError(f"Missing or ambiguous mutation: {name}")
        try:
            AUTH.write_text(original.replace(before, after), encoding="utf-8")
            with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
                report = Path(temporary) / "result.xml"
                result = subprocess.run(  # noqa: S603
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        f"tests/test_google_oauth_auth.py::{test}",
                        "-q",
                        "-p",
                        "no:cacheprovider",
                        "--tb=line",
                        f"--junitxml={report}",
                        f"--basetemp={temporary}/pytest",
                    ],
                    cwd=ROOT,
                    check=False,
                    timeout=60,
                )
                executed = list(ET.parse(report).iter("testcase")) if report.exists() else []
                if (
                    result.returncode != 1
                    or len(executed) != 1
                    or executed[0].find("failure") is None
                    or executed[0].find("error") is not None
                ):
                    raise RuntimeError(f"Mutation was not detected by an assertion: {name}")
            print(f"Detected mutation: {name}")  # noqa: T201
        finally:
            AUTH.write_text(original, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutations", action="store_true")
    args = parser.parse_args()
    (ROOT / ".tmp").mkdir(exist_ok=True)
    if args.mutations:
        mutations()
    run(
        [
            "-m",
            "ruff",
            "check",
            "--select",
            "E,F,I,UP,B,ASYNC",
            "novacode_cli/config/google_oauth_auth.py",
            "novacode_cli/tui/provider_signin.py",
            *TESTS[:2],
            "scripts/verify_provider_signin.py",
        ]
    )
    run(
        [
            "-m",
            "mypy",
            "--follow-imports=skip",
            "--ignore-missing-imports",
            "--disable-error-code=import-untyped",
            "--disable-error-code=misc",
            "--disable-error-code=untyped-decorator",
            str(AUTH),
            "novacode_cli/tui/provider_signin.py",
        ]
    )
    with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
        run(
            [
                "-m",
                "coverage",
                "run",
                "--branch",
                "--source=novacode_cli.config.google_oauth_auth",
                "-m",
                "pytest",
                *TESTS,
                "-q",
                "-p",
                "no:cacheprovider",
                f"--basetemp={temporary}/pytest",
                "--tb=short",
            ]
        )
    run(["-m", "coverage", "report", "-m", "--fail-under=95"])
    print(
        json.dumps(
            {  # noqa: T201
                "auth_source_sha256": hashlib.sha256(AUTH.read_bytes()).hexdigest(),
                "spec": "tests/test_google_oauth_auth.py",
                "spec_approval": "not obtained (autonomous run)",
                "mutations": "3 detected" if args.mutations else "not run",
                "known_limits": [
                    "No live Google account login",
                    "SDK typing excluded from static check",
                    "Full suite blocked by installed Deep Agents API mismatch",
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
