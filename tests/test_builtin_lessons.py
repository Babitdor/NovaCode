"""The built-in lessons remain available through explicit memory search."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from novacode_cli.memory import builtin_lessons
from novacode_cli.memory.agent_memory import AgentMemoryMiddleware
from novacode_cli.memory.search import MemorySearchService


@pytest.fixture
def mw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentMemoryMiddleware:
    monkeypatch.setenv("NOVA_BUILTIN_LESSONS", "1")
    middleware = object.__new__(AgentMemoryMiddleware)
    middleware.agent_dir = tmp_path
    middleware._project_key = ""
    middleware._memory_search_service = MemorySearchService(tmp_path)
    return middleware


def test_builtin_lessons_can_be_disabled(
    mw: AgentMemoryMiddleware, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_BUILTIN_LESSONS", "0")

    assert "No topic memory yet" in asyncio.run(mw._memory_search("python3 command not found"))


def test_every_builtin_leads_with_its_symptom() -> None:
    for lesson in builtin_lessons.LESSONS:
        assert lesson.startswith("- **Symptom:**"), lesson
        assert "**Fix:**" in lesson, lesson
