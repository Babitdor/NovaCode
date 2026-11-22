"""`/teammates`: roster, management actions and public task history.

The worker is a fake that records every action; no service is launched. Each test
proves one action maps to the documented worker payload.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from novacode_cli.teammates import service
from novacode_cli.ui.port_server import ServerPort

if TYPE_CHECKING:
    import pytest

STATUS = {
    "agents": [
        {
            "agent_id": "a1",
            "thread": "t1",
            "name": "Builder",
            "role": "Build things.",
            "model": {"provider": "openai", "model": "gpt-5"},
            "vision": {"provider": "", "model": ""},
            "task_state": "working",
            "computer": {"phase": "ready", "manual": True},
            "approvals": [],
            "tasks": [
                {
                    "id": "task-1",
                    "status": "completed",
                    "text": "Add retry logic",
                    "full_output": "Done: added retries.",
                }
            ],
        },
        {
            "agent_id": "a2",
            "thread": "t2",
            "name": "Reviewer",
            "task_state": "idle",
            "computer": {"phase": "not created", "manual": False},
            "approvals": [{"id": "ap1", "state": "pending", "text": "Approve network access"}],
            "tasks": [],
        },
    ]
}


def _worker() -> SimpleNamespace:
    return SimpleNamespace(
        agent=object(),
        session_state=SimpleNamespace(
            thread_id="t1", session_id="s1", auto_approve=False, todos=[], headless_out_fd=None
        ),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
    )


def _port(answers: list[object]) -> tuple[ServerPort, list[Any]]:
    asked: list[Any] = []
    queue = list(answers)

    async def ask_screen(spec: Any) -> Any:  # noqa: ANN401 - the answer is the caller's
        asked.append(spec)
        return queue.pop(0) if queue else None

    return ServerPort(lambda _frame: None, _worker(), ask_screen=ask_screen), asked


def _fake_request(calls: list[tuple[str, dict[str, Any]]]) -> Any:  # noqa: ANN401
    async def request(
        action: str, payload: dict | None = None, *, launch: bool = True
    ) -> dict:
        _ = launch
        calls.append((action, dict(payload or {})))
        if action == "status":
            return STATUS
        if action == "preview":
            return {"url": "http://127.0.0.1:9/preview"}
        return {"status": "ok"}

    return request


async def test_roster_lists_new_agents_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(service, "request", _fake_request(calls))
    port, asked = _port([None])
    result = await port.run("/teammates", None)
    assert result.ok is True
    items = asked[0].payload["items"]
    assert [item["id"] for item in items] == ["__new__", "a1", "a2", "__close__"]
    assert "input approval needed" in items[2]["detail"]


async def test_history_action_renders_public_task_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(service, "request", _fake_request(calls))
    port, asked = _port([{"id": "a1"}, {"id": "history"}, None, {"id": "__close__"}])
    await port.run("/teammates", None)
    details = [spec for spec in asked if spec.kind == "detail"]
    assert details
    markdown = details[0].payload["markdown"]
    assert "Add retry logic" in markdown
    assert "Done: added retries." in markdown
    assert "**Computer:** ready" in markdown


async def test_assign_sends_text_to_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(service, "request", _fake_request(calls))
    port, _ = _port(
        [
            {"id": "a1"},
            {"id": "assign"},
            {"text": "Review the parser"},
            {"id": "__close__"},
        ]
    )
    await port.run("/teammates", None)
    assert ("assign", {"agent_id": "a1", "text": "Review the parser"}) in calls


async def test_approve_and_deny_use_the_pending_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(service, "request", _fake_request(calls))
    port, _ = _port(
        [{"id": "a2"}, {"id": "approve:ap1"}, {"id": "a2"}, {"id": "deny:ap1"}, {"id": "__close__"}]
    )
    await port.run("/teammates", None)
    decisions = [payload for action, payload in calls if action == "decide"]
    assert decisions == [
        {"agent_id": "a2", "id": "ap1", "approve": True},
        {"agent_id": "a2", "id": "ap1", "approve": False},
    ]


async def test_create_uses_the_form_and_skips_inherited_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(service, "request", _fake_request(calls))
    port, _ = _port(
        [
            {"id": "__new__"},
            {
                "name": "Ada",
                "role": "Build things.",
                "model_provider": "Use Nova's provider",
                "model": "",
                "vision_provider": "Use Nova's provider",
                "vision_model": "",
            },
            {"id": "__close__"},
        ]
    )
    await port.run("/teammates", None)
    created = [payload for action, payload in calls if action == "create"]
    assert created == [{"name": "Ada", "role": "Build things."}]


async def test_pause_stop_and_preview_map_to_worker_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(service, "request", _fake_request(calls))
    port, asked = _port(
        [
            {"id": "a1"},
            {"id": "pause"},
            {"id": "a1"},
            {"id": "stop"},
            {"id": "a1"},
            {"id": "preview"},
            None,
            {"id": "__close__"},
        ]
    )
    await port.run("/teammates", None)
    assert ("pause", {"agent_id": "a1"}) in calls
    assert ("stop", {"agent_id": "a1"}) in calls
    assert ("preview", {"agent_id": "a1"}) in calls
    assert any(
        "http://127.0.0.1:9/preview" in spec.payload.get("markdown", "")
        for spec in asked
        if spec.kind == "detail"
    )


async def test_delete_requires_the_exact_typed_name(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(service, "request", _fake_request(calls))
    port, _ = _port(
        [{"id": "a1"}, {"id": "delete"}, {"confirm": "Builder"}, {"id": "__close__"}]
    )
    await port.run("/teammates", None)
    assert ("delete", {"agent_id": "a1", "confirm": "Builder"}) in calls


async def test_an_unavailable_worker_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_request(
        action: str, _payload: dict | None = None, *, launch: bool = True
    ) -> dict:
        _ = (action, launch)
        message = "worker stopped"
        raise OSError(message)

    monkeypatch.setattr(service, "request", fake_request)
    port, asked = _port([])
    result = await port.run("/teammates", None)
    assert result.ok is False
    assert "worker unavailable" in result.message.lower()
    assert asked == []


async def test_subcommands_refuse_explicitly() -> None:
    port, asked = _port([])
    result = await port.run("/teammates", "create")
    assert result.ok is False
    assert "not ported" in result.message.lower()
    assert asked == []
