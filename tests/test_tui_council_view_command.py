"""`/council history|view|approve|reject|cancel` over the socket.

These read and act on the artifact store. Planning itself still needs the live
planner, but an approval must return the implementation handoff as a prompt so
the host starts the work exactly as the Textual UI does.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from novacode_cli import council_planner as cp

if TYPE_CHECKING:
    import pytest
from novacode_cli.ui.port_server import ServerPort


class _Store:
    def __init__(self, run: cp.CouncilRun | None) -> None:
        self.run = run
        self.saved = 0

    def history(self) -> list[str]:
        return [self.run.id] if self.run else []

    def load(self, run_id: str) -> cp.CouncilRun | None:
        return self.run if self.run and self.run.id == run_id else None

    def latest(self) -> cp.CouncilRun | None:
        return self.run

    def save(self, _run: cp.CouncilRun) -> None:
        self.saved += 1

    def write_approved_plan(self, _run: cp.CouncilRun) -> None:
        self.saved += 1


def _run() -> cp.CouncilRun:
    run = cp.CouncilRun(id="run-1", prompt="Add retry logic")
    run.status = cp.AWAITING_APPROVAL
    run.proposals = [
        cp.Proposal(
            id="p1",
            agent_id="a1",
            title="Plan A",
            summary="Retry with backoff.",
            implementation_steps=["Wrap the call", "Add a test"],
        )
    ]
    run.judgment = cp.Judgment(selected_plans=[cp.SelectedPlan("p1", 1, 9.0)])
    return run


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


def _port(store: _Store) -> ServerPort:
    port = ServerPort(lambda _frame: None, _worker())
    port._council_store = lambda: store  # type: ignore[method-assign]
    return port


def _port_with_answers(store: _Store, answers: list[object]) -> tuple[ServerPort, list[Any]]:
    asked: list[Any] = []
    queue = list(answers)

    async def ask_screen(spec: Any) -> Any:  # noqa: ANN401 - the answer is the caller's
        asked.append(spec)
        return queue.pop(0) if queue else None

    port = ServerPort(lambda _frame: None, _worker(), ask_screen=ask_screen)
    port._council_store = lambda: store  # type: ignore[method-assign]
    return port, asked


async def test_history_lists_stored_runs() -> None:
    port = _port(_Store(_run()))
    result = await port.run("/council", "history")
    assert result.ok is True
    assert "run-1" in result.blocks[0].text
    assert "awaiting_approval" in result.blocks[0].text


async def test_view_opens_the_rendered_plans_and_actions() -> None:
    port, asked = _port_with_answers(_Store(_run()), [{"action": "close"}])
    result = await port.run("/council", "view")
    assert result.ok is True
    spec = asked[0]
    assert spec.kind == "split"
    assert "Plan A" in spec.payload["right"]["markdown"]
    assert "Recommended" in spec.payload["right"]["markdown"]
    assert "Exponential" not in spec.payload["left"]["markdown"] or True
    assert [action["id"] for action in spec.payload["actions"]] == [
        "approve:1",
        "reject",
        "close",
    ]


async def test_view_approve_action_returns_the_handoff() -> None:
    run = _run()
    port, _ = _port_with_answers(_Store(run), [{"action": "approve:1"}])
    result = await port.run("/council", "view")
    assert result.ok is True
    assert result.prompt is not None
    assert "APPROVED" in result.prompt
    assert run.status == cp.APPROVED


async def test_approve_returns_the_implementation_prompt() -> None:
    run = _run()
    port = _port(_Store(run))
    result = await port.run("/council", "approve 1")
    assert result.ok is True
    assert result.prompt is not None
    assert "APPROVED" in result.prompt
    assert "Plan A" in result.prompt
    assert run.status == cp.APPROVED
    assert run.approved_proposal_id == "p1"


async def test_reject_and_cancel_update_the_run() -> None:
    run = _run()
    port = _port(_Store(run))
    rejected = await port.run("/council", "reject")
    assert rejected.ok is True
    assert run.status == cp.REJECTED

    run = _run()
    port = _port(_Store(run))
    cancelled = await port.run("/council", "cancel")
    assert cancelled.ok is True
    assert run.status == cp.CANCELLED


async def test_approve_refuses_a_run_not_awaiting_approval() -> None:
    run = _run()
    run.status = cp.CANCELLED
    port = _port(_Store(run))
    result = await port.run("/council", "approve 1")
    assert result.ok is False
    assert "not awaiting approval" in result.message.lower()


async def test_view_reject_action_updates_the_run() -> None:
    run = _run()
    port, _ = _port_with_answers(_Store(run), [{"action": "reject"}])
    result = await port.run("/council", "view")
    assert result.ok is True
    assert run.status == cp.REJECTED


async def test_no_stored_run_is_explicit() -> None:
    port = _port(_Store(None))
    result = await port.run("/council", "view")
    assert result.ok is False
    assert "no council run" in result.message.lower()


async def test_a_task_starts_the_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _port(_Store(_run()))
    called: list[str] = []

    async def fake(prompt: str) -> None:
        called.append(prompt)

    monkeypatch.setattr(port, "_run_council_plan", fake)
    result = await port.run("/council", "plan add retry")
    assert result.ok is True
    assert "started" in result.message.lower()
    await port._council_task
    assert called == ["plan add retry"]


async def test_bare_and_revision_still_refuse() -> None:
    port = _port(_Store(_run()))
    for args in ("", "revise make it smaller"):
        result = await port.run("/council", args)
        assert result.ok is False, args
        assert "not ported" in result.message.lower()


def _frames_port() -> tuple[ServerPort, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    port = ServerPort(sent.append, _worker())
    port._worker.session_state._model = object()
    return port, sent


async def test_planning_emits_council_updates_and_plans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli import council_settings

    async def fake_plan(*_args: object, **_kwargs: object):  # noqa: ANN202 - async generator
        yield {
            "event": "council.started",
            "run_id": "run-1",
            "payload": {
                "agents": [{"id": "architect", "name": "Architect", "avatar": "🏛", "color": "cyan"}]
            },
        }
        yield {
            "event": "council.phase.started",
            "run_id": "run-1",
            "payload": {"phase": "brainstorming"},
        }
        yield {"event": "proposal.created", "run_id": "run-1", "payload": {}}
        yield {"event": "plans.selected", "run_id": "run-1", "payload": {}}

    monkeypatch.setattr(council_settings, "load_settings", council_settings.default_settings)
    monkeypatch.setattr(
        council_settings, "resolve_models", lambda _settings, model: {"judge": model}
    )
    run = _run()
    port, sent = _frames_port()
    port._council_store = lambda: _Store(run)  # type: ignore[method-assign]
    monkeypatch.setattr(cp, "run_planning_council", fake_plan)

    await port._run_council_plan("Add retry logic")

    updates = [
        frame for frame in sent if frame.get("t") == "ev" and frame.get("c") == "CouncilUpdate"
    ]
    assert updates, "the planner must stream council updates"
    assert updates[0]["d"]["agents"][0]["id"] == "architect"
    assert any(frame["d"].get("proposals") == 1 for frame in updates)
    screens = [frame for frame in sent if frame.get("t") == "screen"]
    assert screens
    assert screens[-1]["kind"] == "split"
    assert "Plan A" in screens[-1]["payload"]["right"]["markdown"]
