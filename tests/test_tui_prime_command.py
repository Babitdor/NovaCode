"""`/prime` over the socket: mode toggle, report, saved-work browser, forget."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from novacode_cli import prime_memory
from novacode_cli.commands import prime_handler

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
from novacode_cli.ui.port_server import ServerPort


class _Store:
    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self.records = records or []

    def put(self, _ns: tuple, _id: str, value: dict[str, Any]) -> None:
        self.records.append(value)

    def get(self, _ns: tuple, _id: str) -> dict[str, Any] | None:
        return None

    def search(self, _ns: tuple, _limit: int = 200) -> list[Any]:
        return []

    def delete(self, _ns: tuple, _id: str) -> None:
        return None


def _worker() -> SimpleNamespace:
    state = SimpleNamespace(
        thread_id="t1",
        session_id="s1",
        auto_approve=False,
        todos=[],
        headless_out_fd=None,
        working_directory="C:/work",
        prime_enabled=False,
        prime_report=None,
        prime_factory=None,
        prime_forget_pending=None,
    )
    return SimpleNamespace(
        agent=object(),
        session_state=state,
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


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(prime_memory, "session_directory", lambda _session: str(tmp_path))
    monkeypatch.setattr(prime_handler, "factory_records", lambda *_a, **_k: [])


async def test_bare_prime_toggles_the_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(monkeypatch, tmp_path)
    port, _ = _port([])
    result = await port.run("/prime", None)
    assert result.ok is True
    assert "off" in result.blocks[0].text.lower() or "on" in result.blocks[0].text.lower()
    assert port._worker.session_state.prime_enabled is True


async def test_report_without_records_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    port, asked = _port([None])
    result = await port.run("/prime", "report")
    assert result.ok is True
    assert asked
    assert asked[0].kind == "detail"
    assert "No Prime delivery record" in asked[0].payload["markdown"]


async def test_tasks_opens_the_saved_work_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    port, asked = _port([None])
    result = await port.run("/prime", "tasks")
    assert result.ok is True
    assert asked
    assert asked[0].kind == "list"
    assert asked[0].payload["items"][0]["label"].startswith("No saved Prime work")


async def test_forget_of_unknown_record_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    port, _ = _port([])
    result = await port.run("/prime", "forget nope")
    assert result.ok is True
    assert "not found" in result.blocks[0].text.lower()


async def test_usage_text_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(monkeypatch, tmp_path)
    port, _ = _port([])
    result = await port.run("/prime", "bogus")
    message = result.blocks[0].text.lower()
    assert "usage" in message or result.ok is False
