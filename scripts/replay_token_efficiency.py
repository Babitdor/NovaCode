"""Fixed offline assembled-request replay; no model success claims.

Run from the repository root with ``python -m scripts.replay_token_efficiency``.
Freeze baseline before source changes, then compare three sequential repeats.
Only synthetic content is written to the public report. --histories writes a
content-free private manifest under .tmp, without constructing a SessionManager.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import socket
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

# This standalone fixture runner uses heterogeneous JSON rows and executable
# contract assertions; it is never imported by the application.
# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, S101, PLR2004
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from novacode_cli.context.snapshot import ContextSnapshotMiddleware

CASES = (
    "short_edit",
    "multi_file",
    "corrections",
    "large_test_output",
    "reads_after_edits",
    "large_catalog",
    "memory",
    "approved_plan",
    "images",
    "cancel_resume",
    "background",
    "concurrent_sessions",
)


def pair(number, text, *, name="read_file", path="fixture.py"):
    """Build a paired tool call and observation."""
    call = {"id": f"call-{number}", "name": name, "args": {"file_path": path}}
    return [AIMessage("", tool_calls=[call]), ToolMessage(text, name=name, tool_call_id=call["id"])]


def fixture(case):
    """Return a stable synthetic conversation and tool roster."""
    messages = [HumanMessage("Implement the approved task. Keep API compatibility. No network.")]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object"},
            },
        }
    ]
    source = "def fixture(value):\n    return value + 1\n" * 100
    if case == "short_edit":
        messages += pair(0, "value = 1\n")
    elif case == "large_test_output":
        messages += pair(0, "PASS fixture\n" * 3000, name="execute")
    elif case == "images":
        messages += [
            HumanMessage(
                content=[
                    {"type": "text", "text": "Preserve this image."},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + "a" * 4000},
                    },
                ]
            )
        ]
    elif case == "large_catalog":
        tools += [
            {
                "type": "function",
                "function": {
                    "name": f"tool_{n}",
                    "description": "Synthetic optional capability " * 20,
                    "parameters": {"type": "object"},
                },
            }
            for n in range(60)
        ]
    else:
        for number in range(6):
            if case == "corrections" and number == 3:
                messages += [HumanMessage("Correction: preserve Unicode and the old signature.")]
            text = source + ("# edited\n" if case == "reads_after_edits" and number >= 3 else "")
            path = f"fixture-{number % 2}.py" if case == "multi_file" else "fixture.py"
            messages += pair(number, text, path=path)
        if case == "approved_plan":
            messages += [HumanMessage("Approved plan: edit fixture, run tests, report blockers.")]
        if case == "memory":
            messages += [HumanMessage("Explicit memory instruction: preserve the JSON encoding.")]
        if case == "background":
            messages += pair(
                20, "Task pending; resume with the same task ID.", name="check_async_task"
            )
        if case == "cancel_resume":
            messages += [
                AIMessage(
                    "",
                    tool_calls=[
                        {"id": "pending", "name": "read_file", "args": {"file_path": "pending.py"}}
                    ],
                )
            ]
    return messages, tools


def blocked(*args, **kwargs):  # noqa: ARG001 - socket boundary signature
    """Fail closed if any replay attempts network access."""
    message = "Offline replay attempted a network connection"
    raise RuntimeError(message)


def measure():
    """Measure all request prefixes without invoking a model."""
    rows = []
    model = FakeListChatModel(responses=["offline"])
    for case in CASES:
        messages, tools = fixture(case)
        raw = [m.model_dump() for m in messages]
        corpus = hashlib.sha256(json.dumps([raw, tools], sort_keys=True).encode()).hexdigest()
        request = ModelRequest(
            model=model,
            messages=messages,
            tools=tools,
            state={"messages": messages},
            system_message=SystemMessage("Follow user instructions and retain tools."),
        )
        snapshots = []
        started = time.perf_counter()
        # Replay each growing prefix, plus auxiliary verification/learning calls.
        for stop in [
            n
            for n in range(1, len(messages) + 1)
            if n == len(messages) or isinstance(messages[n - 1], ToolMessage)
        ]:
            dispatched = []

            def handler(actual, dispatched=dispatched):
                dispatched.append(actual)
                return ModelResponse(result=[AIMessage("offline")])

            response = ContextSnapshotMiddleware().wrap_model_call(
                request.override(messages=messages[:stop]), handler
            )
            diagnostic = response.command.update["_context_snapshot"]
            actual = dispatched[0]
            assert actual.tools == tools
            for before, after in zip(messages[:stop], actual.messages, strict=True):
                if not isinstance(before, ToolMessage):
                    assert before == after
                else:
                    assert before.tool_call_id == after.tool_call_id
                    if before.content != after.content:
                        assert any(m.content == before.content for m in actual.messages)
            snapshots.append(diagnostic["estimated_input_tokens"])
        assert [m.model_dump() for m in messages] == raw
        # Auxiliary requests are fixed synthetic dispatch inputs, counted once each.
        from novacode_cli.context.snapshot import ContextSnapshot

        auxiliary = ContextSnapshot.capture(
            request.override(
                messages=[HumanMessage("Verify compatibility and report any missing requirements.")]
            )
        ).estimated_input_tokens
        completed_reads = {
            m.tool_call_id for m in messages if isinstance(m, ToolMessage) and m.name == "read_file"
        }
        read_arguments = [
            json.dumps(call["args"], sort_keys=True)
            for m in messages
            if isinstance(m, AIMessage)
            for call in m.tool_calls
            if call["name"] == "read_file" and call.get("id") in completed_reads
        ]
        rows.append(
            {
                "case": case,
                "fixture_sha256": corpus,
                "main_input_tokens": sum(snapshots),
                "auxiliary_input_tokens": 2 * auxiliary,
                "estimated_input_tokens": sum(snapshots) + 2 * auxiliary,
                "main_calls": len(snapshots),
                "auxiliary_calls": 2,
                "output_tokens": None,
                "read_calls": len(read_arguments),
                "reread_calls": len(read_arguments) - len(set(read_arguments)),
                "rereads": sum(
                    isinstance(m, ToolMessage) and m.name == "read_file" for m in messages
                ),
                "compactions": 0,
                "duration_ms": (time.perf_counter() - started) * 1000,
            }
        )
    return rows


def histories():  # noqa: PLR0912, PLR0915 - linear, read-only private audit
    """Inspect the frozen private sample read-only, retaining only counters."""
    root = Path.home() / ".nova" / "sessions"
    target = Path(".tmp/token-efficiency-history-manifest.json")
    previous = json.loads(target.read_text(encoding="utf-8")) if target.exists() else None
    files = (
        [root / row["session"] / "recent.jsonl" for row in previous]
        if previous
        else sorted(root.glob("*/recent.jsonl"), key=lambda p: (-p.stat().st_mtime_ns, str(p)))[:30]
    )
    manifest = []
    for recent in files:
        row = {"session": recent.parent.name, "files": []}
        outputs = []
        reads = []
        pending = {}
        changed = False
        row.update({"correction_messages": 0, "recovery_markers": 0, "content_chars": 0})
        for path in [recent.parent / "archive.jsonl", recent]:
            if not path.exists():
                continue
            data = path.read_bytes()
            entry = {
                "name": path.name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "messages": 0,
                "parse_errors": 0,
            }
            if previous:
                frozen = next(r for r in previous if r["session"] == recent.parent.name)
                expected = next(f for f in frozen["files"] if f["name"] == path.name)
                if entry["sha256"] != expected["sha256"]:
                    changed = True
                    break
            for line in data.splitlines():
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    entry["parse_errors"] += 1
                    continue
                entry["messages"] += 1
                content = json.dumps(message.get("content"), sort_keys=True)
                row["content_chars"] += len(content)
                if message.get("type") in {"human", "HumanMessage"}:
                    row["correction_messages"] += int(
                        any(
                            word in content.casefold()
                            for word in ("correction", "instead", "actually", "preserve")
                        )
                    )
                row["recovery_markers"] += int(
                    any(
                        word in content
                        for word in ("/cleared/", "Conversation Summary", "history_files")
                    )
                )
                for call in message.get("tool_calls", []):
                    pending[call.get("id")] = call
                if message.get("type") in {"tool", "ToolMessage"}:
                    outputs.append(hashlib.sha256(content.encode()).hexdigest())
                    call = pending.pop(message.get("tool_call_id"), {})
                    if call.get("name") == "read_file":
                        reads.append(
                            hashlib.sha256(
                                json.dumps(call.get("args"), sort_keys=True).encode()
                            ).hexdigest()
                        )
            row["files"].append(entry)
        if changed:
            manifest.append({"session": recent.parent.name, "status": "changed_since_freeze"})
            continue
        row["status"] = "unchanged"
        row["tool_results"] = len(outputs)
        row["duplicate_tool_results"] = len(outputs) - len(set(outputs))
        row["reads"] = len(reads)
        row["rereads"] = len(reads) - len(set(reads))
        manifest.append(row)
    target.parent.mkdir(parents=True, exist_ok=True)
    if previous is None:
        target.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    analysis = target.with_name("token-efficiency-history-analysis.json")
    analysis.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    fields = (
        "tool_results",
        "duplicate_tool_results",
        "reads",
        "rereads",
        "content_chars",
        "correction_messages",
        "recovery_markers",
    )
    return {
        "selected": len(manifest),
        "changed_since_freeze": sum(row["status"] != "unchanged" for row in manifest),
        **{key: sum(row.get(key, 0) for row in manifest) for key in fields},
    }


def main():
    """Write raw measurements and paired gate results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--histories", action="store_true")
    args = parser.parse_args()
    history_count = histories() if args.histories else None
    dependencies = {
        name: importlib.metadata.version(name)
        for name in ("langchain-core", "langchain", "langgraph", "deepagents")
    }
    with (
        tempfile.TemporaryDirectory() as profile,
        patch.dict(os.environ, {"USERPROFILE": profile, "NOVA_LOCAL_METRICS": "0"}),
        patch("pathlib.Path.home", return_value=Path(profile)),
        patch.object(socket.socket, "connect", blocked),
        patch.object(socket, "create_connection", blocked),
    ):
        runs = [measure() for _ in range(3)]
    result = {
        "baseline_revision": "9b9cf8f1ee50bc62e7358d54ef72f9fde312e272",
        "dependencies": dependencies,
        "history_sample_count": history_count,
        "provider_calls": 0,
        "measurement": "assembled-request estimates; no model invoked",
        "configuration_sha256": {
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in ("pyproject.toml", "uv.lock")
        },
        "source_sha256": {
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in (
                "novacode_cli/context/snapshot.py",
                "novacode_cli/context/observations.py",
                "novacode_cli/context/estimates.py",
                "novacode_cli/tracking/usage_tree.py",
                "novacode_cli/agents/context_compaction.py",
            )
            if Path(name).exists()
        },
        "runs": runs,
    }
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        assert dependencies == baseline["dependencies"]
        comparisons = []
        for before_run, after_run in zip(baseline["runs"], runs, strict=True):
            for before, after in zip(before_run, after_run, strict=True):
                assert before["fixture_sha256"] == after["fixture_sha256"]
                for key in ("main_calls", "auxiliary_calls", "rereads", "compactions"):
                    assert before[key] == after[key]
                comparisons.append(
                    {
                        "case": after["case"],
                        "before": before["estimated_input_tokens"],
                        "after": after["estimated_input_tokens"],
                        "reduction": 1
                        - after["estimated_input_tokens"] / before["estimated_input_tokens"],
                    }
                )
        result["comparisons"] = comparisons
        result["aggregate_reduction"] = 1 - sum(r["after"] for r in comparisons) / sum(
            r["before"] for r in comparisons
        )
        result["efficiency_gate"] = result["aggregate_reduction"] >= 0.10 and all(
            r["reduction"] >= -0.05 for r in comparisons
        )
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return int(args.baseline is not None and not result["efficiency_gate"])


if __name__ == "__main__":
    raise SystemExit(main())
