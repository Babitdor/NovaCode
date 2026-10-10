"""The lessons Nova ships, and the record of which lessons get used.

Across twelve unrelated tasks the only things that earned a place in the shared
pool were environment facts true for everyone, so those ship with Nova instead
of being relearned. And a memory that is only ever written to needs some
evidence of what is used: recalls are counted, and so is a recall at a failure
that then cleared.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from novacode_cli.memory import builtin_lessons, recall_stats
from novacode_cli.memory.agent_memory import AgentMemoryMiddleware

REQUEST = "Serve a static page with nginx on port 8080."


@pytest.fixture
def mw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentMemoryMiddleware:
    monkeypatch.setenv("NOVA_BUILTIN_LESSONS", "1")
    middleware = object.__new__(AgentMemoryMiddleware)
    middleware.agent_dir = tmp_path
    middleware._corpus_cache = middleware._corpus_sig = middleware._retrieval_cache = None
    return middleware


def _facing(tool_output: str | None) -> SimpleNamespace:
    messages = [{"role": "user", "content": REQUEST}]
    if tool_output is not None:
        messages += [{"role": "assistant", "content": ""}, {"role": "tool", "content": tool_output}]
    return SimpleNamespace(messages=messages)


# ── built-in lessons ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("E: Unable to locate package nginx\napt-get install failed, exit code: 100", "apt-get update"),
        ("bash: line 1: python3: command not found\nExit code: 127", "apt-get install -y python3"),
        ("error: externally-managed-environment\npip install failed", "--break-system-packages"),
        ("E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 1959", "do not delete the lock"),
    ],
)
def test_a_fresh_install_already_knows_the_common_environment_fixes(mw, error, expected) -> None:
    assert expected in mw._relevant_memories(_facing(error))


def test_they_are_not_offered_until_something_goes_wrong(mw) -> None:
    assert mw._relevant_memories(_facing(None)) == ""
    assert mw._relevant_memories(_facing("total 8\n-rw-r--r-- 1 root root 612 nginx.conf")) == ""


def test_every_builtin_leads_with_its_symptom() -> None:
    for lesson in builtin_lessons.LESSONS:
        assert lesson.startswith("- **Symptom:**"), lesson
        assert "**Fix:**" in lesson, lesson


def test_they_can_be_switched_off(mw, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_BUILTIN_LESSONS", "0")
    assert mw._relevant_memories(_facing("E: Unable to locate package nginx, exit code: 100")) == ""


def test_memory_search_does_not_point_at_a_file_that_is_not_there(mw) -> None:
    import asyncio

    out = asyncio.run(mw._memory_search("apt-get"))
    assert builtin_lessons.TOPIC not in out


# ── which lessons get used ─────────────────────────────────────────────────


def test_a_recall_is_counted_once_per_moment(mw, tmp_path: Path) -> None:
    error = _facing("E: Unable to locate package nginx\napt-get install failed, exit code: 100")
    mw._relevant_memories(error)
    mw._relevant_memories(error)  # the same moment again: a cache hit, not a second recall
    (top,) = [e for e in recall_stats.report(tmp_path) if "Unable to locate package" in e["text"]]
    assert top["recalls"] == 1 and top["resolved"] == 0


def test_a_failure_that_then_clears_is_credited_to_what_was_recalled(mw, tmp_path: Path) -> None:
    mw._relevant_memories(_facing("E: Unable to locate package nginx\napt-get install failed, exit code: 100"))
    mw._relevant_memories(_facing("Setting up nginx (1.24.0) ...\nProcessing triggers for libc-bin"))
    (top,) = [e for e in recall_stats.report(tmp_path) if "Unable to locate package" in e["text"]]
    assert top["resolved"] == 1


def test_a_failure_that_persists_is_not(mw, tmp_path: Path) -> None:
    mw._relevant_memories(_facing("E: Unable to locate package nginx\napt-get install failed, exit code: 100"))
    mw._relevant_memories(_facing("E: Unable to locate package nginx-full\nstill failed, exit code: 100"))
    assert all(e["resolved"] == 0 for e in recall_stats.report(tmp_path))


def test_lessons_never_recalled_can_be_listed(tmp_path: Path) -> None:
    used, idle = "- `apt-get update` before install", "- a fact nobody has needed"
    recall_stats.note_recalled(tmp_path, [used])
    assert recall_stats.unused(tmp_path, [used, idle]) == [idle]
