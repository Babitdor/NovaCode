"""Mutation probe: re-inject the placeholder-key defect and confirm the test fails."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

target = pathlib.Path("novacode_cli/agents/tool_verdicts.py")
backup = pathlib.Path("novacode_cli/agents/tool_verdicts.py.bak")
shutil.copy(target, backup)

original = target.read_text(encoding="utf-8")
broken = original.replace(
    "content=candidate.content,", 'content="x" * candidate.content_chars,'
)
if broken == original:
    print("FAIL: no substitution made, the probe is vacuous")
    backup.unlink()
    sys.exit(1)

target.write_text(broken, encoding="utf-8")
print("injected: verdicts stored under a placeholder key")

result = subprocess.run(
    [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_tool_verdicts.py",
        "-q",
        "-k",
        "findable_by_the_real_message",
        "--timeout=300",
    ],
    capture_output=True,
    text=True,
)
print("exit:", result.returncode)
print(result.stdout.strip().splitlines()[-6:] and "\n".join(result.stdout.strip().splitlines()[-6:]))

shutil.move(str(backup), str(target))
restored = target.read_text(encoding="utf-8")
print("restored byte-identical:", restored == original)
sys.exit(0 if result.returncode != 0 else 1)
