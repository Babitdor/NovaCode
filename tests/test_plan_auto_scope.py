"""Plan-scoped auto-approve must reset after the plan run."""
import asyncio
from types import SimpleNamespace

from novacode_cli.tui.app import NovaApp


def _stub(approved_plan, auto_approve=True, scoped=True):
    logs = []
    ran = []
    events = []

    class _SS(SimpleNamespace):
        def consume_approved_plan(self):
            events.append("consume")
            return approved_plan
        def clear_plan_agent(self):
            events.append("clear-plan-agent")
        def reset_conversation(self):
            events.append("reset")
            self.thread_id = "fresh-thread"
            self.todos = None

    class _Agent:
        async def aupdate_state(self, *, config, values, as_node=None):
            events.append("seed-todos")
            self.seeded = (config, values, as_node)

    stub = SimpleNamespace(
        session_state=_SS(auto_approve=auto_approve, thread_id="old-thread", todos=None),
        agent=_Agent(),
        _plan_scoped_auto_approve=scoped,
        _log=lambda *a, **k: logs.append(a),
        _paint_todos=lambda todos: events.append("paint-todos"),
        events=events,
    )
    async def _sp(text):
        events.append("stream")
        ran.append(text)
    stub._stream_prompt = _sp
    async def _noop_remove():
        return None

    stub._transcript = lambda: SimpleNamespace(remove_children=lambda: _noop_remove())
    stub._show_home_banner = lambda: None
    return stub, ran


def test_auto_approve_restored_after_plan_run():
    stub, ran = _stub("# My Plan\n1. do thing")
    asyncio.run(NovaApp._maybe_run_approved_plan(stub))
    assert ran, "plan should have executed"
    assert stub.session_state.auto_approve is False, "auto_approve must reset after plan run"
    assert stub._plan_scoped_auto_approve is False


def test_auto_approve_restored_even_without_plan_content():
    stub, ran = _stub(None)
    asyncio.run(NovaApp._maybe_run_approved_plan(stub))
    assert not ran
    assert stub.session_state.auto_approve is False


def test_global_auto_approve_untouched():
    # User had auto-approve on globally (not plan-scoped) -> keep it.
    stub, _ = _stub("# P", auto_approve=True, scoped=False)
    asyncio.run(NovaApp._maybe_run_approved_plan(stub))
    assert stub.session_state.auto_approve is True


def test_approved_plan_resets_context_and_seeds_protected_steps_before_execution():
    plan = "# Parser\n\n## Steps\n### 1. Update the parser\n### 2. Verify compatibility\n"
    stub, ran = _stub(plan, auto_approve=False, scoped=False)

    asyncio.run(NovaApp._maybe_run_approved_plan(stub))

    assert stub.events.index("reset") < stub.events.index("seed-todos") < stub.events.index("stream")
    assert stub.agent.seeded[0] == {"configurable": {"thread_id": "fresh-thread"}}
    assert stub.agent.seeded[1]["todos"] == [
        {"content": "Update the parser", "status": "in_progress"},
        {"content": "Verify compatibility", "status": "pending"},
    ]
    assert plan in ran[0]
    assert stub.session_state.todos == stub.agent.seeded[1]["todos"]
