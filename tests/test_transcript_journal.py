"""A resumed session shows what the user ran, not just what the agent said.

``!`` commands and background jobs never enter the agent's message list, so a
resume used to bring back the chat without them.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "tests")


def test_entries_are_placed_after_the_turn_they_followed():
    from novacode_cli.session.transcript_journal import place

    u = lambda t: {"k": "user", "text": t}  # noqa: E731
    b = lambda c: {"k": "bash", "cmd": c}  # noqa: E731
    log = [b("pre"), u("one"), b("a"), u("two"), u("three"), b("c"), b("d")]

    per, lead = place(log, ["one", "two", "three"])
    assert lead == [b("pre")]
    assert per == [[b("a")], [], [b("c"), b("d")]]

    # "one" is no longer replayed (window / compaction): its entries go with it.
    per, lead = place(log, ["two", "three"])
    assert lead == [] and per == [[], [b("c"), b("d")]]

    # A session that only ran commands still gets them back.
    per, lead = place([b("x")], [])
    assert lead == [b("x")]


def test_a_torn_last_line_does_not_lose_the_journal(tmp_path):
    from novacode_cli.session import transcript_journal as tj

    tj.append(tmp_path, "s1", {"k": "bash", "cmd": "ls", "lines": ["a"] * 500, "exit": 0})
    tj.flush()
    with tj.path_for(tmp_path, "s1").open("a", encoding="utf-8") as f:
        f.write('{"k": "bash", "cmd": "half-writ')  # the process died mid-write
    entries = tj.load(tmp_path, "s1")
    assert [e["cmd"] for e in entries] == ["ls"]
    assert len(entries[0]["lines"]) == tj.MAX_LINES, "output is capped per command"
    assert tj.load(tmp_path, "no-such-session") == []


def test_a_bang_command_comes_back_on_resume(tmp_path):
    from langchain_core.messages import AIMessage, HumanMessage
    from novacode_cli.tui.widgets import OutputLog
    from textual.widgets import Static

    import test_tui_app as T
    from novacode_cli.session import transcript_journal as tj
    from novacode_cli.session.session_persistence import SessionManager
    from novacode_cli.tui.app import ChatMessage, NovaApp
    from novacode_cli.tui.widgets import CachedMarkdown as Markdown
    from novacode_cli.ui.ui_elements import TokenTracker

    sm = SessionManager(sessions_dir=tmp_path)

    def make(restored=None):
        ss = T._SS()
        ss.session_id = "sess-1"
        return NovaApp(
            agent=T._FakeAgent(), assistant_id="nova-agent", session_state=ss, backend=None,
            token_tracker=TokenTracker(), image_tracker=None, model_name="m", session_manager=sm,
            restored_messages=restored,
        )

    async def first_run() -> None:
        app = make()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app._add_message(app._user_label(), "user", Markdown("hello there"))
            await app._run_bash(f'!"{sys.executable}" -c "print(41+1)"')
            await app.workers.wait_for_complete()
            for _ in range(4):
                await pilot.pause()

    async def resumed() -> tuple[list[str], str, list[str]]:
        app = make([HumanMessage(content="hello there"), AIMessage(content="hi!")])
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            for _ in range(6):
                await pilot.pause()
            order = [
                "msg" if isinstance(w, ChatMessage) else "bash"
                for w in app.query_one("#transcript").children
                if isinstance(w, ChatMessage) or w.has_class("bash-inline")
            ]
            block = app.query_one(".bash-inline")
            head = str(block.query_one(".bash-inline-head", Static).render())
            return order, head, [s.text.rstrip() for s in block.query_one(OutputLog).lines]

    asyncio.run(first_run())
    tj.flush()
    kinds = [e["k"] for e in tj.load(tmp_path, "sess-1")]
    assert kinds == ["user", "bash"], kinds

    order, head, out = asyncio.run(resumed())
    assert order == ["msg", "msg", "bash"], "the command belongs after the turn it followed"
    assert head.startswith("! ") and "print(41+1)" in head
    assert out == ["  └  42"], out

    tj.flush()
    assert [e["k"] for e in tj.load(tmp_path, "sess-1")] == kinds, "a replay must not journal itself"
