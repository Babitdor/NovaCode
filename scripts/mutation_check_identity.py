"""Prove the new regression tests bite: re-inject each defect, expect a red test.

For every mutation: hash the file, apply the defect, run the named test, restore
the original bytes, and verify the hash is byte-identical afterwards. A mutation
that leaves the test GREEN means the test cannot detect that defect -- which is
the thing worth knowing.

Run: ``uv run python scripts/mutation_check_identity.py``
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OFFLOAD = ROOT / "novacode_cli/agents/tool_offload.py"
CONFIG = ROOT / "novacode_cli/config/nova_config.py"

#: (label, path, [(old, new), ...], test id, why the test should go red)
MUTATIONS: list[tuple[str, Path, list[tuple[str, str]], str, str]] = [
    (
        "M1 drop the payload digest from the offload filename",
        OFFLOAD,
        [
            (
                '    prefix = _SAFE.sub("-", f"{name or \'tool\'}-{call_id}")[:64]\n'
                '    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]\n'
                '    return f"{prefix}-{digest}"',
                "    return _SAFE.sub(\"-\", f\"{name or 'tool'}-{call_id}\")[:80]",
            )
        ],
        "tests/test_compaction_tail.py::test_a_reused_tool_call_id_offloads_each_payload_separately",
        "the second result overwrites the first's file",
    ),
    (
        "M2 key `before` by tool_call_id again (verdict edit)",
        OFFLOAD,
        [
            (
                "            idx: (msg.name, msg.content)\n"
                "            for idx, msg in enumerate(messages)\n"
                "            if isinstance(msg, ToolMessage)\n"
                "        }\n"
                "        judged = self._judged_stale_ids(messages)",
                "            msg.tool_call_id: (msg.name, msg.content)\n"
                "            for msg in messages\n"
                "            if isinstance(msg, ToolMessage)\n"
                "        }\n"
                "        judged = self._judged_stale_ids(messages)",
            ),
            (
                "            name, payload = before.get(index, (None, None))",
                "            name, payload = before.get(msg.tool_call_id, (None, None))",
            ),
        ],
        "tests/test_verdict_tool_uses_edit.py::test_a_reused_tool_call_id_keeps_each_results_own_content",
        "both results are restored from the one payload kept per id",
    ),
    (
        "M3 track `judged` by tool_call_id again",
        OFFLOAD,
        [
            ("        judged: set[int] = set()", "        judged: set[str] = set()"),
            (
                "            if stale:\n                judged.add(index)",
                "            if stale:\n                judged.add(message.tool_call_id)",
            ),
            (
                "            if index in judged:\n                continue",
                "            if msg.tool_call_id in judged:\n                continue",
            ),
        ],
        "tests/test_verdict_tool_uses_edit.py::test_a_stale_verdict_does_not_clear_a_same_id_sibling",
        "one stale verdict clears every result sharing the id",
    ),
    (
        "M4 back to a bare bool() on the flag",
        CONFIG,
        [
            (
                '        return _config_bool(self._config.get("tool_verdicts_enabled", False))',
                '        return bool(self._config.get("tool_verdicts_enabled", False))',
            )
        ],
        "tests/test_tool_verdict_flag.py::test_only_an_explicit_yes_enables_the_flag",
        'a hand-edited "false" turns the feature ON',
    ),
]


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    failures = 0
    pristine = {path: _hash(path) for path in {m[1] for m in MUTATIONS}}
    for label, path, edits, test_id, why in MUTATIONS:
        original = path.read_bytes()
        before = _hash(path)
        text = original.decode("utf-8")
        for old, new in edits:
            if text.count(old) != 1:
                print(f"  !! anchor not unique ({text.count(old)}x) for {label}")
                failures += 1
                break
            text = text.replace(old, new)
        else:
            path.write_bytes(text.encode("utf-8"))
            try:
                run = subprocess.run(
                    [sys.executable, "-m", "pytest", test_id, "-q", "--timeout=300", "-x"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                caught = run.returncode != 0
            finally:
                path.write_bytes(original)

            status = "CAUGHT" if caught else "MISSED (test still passes!)"
            if not caught:
                failures += 1
            tail = [ln for ln in run.stdout.strip().splitlines() if ln.strip()][-1:]
            print(f"  {status:26} {label}")
            print(f"    why: {why}")
            print(f"    {tail[0] if tail else ''}")
            continue
        path.write_bytes(original)

    # Every file must be byte-identical to how it started.
    for path, want in pristine.items():
        got = _hash(path)
        ok = "IDENTICAL" if got == want else "*** CHANGED ***"
        if got != want:
            failures += 1
        print(f"  restored {path.name}: {ok} (sha256={got[:16]})")

    print()
    print(f"mutations missed: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
