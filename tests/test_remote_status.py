"""Tests for the remote status line — a throttled, edit-in-place activity log."""

from __future__ import annotations

import asyncio

from novacode_cli.remote.bridge import categorize_tools
from novacode_cli.remote.status import RemoteStatusLine


class _FakeEdit:
    def __init__(self):
        self.calls: list[tuple[bool, str]] = []

    async def __call__(self, text, final=False):
        self.calls.append((final, text))

    @property
    def working(self):
        return [t for f, t in self.calls if not f]

    @property
    def final(self):
        return [t for f, t in self.calls if f]


def test_extract_response_returns_only_final_answer():
    """The chat answer is the LAST AI message — not the step-by-step narration."""
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from novacode_cli.remote.processor import _extract_response

    msgs = [
        HumanMessage("build the feature"),
        AIMessage(content="Now I'll read the config", id="a1"),  # narration
        ToolMessage(content="...", tool_call_id="t1"),
        AIMessage(content="Let me edit the file", id="a2"),  # narration
        ToolMessage(content="...", tool_call_id="t2"),
        AIMessage(content="Done — added the endpoint and a test.", id="a3"),  # answer
    ]
    state = SimpleNamespace(values={"messages": msgs})
    out = _extract_response(state, pre_msg_count=1)  # skip the human prompt
    assert out == "Done — added the endpoint and a test."


def test_categorize_tools():
    assert categorize_tools(["read_file", "read_file", "edit_file"]) == "read×2, edit"
    assert categorize_tools(["task", "execute"]) == "subagent, run"
    assert categorize_tools([]) == ""


class TestStatusLine:
    async def test_coalesces_and_summarizes(self):
        edit = _FakeEdit()
        s = RemoteStatusLine(edit, interval=0.05)
        s.start()
        for n in ["read_file", "read_file", "edit_file", "task"]:
            s.note(n)
        await asyncio.sleep(0.12)  # ~2 intervals
        # A burst of notes is one (or few) coalesced edits — not one per note.
        assert 1 <= len(edit.working) <= 3, edit.working
        assert "read_file" in edit.working[-1] and "subagent" in edit.working[-1]
        assert edit.working[-1].startswith("⚙️")

    async def test_finalize_settles_to_done_summary(self):
        edit = _FakeEdit()
        s = RemoteStatusLine(edit, interval=0.02)
        s.start()
        s.note("edit_file")
        s.note("execute")
        await s.finalize()
        assert edit.final, "a final edit was sent"
        last = edit.final[-1]
        assert last.startswith("✅") and "2 tools" in last
        assert "edit_file" in last and "execute" in last

    async def test_no_tools_finalizes_to_done(self):
        edit = _FakeEdit()
        s = RemoteStatusLine(edit, interval=0.02)
        s.start()
        await s.finalize()
        assert edit.final and edit.final[-1].startswith("✅ **Done**")
        assert "tool" not in edit.final[-1]

    async def test_todos_shown_as_live_checklist(self):
        edit = _FakeEdit()
        s = RemoteStatusLine(edit, interval=0.02)
        s.start()
        s.note_todos(
            [
                {"content": "Read config", "status": "completed"},
                {"content": "Add endpoint", "status": "in_progress"},
                {"content": "Write tests", "status": "pending"},
            ]
        )
        s.note("edit_file")
        content = s._content()
        # The plan renders as a checklist...
        assert "Read config" in content
        assert "Add endpoint" in content
        assert "Write tests" in content
        # ...with the tool log beneath.
        assert "⚙️" in content and "edit_file" in content
        # The plan is kept (with the done summary) on finalize.
        await s.finalize()
        last = edit.final[-1]
        assert "Write tests" in last and "✅" in last

    async def test_first_paint_is_working(self):
        edit = _FakeEdit()
        s = RemoteStatusLine(edit, interval=0.05)
        s.start()
        await asyncio.sleep(0.02)  # let the immediate first paint run
        assert edit.working and edit.working[0].startswith("⚙️ **Working**")
        await s.finalize()


async def test_processor_prepends_user_mention():
    from novacode_cli.remote.processor import remote_message_processor
    from novacode_cli.remote.bridge import RemoteMessage, RemotePlatform
    from types import SimpleNamespace

    replies = []
    async def fake_reply(text):
        replies.append(text)

    # Create a RemoteMessage with a user mention
    msg = RemoteMessage(
        platform=RemotePlatform.DISCORD,
        chat_id="123456",
        user_name="test_user",
        text="Hello agent",
        reply_fn=fake_reply,
        user_mention="<@123456>",
    )

    queue = asyncio.Queue()
    await queue.put(msg)

    # Mock execute_fn to simulate turn completion
    async def fake_execute(*args, **kwargs):
        pass

    # Mock agent and state
    class FakeState:
        values = {"messages": []}

    class FakeAgent:
        async def aget_state(self, config):
            return FakeState()

    session_state = SimpleNamespace(
        thread_id="test-thread",
        auto_approve=False,
        session_id="test-session"
    )

    # Start processor as a background task, wait for it to process the message, then cancel it
    task = asyncio.create_task(
        remote_message_processor(
            queue=queue,
            agent=FakeAgent(),
            assistant_id="assistant-123",
            session_state=session_state,
            console=SimpleNamespace(print=lambda *a, **kw: None),
            token_tracker=SimpleNamespace(),
            backend=SimpleNamespace(),
            image_tracker=SimpleNamespace(),
            seen_message_ids=set(),
            execute_fn=fake_execute,
        )
    )

    # Wait for the queue to be empty
    await queue.join()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Assert that the final reply starts with the user mention
    assert len(replies) == 1
    assert replies[0].startswith("<@123456>\n")


class TestActivityLog:
    """Tool calls show what they did and how they ended; reasoning is kept."""

    async def test_tool_lines_carry_detail_and_result(self):
        s = RemoteStatusLine(_FakeEdit(), interval=1)
        s.note("read_file", "read_file(process.py)", "c1")
        s.note("shell", 'shell("pytest -q")', "c2")
        s.note_result("c1")
        live = s._content()
        assert "✓ `read_file(process.py)`" in live
        assert '⏳ `shell("pytest -q")`' in live
        s.note_result("c2", error=True)
        assert '✗ `shell("pytest -q")`' in s._content()

    async def test_the_same_call_reported_twice_is_one_line(self):
        s = RemoteStatusLine(_FakeEdit(), interval=1)
        s.note("task", "task(...)", "c1")
        s.note("task", "🤖 reviewer", "c1")
        assert s._content().count("reviewer") == 1 and "task(...)" not in s._content()

    async def test_reasoning_is_folded_into_the_final_message_and_prose_is_not(self):
        edit = _FakeEdit()
        s = RemoteStatusLine(edit, interval=1)
        s.note_text("the loop drops the last row", kind="reasoning")
        s.note_text("Fixed it.", kind="text")
        await s.finalize()
        final = edit.final[-1]
        assert "**💭 Reasoning**" in final and "the loop drops the last row" in final
        assert "Fixed it." not in final, "the answer is its own message"

    async def test_prose_before_a_tool_call_is_narration(self):
        s = RemoteStatusLine(_FakeEdit(), interval=1)
        s.note_text("Let me read the file", kind="text")
        s.note("read_file", "read_file(a.py)", "c1")
        assert "Let me read the file" not in s._content()
