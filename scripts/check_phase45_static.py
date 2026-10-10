"""Compare static findings with HEAD for files unchanged before Phase 4-5."""

from __future__ import annotations

# Fixed local commands; baseline source snapshots are temporary and never imported.
# ruff: noqa: INP001, T201, S603, S607, D103
import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

FILES = [
    "novacode_cli/agents/agent_file.py",
    "novacode_cli/agents/model_router.py",
    "novacode_cli/agents/tool_verdicts.py",
    "novacode_cli/skills/load.py",
    "novacode_cli/skills/refreshing_middleware.py",
    "novacode_cli/skills/skills_prefs.py",
    "novacode_cli/token_utils.py",
    "novacode_cli/compaction.py",
]


def checks(directory: Path, root: Path, scratch: Path) -> dict:
    findings = {}
    lint = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--config",
            str(root / "pyproject.toml"),
            "--output-format=json",
            *FILES,
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    if lint.returncode not in (0, 1) or not lint.stdout.strip():
        raise RuntimeError(lint.stderr)
    findings["lint"] = [
        f"{Path(item['filename']).relative_to(directory).as_posix()}:{item['code']}:{item['message']}"
        for item in json.loads(lint.stdout)
    ]
    types = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--follow-imports=skip",
            "--config-file",
            str(root / "pyproject.toml"),
            "--cache-dir",
            str(scratch / "mypy"),
            *FILES,
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    findings["types"] = [
        re.sub(r":\d+:", ":", line).replace("\\", "/")
        for line in types.stdout.splitlines()
        if ": error:" in line
    ]
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("phase45-static.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="nova-phase45-static-") as temporary:
        baseline = Path(temporary) / "baseline"
        for relative in FILES:
            file = baseline / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["git", "show", f"HEAD:{relative}"], cwd=root, capture_output=True, check=True
            )
            file.write_bytes(result.stdout)
        for package in ("novacode_cli", "novacode_cli/agents", "novacode_cli/skills"):
            if (root / package / "__init__.py").exists():
                (baseline / package / "__init__.py").write_text("", encoding="utf-8")
        before = checks(baseline, root, Path(temporary) / "before")
        after = checks(root, root, Path(temporary) / "after")
        new = {
            kind: list((Counter(after[kind]) - Counter(before[kind])).elements()) for kind in before
        }
        args.output.write_text(
            json.dumps({"baseline": before, "candidate": after, "new_findings": new}, indent=2),
            encoding="utf-8",
        )
        print(
            {
                kind: {
                    "baseline": len(before[kind]),
                    "candidate": len(after[kind]),
                    "new": len(new[kind]),
                }
                for kind in before
            }
        )
        return int(any(new.values()))


if __name__ == "__main__":
    raise SystemExit(main())
