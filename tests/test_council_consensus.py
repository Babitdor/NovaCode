"""Web-only parallel analysis and chairman consensus, using synthetic providers."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from novacode_cli import council
from novacode_cli.council_consensus import resolve_chairman, run_web_consensus
from novacode_cli.council_settings import default_settings


@pytest.mark.asyncio
async def test_members_start_in_parallel_and_cancel_cleanly(monkeypatch):
    started, finished = set(), set()
    all_started = asyncio.Event()

    async def stream(persona, conversation, model):
        started.add(persona.id)
        if len(started) == 3:
            all_started.set()
        try:
            await all_started.wait()
            yield "delta", "Perspective: " + persona.id
            await asyncio.Event().wait()
        finally:
            finished.add(persona.id)

    monkeypatch.setattr(council, "_stream_persona_turn", stream)
    stream = council.run_council("Choose", object(), council.PERSONAS[:3], advice_only=True)
    async with asyncio.timeout(2):
        async for event in stream:
            if event["type"] == "agent_delta":
                assert len(started) == 3
                break
    await stream.aclose()
    assert finished == started


async def debate():
    yield {"type": "council_start", "topic": "Choose A or B; priority: safety", "agents": []}
    yield {"type": "agent_done", "id": "architect", "text": "A is fast, but lacks isolation."}
    yield {
        "type": "agent_done",
        "id": "skeptic",
        "text": "B offers isolation. Verify the vendor claim.",
    }
    yield {"type": "vote", "voter": "architect", "choice": "A", "reason": "Speed"}
    yield {"type": "vote", "voter": "skeptic", "choice": "B", "reason": "Safety"}
    yield {
        "type": "verdict",
        "winner_id": "",
        "winner_name": "No consensus",
        "votes": 2,
        "tied": True,
        "option_tally": {"A": 1, "B": 1},
        "answer": "Split vote",
    }
    yield {"type": "done"}


@pytest.mark.asyncio
async def test_chairman_receives_analyses_votes_and_contradictions_before_final_answer():
    prompts = []

    class Chairman:
        async def astream(self, messages):
            prompts.append(messages)
            yield SimpleNamespace(content="Prefer B for isolation. ")
            yield SimpleNamespace(content="Verify the vendor claim before committing.")

    events = [
        e async for e in run_web_consensus("Choose", debate(), Chairman(), default_settings().judge)
    ]
    assert [e["type"] for e in events][-4:] == [
        "chairman_delta",
        "chairman_delta",
        "verdict",
        "done",
    ]
    assert sum(e["type"] == "verdict" for e in events) == 1
    assert any(e["type"] == "vote_result" for e in events)
    payload = json.loads(prompts[0][1][1])
    assert len(payload["member_analyses"]) == 2
    assert [v["reason"] for v in payload["ballots"]] == ["Speed", "Safety"]
    assert "contradictions" in prompts[0][0][1]
    verdict = next(e for e in events if e["type"] == "verdict")
    assert verdict["tied"] is True
    assert verdict["option_tally"] == {"A": 1, "B": 1}
    assert verdict["synthesized"]
    assert "Verify the vendor claim" in verdict["answer"]


@pytest.mark.asyncio
async def test_failed_chairman_does_not_turn_vote_into_fake_consensus():
    class Unavailable:
        async def astream(self, messages):
            raise RuntimeError("provider failed")
            yield

    events = [
        e
        async for e in run_web_consensus(
            "Choose", debate(), Unavailable(), default_settings().judge
        )
    ]
    assert not any(e["type"] == "verdict" for e in events)
    assert any(e["type"] == "vote_result" for e in events)
    assert events[-2]["type"] == "council_error"
    assert events[-1]["type"] == "done"


def test_chairman_reuses_matching_member_model():
    settings = default_settings()
    first = replace(settings.members[0], provider="openai", model="shared")
    settings = replace(
        settings,
        members=(first, *settings.members[1:]),
        judge=replace(settings.judge, provider="openai", model="shared"),
    )
    shared = object()
    assert resolve_chairman(settings, object(), {first.id: shared}) is shared


@pytest.mark.asyncio
async def test_closing_synthesis_closes_provider_stream():
    closed = []

    class StreamingChairman:
        async def astream(self, messages):
            try:
                yield SimpleNamespace(content="Partial synthesis")
                await asyncio.Event().wait()
            finally:
                closed.append(True)

    stream = run_web_consensus("Choose", debate(), StreamingChairman(), default_settings().judge)
    async for event in stream:
        if event["type"] == "chairman_delta":
            break
    await stream.aclose()
    assert closed == [True]
