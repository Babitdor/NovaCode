"""Reproduce offline cache/approval checks and optional restored mutations."""
# ruff: noqa: S101, S603, T201

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = [
    "tests/test_knowledge_cache.py",
    "tests/test_teammates.py",
    "tests/test_teammate_registry.py",
    "tests/test_teammates_ui.py",
    "tests/test_teammate_computer.py",
    "tests/test_teammate_updates.py",
    "tests/test_usage_tree.py",
    "tests/test_headless.py",
    "tests/test_headless_production.py",
    "tests/test_tool_search.py",
    "tests/test_native_code_mode.py",
    "tests/test_security_paths_audit.py",
    "tests/test_security_middleware.py",
    "tests/test_computer.py",
    "tests/test_deepagents_compat.py",
    "tests/test_token_efficiency.py",
]
NEW_MODULES = [
    "novacode_cli/context/knowledge_cache.py",
    "novacode_cli/tools/knowledge_tools.py",
    "novacode_cli/commands/cache_handler.py",
]
LINT = [
    *NEW_MODULES,
    "novacode_cli/teammates/controller.py",
    "novacode_cli/teammates/graph.py",
    "novacode_cli/teammates/registry.py",
    "novacode_cli/teammates/service.py",
    "novacode_cli/tui/teammates.py",
    *TESTS[:6],
    "scripts/replay_token_efficiency.py",
    "scripts/verify_knowledge_approval.py",
]
MUTATIONS = [
    (
        "complete source hashes",
        NEW_MODULES[1],
        "digest(content)",
        "digest(str(len(content)))",
        "tests/test_knowledge_cache.py::test_hash_validation_ignores_timestamps_and_deletion",
    ),
    (
        "credential exclusions",
        NEW_MODULES[1],
        "    parts = Path(relative).parts",
        "    return\n    parts = Path(relative).parts",
        "tests/test_knowledge_cache.py::test_private_or_escaping_sources_rejected",
    ),
    (
        "tool-free summaries",
        NEW_MODULES[1],
        'getattr(response, "tool_calls", None)',
        "False",
        "tests/test_knowledge_cache.py::test_invalid_model_response_not_published",
    ),
    (
        "default manual input",
        "novacode_cli/teammates/controller.py",
        'if self.data.get("auto_approve", False):',
        "if True:",
        "tests/test_teammates.py::test_enable_does_not_approve_pending_and_disable_gates_next",
    ),
    (
        "observation invalidation",
        "novacode_cli/teammates/controller.py",
        "epoch != self.input_epoch",
        "False",
        "tests/test_teammates.py::test_auto_approve_control_change_during_frame_preparation",
    ),
]


def run(arguments: list[str], *, expect_failure: bool = False) -> None:
    """Print each actual command and require its expected exit outcome."""
    print("RUN", sys.executable, *arguments, flush=True)
    result = subprocess.run([sys.executable, *arguments], cwd=ROOT, check=False)
    if expect_failure:
        if result.returncode != 1:
            message = f"Mutation must fail assertions (exit 1); got {result.returncode}"
            raise RuntimeError(message)
    else:
        result.check_returncode()


def mutations() -> None:
    """Execute five named defects, verifying byte-for-byte restoration each time."""
    for name, filename, before, after, test in MUTATIONS:
        path = ROOT / filename
        original = path.read_bytes()
        text = original.decode("utf-8")
        assert text.count(before) == 1, (name, "mutation anchor must be unique")
        print("MUTATION", name, flush=True)
        try:
            path.write_bytes(text.replace(before, after).encode("utf-8"))
            run(["-m", "pytest", test, "-q"], expect_failure=True)
        finally:
            path.write_bytes(original)
        assert path.read_bytes() == original
    print("MUTATIONS KILLED", len(MUTATIONS), flush=True)


INTEGRATIONS = [
    "novacode_cli/agents/core_agent.py",
    "novacode_cli/agents/tool_search.py",
    "novacode_cli/commands/__init__.py",
    "novacode_cli/computer/preview.py",
    "novacode_cli/skills/retrieval.py",
    "novacode_cli/tui/app.py",
]


def main() -> None:
    """Run focused proof without providers, weight downloads or real VMs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutations", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            {
                name: importlib.metadata.version(name)
                for name in ("pytest", "ruff", "mypy", "langchain-core", "deepagents")
            }
        )
    )
    source = b"".join(
        name.encode() + b"\0" + (ROOT / name).read_bytes()
        for name in sorted(set(LINT + INTEGRATIONS))
    )
    print("SOURCE", hashlib.sha256(source).hexdigest())
    run(["-m", "pytest", *TESTS, "-q"])
    run(["-m", "ruff", "check", *LINT])
    run(["-m", "ruff", "format", "--check", *LINT])
    run(["-m", "mypy", *NEW_MODULES, "--follow-imports", "skip"])
    run(["-m", "scripts.replay_token_efficiency", "--output", ".tmp/knowledge-token-replay.json"])
    if args.mutations:
        mutations()


if __name__ == "__main__":
    main()
