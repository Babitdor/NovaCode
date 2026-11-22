"""`/proactive` over the socket: list → actions → form/report, backed by the store.

No model, no SQLite file and no runtime threads: a fake store records calls and
the runtime is disabled. The screen loop is what this pins: every action maps to
one store method, validation happens before save, and leaving the screen is
explicit.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from novacode_cli.proactive import AgentSpec
from novacode_cli.ui.port_server import ServerPort


class _Store:
    def __init__(self) -> None:
        self.spec = AgentSpec(
            name="Release watcher",
            instructions="Look for release notes.",
            workspace="C:/work",
            enabled=False,
        )
        self.saved: list[tuple[Any, Any]] = []
        self.queued: list[tuple[str, str]] = []
        self.removed: list[str] = []

    def agents(self) -> list[tuple[str, Any]]:
        return [("agent-1", self.spec)]

    def save(self, key: str | None, spec: Any) -> str:  # noqa: ANN401 - FakeStore boundary
        self.saved.append((key, spec))
        self.spec = spec
        return key or "agent-new"

    def queue(self, key: str, context: str) -> None:
        self.queued.append((key, context))

    def history(self, _key: str) -> list[dict[str, Any]]:
        return [{"status": "complete", "trigger": "Manual", "created": 0, "result": "All good"}]

    def remove(self, key: str) -> None:
        self.removed.append(key)


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


def _port(answers: list[object]) -> tuple[ServerPort, list[Any], _Store]:
    asked: list[Any] = []
    queue = list(answers)
    store = _Store()

    async def ask_screen(spec: Any) -> Any:  # noqa: ANN401 - the answer is the caller's
        asked.append(spec)
        return queue.pop(0) if queue else None

    port = ServerPort(lambda _frame: None, _worker(), ask_screen=ask_screen)
    port._proactive_store = lambda: store  # type: ignore[method-assign]
    port._start_proactive_runtime = lambda _store: None  # type: ignore[method-assign]
    return port, asked, store


async def test_the_picker_lists_agents_new_and_close() -> None:
    port, asked, _ = _port([None])
    result = await port.run("/proactive", None)
    assert result.ok is True
    ids = [item["id"] for item in asked[0].payload["items"]]
    assert ids == ["__new__", "agent-1", "__close__"]
    assert "Release watcher" in asked[0].payload["items"][1]["label"]


async def test_new_agent_is_validated_and_saved_under_a_new_key() -> None:
    port, _, store = _port(
        [
            {"id": "__new__"},
            {"id": "edit"},
            {
                "name": "Docs watcher",
                "instructions": "Read the docs.",
                "workspace": "C:/work",
                "github": "",
                "pypi": "",
                "cron": "",
                "read_workspace": True,
                "watch_tags": False,
                "enabled": False,
            },
            {"id": "back"},
            {"id": "__close__"},
        ]
    )
    result = await port.run("/proactive", None)
    assert result.ok is True
    assert store.saved[0][0] is None, "a new agent has no key yet"
    saved = store.saved[0][1]
    assert saved.name == "Docs watcher"
    assert saved.read_workspace is True


async def test_a_run_queues_the_context() -> None:
    port, _, store = _port(
        [
            {"id": "agent-1"},
            {"id": "run"},
            {"context": "release 2.0"},
            {"id": "back"},
            {"id": "__close__"},
        ]
    )
    result = await port.run("/proactive", None)
    assert result.ok is True
    assert store.queued == [("agent-1", "release 2.0")]


async def test_history_opens_a_report_then_returns_to_actions() -> None:
    port, asked, _ = _port(
        [
            {"id": "agent-1"},
            {"id": "history"},
            None,
            {"id": "back"},
            {"id": "__close__"},
        ]
    )
    await port.run("/proactive", None)
    reports = [spec for spec in asked if spec.kind == "detail"]
    assert reports
    assert "All good" in reports[0].payload["markdown"]


async def test_remove_requires_confirmation() -> None:
    port, _, store = _port(
        [
            {"id": "agent-1"},
            {"id": "remove"},
            False,
            {"id": "back"},
            {"id": "__close__"},
        ]
    )
    await port.run("/proactive", None)
    assert store.removed == [], "a declined confirm removes nothing"

    port, _, store = _port([{"id": "agent-1"}, {"id": "remove"}, True, {"id": "__close__"}])
    await port.run("/proactive", None)
    assert store.removed == ["agent-1"]


async def test_an_invalid_definition_never_reaches_the_store() -> None:
    port, _, store = _port(
        [
            {"id": "__new__"},
            {"id": "edit"},
            {
                "name": "",
                "instructions": "x",
                "workspace": "C:/work",
                "github": "",
                "pypi": "",
                "cron": "",
                "read_workspace": False,
                "watch_tags": False,
                "enabled": False,
            },
            {"id": "back"},
            {"id": "__close__"},
        ]
    )
    await port.run("/proactive", None)
    assert store.saved == []


async def test_subcommands_refuse_explicitly() -> None:
    port, asked, _ = _port([])
    result = await port.run("/proactive", "list")
    assert result.ok is False
    assert "not ported" in result.message.lower()
    assert asked == []
