"""Scan Git-tracked text for credential formats, printing locations only.

This deliberately narrow check cannot detect every secret or certify that a
candidate is live. Exit 1 for candidates, 2 for an unreadable tracked input.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "github-fine-grained-token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{70,}\b"),
    "anthropic-key": re.compile(r"\bsk-ant-api\d+-[A-Za-z0-9_-]{70,}\b"),
    "openai-key": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
}
TEXT_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".json", ".md", ".toml", ".yaml", ".yml",
                 ".txt", ".ini", ".cfg", ".env", ".jinja", ".sh", ".ps1", ".example", ".template"}


def scan(text):
    return [(kind, text.count("\n", 0, match.start()) + 1)
            for kind, pattern in PATTERNS.items() for match in pattern.finditer(text)]


def main():
    # Known-format dummy and nonmatching control prove the detector can flag a
    # candidate and reject an ordinary token; these are not live credentials.
    assert scan("ghp_" + "A" * 36) == [("github-token", 1)]
    assert not scan("normal configuration value")
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                            check=True, timeout=10)
    findings, errors, checked = [], [], 0
    source_hash = hashlib.sha256()
    for raw in sorted(result.stdout.decode().split("\0")):
        if not raw:
            continue
        path = ROOT / raw
        try:
            content = path.read_bytes()
        except OSError as error:
            errors.append({"path": raw, "error_type": type(error).__name__})
            continue
        if raw.startswith("novacode_cli/") or raw in {"pyproject.toml", "uv.lock"}:
            source_hash.update(raw.encode() + b"\0" + hashlib.sha256(content).digest())
        if path.suffix.lower() not in TEXT_SUFFIXES and not path.name.startswith(".env"):
            continue
        checked += 1
        for kind, line in scan(content.decode("utf-8", errors="replace")):
            findings.append({"path": raw, "line": line, "kind": kind})
    report = {"tracked_text_files_checked": checked, "candidates": findings,
              "errors": errors, "application_tree_sha256": source_hash.hexdigest(),
              "limits": "Pattern scan only; ignores untracked files/history and does not validate credentials."}
    output = ROOT / "docs" / "security-audit" / "tracked-secrets.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 2 if errors else (1 if findings else 0)


if __name__ == "__main__":
    raise SystemExit(main())
