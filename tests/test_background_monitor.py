"""Background commands the agent launched report back when they finish.

A job the agent started with ``background=True`` used to finish silently: the
completion only appended to ``_pending_job_notes``, which is prepended to the
NEXT user message. If the user walked away, nothing happened — the agent never
learned its build/test/deploy had finished.

Only Ctrl+B-detached jobs (``resume_on_done``) auto-resumed. This extends that
to agent-launched jobs, while leaving user-launched ones (a dev server) alone.
"""

from __future__ import annotations

import asyncio

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False


def test_agent_launched_flag_defaults_off():
    """A plain job is user-launched until something says otherwise."""
    from novacode_cli.shell.jobs import JobRegistry

    reg = JobRegistry()
    job = reg.add("npm run dev", "shell", None)
    assert job.agent_launched is False
    assert job.resume_on_done is False


async def _run(*, agent_launched: bool, resume_on_done: bool, turn_active: bool) -> dict:
    """Complete one job and report whether the agent was resumed."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from test_tui_app import _FakeAgent, _SS

    from novacode_cli.shell import jobs as _jobs
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    _jobs.get_registry().reset()
    app = NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="m",
    )
    async with app.run_test(size=(100, 40)) as pilot:
        for _ in range(2):
            await pilot.pause()

        resumed: list = []
        app._continue_after_task = lambda job: resumed.append(job)
        app._turn_active = turn_active
        app._pending_job_notes.clear()

        reg = _jobs.get_registry()
        job = reg.add("pytest -q", "shell", None)
        job.agent_launched = agent_launched
        job.resume_on_done = resume_on_done
        app._pending_job_notes.clear()  # drop the observer's start-of-job note
        reg.complete(job.id, 0, "all tests passed\n")
        for _ in range(4):
            await pilot.pause()

        return {
            "resumed": len(resumed),
            "notes": list(app._pending_job_notes),
        }


def test_agent_launched_job_resumes_the_agent():
    """The gap this closes: the agent's own background work reports back."""
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_run(agent_launched=True, resume_on_done=False, turn_active=False))
    assert out["resumed"] == 1, "agent-launched job did not resume the agent"


def test_ctrl_b_detached_job_still_resumes():
    """The pre-existing path must keep working."""
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_run(agent_launched=False, resume_on_done=True, turn_active=False))
    assert out["resumed"] == 1


def test_user_launched_job_only_leaves_a_note():
    """A dev server the USER started should not hijack the agent."""
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_run(agent_launched=False, resume_on_done=False, turn_active=False))
    assert out["resumed"] == 0, "a user-launched job must not resume the agent"
    assert any("completed" in n for n in out["notes"]), out["notes"]


def test_no_resume_while_the_agent_is_mid_turn():
    """Resuming mid-turn would interleave two prompts on one thread."""
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_run(agent_launched=True, resume_on_done=False, turn_active=True))
    assert out["resumed"] == 0, "resumed while a turn was already active"
    assert any("completed" in n for n in out["notes"]), out["notes"]


def test_registry_observer_can_be_removed():
    from novacode_cli.shell.jobs import JobRegistry

    reg = JobRegistry()
    events: list[str] = []

    class ObserverOwner:
        def observe(self, event: str, _job: object) -> None:
            events.append(event)

    owner = ObserverOwner()
    reg.add_observer(owner.observe)
    reg.add_observer(owner.observe)
    reg.add("first", "shell")
    reg.remove_observer(owner.observe)
    reg.add("second", "shell")

    assert events == ["started"]
    assert reg._observers == []


def test_process_manager_unregisters_external_process():
    from novacode_cli.process_manager import ProcessInfo, ProcessManager

    manager = ProcessManager()
    info = ProcessInfo(pid=987654321, name="bg-test", command="true")
    manager.register_process(info)

    manager.unregister_process(info.pid)

    assert manager.get_process(info.pid) is None
    assert manager.get_by_name("bg-test") is None


# --- what the notes cost ---------------------------------------------------
# One turn that backgrounded 20 commands produced a 24 KB note block: the same
# command line twice per task, plus a paragraph of tool hints, and the user's
# real question arrived underneath it.


def test_job_note_is_one_line_led_by_its_task_id():
    from novacode_cli.tui.app import job_note

    note = job_note("task_61", "completed (exit 0)", "pytest -q")
    assert note.startswith("task_61: completed (exit 0)")
    assert "get_task_logs('task_61')" in note
    assert "\n" not in note


def test_job_note_trims_a_huge_command_but_keeps_the_id():
    from novacode_cli.tui.app import _JOB_NOTE_COMMAND_CHARS, job_note

    note = job_note("task_9", "failed (exit 1)", "python " + "x" * 5000)
    assert len(note) < _JOB_NOTE_COMMAND_CHARS + 120
    assert note.endswith("Read it with get_task_logs('task_9').")


def test_notes_block_keeps_the_newest_within_budget():
    from novacode_cli.tui.app import pending_job_notes_block

    notes = [
        f"task_{i}: done. Command: x. Read it with get_task_logs('task_{i}')." for i in range(200)
    ]
    block = pending_job_notes_block(notes, limit=500)
    assert len(block) <= 500
    assert "task_199" in block
    assert "task_0:" not in block
    assert "earlier note(s) omitted" in block
    # chronological: the survivor list keeps its order
    kept = [line for line in block.splitlines() if line.startswith("- task_")]
    assert kept == sorted(kept, key=lambda line: int(line.split("_")[1].split(":")[0]))


def test_notes_block_of_nothing_is_empty():
    from novacode_cli.tui.app import pending_job_notes_block

    assert pending_job_notes_block([]) == ""


def test_a_tasks_note_is_replaced_not_repeated():
    """`task_61: still running` then `task_61: completed` is one fact, not two."""
    from novacode_cli.tui.app import NovaApp, job_note

    app = object.__new__(NovaApp)
    app._pending_job_notes = []
    app._queue_pending_job_note(
        job_note("task_61", "still running (backgrounded mid-turn)", "pytest -q"),
        task_id="task_61",
    )
    app._queue_pending_job_note(
        job_note("task_61", "completed (exit 0)", "pytest -q"), task_id="task_61"
    )
    assert len(app._pending_job_notes) == 1
    assert "completed (exit 0)" in app._pending_job_notes[0]


def test_other_tasks_keep_their_own_notes():
    from novacode_cli.tui.app import NovaApp, job_note

    app = object.__new__(NovaApp)
    app._pending_job_notes = []
    app._queue_pending_job_note(job_note("task_1", "completed (exit 0)", "a"), task_id="task_1")
    app._queue_pending_job_note(job_note("task_2", "completed (exit 0)", "b"), task_id="task_2")
    assert len(app._pending_job_notes) == 2
