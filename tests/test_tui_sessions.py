"""Session panes: per-session transcripts, buffering while hidden, state swap.

``NovaApp`` keeps the *current* conversation on itself (streaming message, open
tool cards, trackers). With several sessions those belong to whichever pane is on
screen, so switching swaps the bundle. That is only safe because a hidden pane
never renders — it buffers instead. These tests pin both halves:

* the active pane renders and ``_transcript()`` follows it;
* a hidden pane buffers, drops transient deltas, and replays on switch;
* switching saves the outgoing pane's state and restores the incoming one's.

No child process is involved: a second pane is mounted directly, which is exactly
what the spawn path will do once the tab bar lands.

Runnable directly (``python tests/test_tui_sessions.py``) or via pytest.
"""

from __future__ import annotations

import asyncio

import pytest

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False

pytestmark = pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")


class _SS:
    """Minimal session state (mirrors tests/test_tui_app.py)."""

    thread_id = "t1"
    session_id = "s-abcdef12"
    auto_approve = True
    plan_mode_enabled = False
    todos: list = []
    steering_instructions: list = []


class _FakeAgent:
    async def aget_state(self, config):
        class _V:
            values: dict = {"messages": []}

        return _V()

    async def astream(self, inp, **kw):
        return
        yield  # pragma: no cover

    async def aupdate_state(self, **kw):
        pass


def _app():
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    return NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="m",
    )


async def _add_pane(app, sid="child-1", title="child"):
    """Mount a second pane the way the spawn path will."""
    from textual.containers import VerticalScroll
    from textual.widgets import ContentSwitcher

    from novacode_cli.tui.session_pane import SessionPane

    scroll = VerticalScroll(id=f"pane-{sid}")
    await app.query_one("#panes", ContentSwitcher).mount(scroll)
    pane = SessionPane(sid=sid, title=title, scroll=scroll, kind="child")
    app._panes.append(pane)
    return pane


# ── root pane ────────────────────────────────────────────────────────────────


async def _drive_root_pane():
    app = _app()
    async with app.run_test():
        assert app._root_pane is app._active_pane
        assert app._root_pane.kind == "root"
        # The root pane wraps the original widget, so nothing changes for a
        # single-session run.
        assert app._transcript() is app.query_one("#transcript")
        assert app._panes == [app._root_pane]


def test_root_pane_wraps_the_existing_transcript():
    asyncio.run(_drive_root_pane())


async def _drive_transcript_follows_active():
    app = _app()
    async with app.run_test():
        other = await _add_pane(app)
        assert app._transcript() is app._root_pane.scroll
        await app._switch_to(other)
        assert app._transcript() is other.scroll
        assert app._transcript() is not app._root_pane.scroll


def test_transcript_follows_the_active_pane():
    asyncio.run(_drive_transcript_follows_active())


# ── delivery ─────────────────────────────────────────────────────────────────


async def _drive_active_renders():
    import novacode_cli.ui_events as ev

    app = _app()
    async with app.run_test() as pilot:
        before = len(app._root_pane.scroll.children)
        await app._deliver(
            app._root_pane,
            ev.AssistantMessage(text="hello", agent_name="nova", agent_color="cyan"),
        )
        await pilot.pause()
        assert len(app._root_pane.scroll.children) > before
        assert not app._root_pane.buffer  # rendered, not buffered
        assert app._root_pane.unread == 0


def test_active_pane_renders_immediately():
    asyncio.run(_drive_active_renders())


async def _drive_hidden_buffers():
    import novacode_cli.ui_events as ev

    app = _app()
    async with app.run_test() as pilot:
        hidden = await _add_pane(app)  # created but never switched to
        before = len(app._root_pane.scroll.children)

        await app._deliver(
            hidden, ev.AssistantMessage(text="bg work", agent_name="nova", agent_color="cyan")
        )
        await pilot.pause()

        # Nothing drew, least of all into the visible pane.
        assert len(app._root_pane.scroll.children) == before
        assert len(hidden.buffer) == 1
        assert hidden.unread == 1


def test_hidden_pane_buffers_instead_of_rendering():
    asyncio.run(_drive_hidden_buffers())


async def _drive_hidden_drops_deltas():
    import novacode_cli.ui_events as ev

    app = _app()
    async with app.run_test():
        hidden = await _add_pane(app)
        for _ in range(50):
            await app._deliver(hidden, ev.TextDelta(text="x"))
            await app._deliver(hidden, ev.ReasoningDelta(text="y"))
            await app._deliver(hidden, ev.StatusUpdate(message="thinking"))
        # Transient fragments are dropped; the committed message is what counts.
        assert len(hidden.buffer) == 0
        await app._deliver(
            hidden, ev.AssistantMessage(text="final", agent_name="n", agent_color="c")
        )
        assert len(hidden.buffer) == 1


def test_hidden_pane_drops_transient_deltas():
    asyncio.run(_drive_hidden_drops_deltas())


async def _drive_status_tracking():
    import novacode_cli.ui_events as ev

    app = _app()
    async with app.run_test():
        hidden = await _add_pane(app)
        await app._deliver(hidden, ev.ToolCall(name="shell", display_str="ls", icon="⚡"))
        assert hidden.status == "running"
        await app._deliver(hidden, ev.Done(had_response=True))
        assert hidden.status == "idle"


def test_delivery_tracks_pane_status():
    asyncio.run(_drive_status_tracking())


# ── switching ────────────────────────────────────────────────────────────────


async def _drive_switch_replays():
    import novacode_cli.ui_events as ev

    app = _app()
    async with app.run_test() as pilot:
        hidden = await _add_pane(app)
        for i in range(3):
            await app._deliver(
                hidden,
                ev.AssistantMessage(text=f"msg{i}", agent_name="nova", agent_color="cyan"),
            )
        assert len(hidden.buffer) == 3

        await app._switch_to(hidden)
        await pilot.pause()

        assert not hidden.buffer, "buffer should drain on switch"
        assert hidden.unread == 0
        assert len(hidden.scroll.children) >= 3


def test_switching_replays_buffered_events():
    asyncio.run(_drive_switch_replays())


async def _drive_switch_swaps_state():
    app = _app()
    async with app.run_test():
        other = await _add_pane(app)

        # Mark the root conversation, then give the incoming pane its own values.
        app._turn_active = True
        app._ctx_warned = True
        root_tracker = app.token_tracker
        other.state = {"_turn_active": False, "_ctx_warned": False, "token_tracker": None}

        await app._switch_to(other)
        assert app._turn_active is False
        assert app._ctx_warned is False
        assert app.token_tracker is None

        # Switching back restores exactly what the root pane had.
        await app._switch_to(app._root_pane)
        assert app._turn_active is True
        assert app._ctx_warned is True
        assert app.token_tracker is root_tracker


def test_switching_swaps_conversation_state():
    asyncio.run(_drive_switch_swaps_state())


async def _drive_switch_is_idempotent():
    app = _app()
    async with app.run_test():
        root = app._root_pane
        app._turn_active = True
        await app._switch_to(root)  # already active — must be a no-op
        assert app._active_pane is root
        assert app._turn_active is True


def test_switching_to_the_active_pane_is_a_noop():
    asyncio.run(_drive_switch_is_idempotent())


# ── pane state object ────────────────────────────────────────────────────────


def test_save_and_load_round_trip():
    from novacode_cli.tui.session_pane import STATEFUL_ATTRS, SessionPane

    class _App:
        pass

    src = _App()
    for name in STATEFUL_ATTRS:
        setattr(src, name, f"value-{name}")

    pane = SessionPane(sid="p", title="p", scroll=None)
    pane.save_from(src)
    assert pane.has_state

    dst = _App()
    pane.load_into(dst)
    for name in STATEFUL_ATTRS:
        assert getattr(dst, name) == f"value-{name}"


def test_missing_attributes_are_not_invented():
    """Several attrs are created lazily; absent ones must not appear as None."""
    from novacode_cli.tui.session_pane import SessionPane

    class _Empty:
        pass

    src = _Empty()
    pane = SessionPane(sid="p", title="p", scroll=None)
    pane.save_from(src)

    dst = _Empty()
    pane.load_into(dst)
    assert not hasattr(dst, "_turn_active")


def test_stateful_attrs_has_no_duplicates():
    from novacode_cli.tui.session_pane import STATEFUL_ATTRS

    assert len(STATEFUL_ATTRS) == len(set(STATEFUL_ATTRS))


# ── layout: the pane wrapper must not shrink the transcript ──────────────────


async def _drive_banner_fits_its_container(term_width: int = 120, *, _height: int = 40):
    """The Matrix-rain banner must span the transcript's real content width.

    Regression: wrapping the transcript in a ContentSwitcher for session panes
    left it unstyled, so the transcript stopped filling the screen and its
    content width shrank. The banner sizes itself from the TERMINAL width, so it
    no longer fit — every row wrapped, pushing the logo down a row and letting it
    spring back as the rain shifted ("the rain pushes the logo down, then up").

    This asserts **equality**, not ``<=``. The banner had the opposite defect
    too: a 200-column ceiling in ``MatrixRain._configure`` meant that on a
    terminal wider than ~204 columns the grid pinned itself to 200 and left a
    blank band down the right-hand side of the backdrop. A ``<=`` assertion
    passes happily through that — it only ever checked that the grid was not too
    *wide*. The rain must fill the content width exactly.

    Driven at both ends of the old clamp: 260 columns (past the 200 ceiling) and
    50 (below the 60 floor). 120 would pass under either old defect.
    """
    from rich.cells import cell_len

    app = _app()
    async with app.run_test(size=(term_width, _height)) as pilot:
        # The banner is mounted during startup; don't mount a second one (the
        # widget has a fixed id, so a duplicate raises and _show_home_banner
        # swallows it, leaving _home_banner=None).
        await pilot.pause()
        rain = app._home_banner
        assert rain is not None, "home banner should be mounted at startup"
        # Stop the 15fps repaint or pilot.pause() never settles.
        rain.pause()
        await pilot.pause()

        avail = app._transcript().content_size.width
        assert avail > 0

        assert rain._col_count == max(avail, rain._art_w), (
            f"rain grid {rain._col_count} against transcript content {avail} at "
            f"{term_width} columns — a narrower grid leaves a blank band, a wider one "
            "wraps every row"
        )
        for line in rain._build_frame().plain.split("\n"):
            assert cell_len(line) <= avail


@pytest.mark.parametrize("term_width", [260, 120, 50])
def test_home_banner_fits_the_transcript_width(term_width: int):
    asyncio.run(_drive_banner_fits_its_container(term_width))


async def _drive_transcript_fills_screen():
    """The pane wrapper must be layout-transparent."""
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        transcript = app._transcript()
        panes = app.query_one("#panes")
        # The switcher must span the full screen and pass that width through,
        # losing only the scroll gutter — NOT collapse to auto-size.
        assert panes.size.width == 120
        assert transcript.content_size.width >= 120 - 6, (
            f"transcript content {transcript.content_size.width} is too narrow; "
            "the banner is sized from the terminal width and will wrap"
        )


def test_pane_switcher_does_not_shrink_the_transcript():
    asyncio.run(_drive_transcript_fills_screen())


async def _drive_tab_bar_does_not_eat_the_panes():
    """The visible tab bar must not steal the panes' vertical space.

    Regression: ``Tabs`` wraps its content in a ``tabs-scroll`` that is
    ``height: 1fr``. With ``#session-tabs { height: auto }`` that 1fr child had
    no bound, so the tab bar expanded to ~the whole screen and crushed #panes to
    a single row. Every session pane — the root transcript included — then
    rendered in a 1-row sliver, so nothing showed once a second session existed.
    Only rendered HEIGHT catches it; display flags and child counts stay correct.
    """
    from textual.widgets import Tabs

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        pane = await _add_pane(app)
        app._refresh_tabs()  # tab bar becomes visible (2 sessions)
        await pilot.pause()

        tabs = app.query_one("#session-tabs", Tabs)
        panes = app.query_one("#panes")
        assert tabs.display is True
        # The bar is a few rows; it must not swallow the screen.
        assert tabs.size.height <= 5, f"tab bar is {tabs.size.height} rows tall"
        # The panes keep the bulk of the height so content is actually visible.
        assert panes.size.height >= 20, (
            f"#panes collapsed to {panes.size.height} rows — the tab bar ate it"
        )

        # And a switched-to child pane has real height to render into.
        await app._switch_to(pane)
        await pilot.pause()
        assert pane.scroll.size.height >= 20, (
            f"child pane is {pane.scroll.size.height} rows — nothing will render"
        )


def test_tab_bar_does_not_collapse_the_panes():
    asyncio.run(_drive_tab_bar_does_not_eat_the_panes())


async def _drive_tabs_hidden_at_startup():
    """A single-session run must look exactly as it did before panes existed."""
    from textual.widgets import Tabs

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        tabs = app.query_one("#session-tabs", Tabs)
        assert tabs.display is False, "empty tab bar must not take up a row"


def test_tab_bar_is_hidden_before_any_spawn():
    asyncio.run(_drive_tabs_hidden_at_startup())


async def _drive_exactly_one_pane_visible():
    """Only the active pane may be displayed.

    ContentSwitcher hides non-current children at COMPOSE time only; a pane
    mounted later stays visible and renders on top of the active one. The new
    (empty) pane then covered the running session's transcript, so the whole
    screen looked blank — messages were being mounted, just hidden underneath.
    """
    from textual.widgets import ContentSwitcher

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        switcher = app.query_one("#panes", ContentSwitcher)

        pane = await _add_pane(app)
        pane.scroll.display = False  # what spawn_session does
        await pilot.pause()

        def visible():
            return [c.id for c in switcher.children if c.display]

        assert visible() == ["transcript"], visible()

        await app._switch_to(pane)
        await pilot.pause()
        assert visible() == [pane.scroll.id], visible()

        await app._switch_to(app._root_pane)
        await pilot.pause()
        assert visible() == ["transcript"], visible()


def test_only_the_active_pane_is_visible():
    asyncio.run(_drive_exactly_one_pane_visible())


async def _drive_spawn_activates_the_new_session():
    """Spawning must make the new session ACTIVE, not merely create its tab.

    Previously the tab appeared but the root pane stayed active, so everything
    typed next went to the root agent and rendered in the root transcript while
    the spawned session sat idle, never receiving a prompt. It looked exactly
    like "the summoned session does nothing".
    """
    from textual.widgets import Tabs

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        stub = _StubSupervisor()
        app._session_supervisor = stub

        async def fake_spawn(**kw):
            class _C:
                status = "idle"

            return _C()

        stub.spawn = fake_spawn

        await app.spawn_session("answer", "")
        await pilot.pause()

        child = app._panes[-1]
        assert app._active_pane is child, "spawn should switch to the new session"
        assert app._active_pane.kind == "child"

        # The tab highlight must agree with the active pane, or the user is told
        # they are somewhere they are not.
        tabs = app.query_one("#session-tabs", Tabs)
        assert tabs.active == child.sid

        # And typing now reaches the child, not the root agent.
        await app._dispatch_to_child(child, "hey")
        assert stub.prompts == [(child.sid, "hey")]


def test_spawning_switches_to_the_new_session():
    asyncio.run(_drive_spawn_activates_the_new_session())


async def _drive_tab_highlight_follows_switch():
    from textual.widgets import Tabs

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        pane = await _add_pane(app)
        pane.scroll.display = False
        app._refresh_tabs()
        await pilot.pause()

        tabs = app.query_one("#session-tabs", Tabs)
        await app._switch_to(pane)
        await pilot.pause()
        assert tabs.active == pane.sid

        await app._switch_to(app._root_pane)
        await pilot.pause()
        assert tabs.active == app._root_pane.sid


def test_tab_highlight_follows_the_active_pane():
    asyncio.run(_drive_tab_highlight_follows_switch())


async def _drive_child_reply_lands_in_its_own_pane():
    """A child's reply must render in the CHILD pane, not the root's widgets.

    `_render` reuses `self._stream_msg` when it is not None. A new pane with no
    saved state inherited the root pane's live widget references on switch, so
    the child's streamed reply was appended to the ROOT's (hidden) message widget
    — the agent answered and the tab stayed black.
    """
    import novacode_cli.ui_events as ev

    from novacode_cli.tui.session_pane import fresh_state

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        # Give the root a live streaming widget, exactly as a prior turn would.
        await app._render(ev.TextDelta(text="root turn in progress"))
        await pilot.pause()
        root_widget = app._stream_msg
        assert root_widget is not None
        root_children = len(app._root_pane.scroll.children)

        pane = await _add_pane(app)
        pane.scroll.display = False
        pane.state = fresh_state(session_state=_SS(), assistant_id="nova-agent")
        await app._switch_to(pane)
        await pilot.pause()

        # Switching to a fresh pane must clear the inherited widget refs.
        assert app._stream_msg is None, "child pane inherited the root's stream widget"

        await app._render(ev.TextDelta(text="child reply"))
        await app._render(
            ev.AssistantMessage(text="child reply", agent_name="nova", agent_color="cyan")
        )
        await pilot.pause()

        # The reply is visible in the child's own scroll…
        assert len(pane.scroll.children) >= 1
        # …and the root's transcript was not touched.
        assert len(app._root_pane.scroll.children) == root_children
        assert app._stream_msg is not root_widget


def test_child_reply_renders_in_the_child_pane():
    asyncio.run(_drive_child_reply_lands_in_its_own_pane())


def test_fresh_state_clears_every_widget_reference():
    """Anything holding a widget must start empty in a new pane."""
    from novacode_cli.tui.session_pane import STATEFUL_ATTRS, fresh_state

    state = fresh_state()
    for name in (
        "_stream_msg",
        "_reason_msg",
        "_tool_group",
        "_tool_group_body",
        "_last_tool",
        "_home_banner",
    ):
        assert state[name] is None, f"{name} must not carry over"
    assert state["_tool_components"] == {}
    # Todos are docked chrome now: the DATA is per-pane, not a widget ref.
    assert state["_todos"] == []
    assert state["_todos_agent"] is None
    assert state["_subagent_widgets"] == {}
    assert state["_seen"] == set()
    assert state["_turn_active"] is False

    # Every attribute the swap restores must have a defined starting value,
    # or the pane silently inherits the previous one.
    missing = [n for n in STATEFUL_ATTRS if n not in state]
    assert not missing, f"fresh_state is missing: {missing}"


def test_question_modal_answer_is_read_as_a_dict():
    """QuestionResponse is a TypedDict, so the answer is a KEY, not an attribute.

    Reading it with getattr() returned None and the fallback stringified the
    whole dict, so a new session was literally named "{'answer'".
    """
    from novacode_cli.ui.question_prompt import QuestionResponse

    response: QuestionResponse = {"answer": "parser: add retry", "selected_index": None}
    assert not hasattr(response, "answer")  # the trap
    assert response.get("answer") == "parser: add retry"

    # The parse the spawn flow performs on it.
    raw = str(response.get("answer") or "")
    name, _, task = raw.partition(":")
    assert name.strip() == "parser"
    assert task.strip() == "add retry"


# ── child wiring (stub supervisor: no subprocess) ────────────────────────────


class _StubSupervisor:
    """Records what the app asks of the supervisor."""

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.replies: list[tuple[str, str, object]] = []
        self.closed: list[str] = []
        self.capacity = False

    def at_capacity(self):
        return self.capacity

    async def send_prompt(self, sid, text):
        self.prompts.append((sid, text))
        return "p1"

    async def cancel(self, sid):
        self.cancelled.append(sid)

    async def reply_interrupt(self, sid, iid, result):
        self.replies.append((sid, iid, result))

    async def close(self, sid, **kw):
        self.closed.append(sid)
        return None


async def _drive_child_input_forwarded():
    app = _app()
    async with app.run_test():
        stub = _StubSupervisor()
        app._session_supervisor = stub
        pane = await _add_pane(app)
        await app._switch_to(pane)

        await app._dispatch_to_child(pane, "refactor the parser")
        assert stub.prompts == [(pane.sid, "refactor the parser")]
        assert pane.status == "running"


def test_input_in_a_child_tab_goes_to_that_process():
    asyncio.run(_drive_child_input_forwarded())


async def _drive_child_rejects_other_slash():
    app = _app()
    async with app.run_test():
        stub = _StubSupervisor()
        app._session_supervisor = stub
        pane = await _add_pane(app)
        await app._switch_to(pane)

        await app._dispatch_to_child(pane, "/model")
        assert stub.prompts == []  # not forwarded as a prompt


def test_unsupported_slash_in_child_tab_is_not_forwarded():
    asyncio.run(_drive_child_rejects_other_slash())


async def _drive_cancel_is_per_session():
    app = _app()
    async with app.run_test():
        stub = _StubSupervisor()
        app._session_supervisor = stub
        pane = await _add_pane(app)
        await app._switch_to(pane)

        killed = []
        app.workers.cancel_all = lambda: killed.append("all")  # type: ignore[assignment]
        app.action_cancel_turn()
        await asyncio.sleep(0.05)

        assert stub.cancelled == [pane.sid]
        # cancel_all() would also kill the supervisor's child readers and the
        # remote consumer, detaching every other session.
        assert killed == []


def test_escape_cancels_only_the_active_session():
    asyncio.run(_drive_cancel_is_per_session())


async def _drive_hidden_interrupt_does_not_steal_screen():
    app = _app()
    async with app.run_test() as pilot:
        app._session_supervisor = _StubSupervisor()
        hidden = await _add_pane(app)  # not switched to

        await app._on_child_message(
            hidden.sid, {"t": "interrupt", "id": "i1", "kind": "tool", "payload": {}}
        )
        await pilot.pause()

        assert hidden.status == "needs-approval"
        assert hidden.pending_interrupt is not None  # held for later
        # No modal was pushed over the visible session.
        assert app.screen is app.screen_stack[0]


def test_background_approval_flags_the_tab_without_stealing_focus():
    asyncio.run(_drive_hidden_interrupt_does_not_steal_screen())


async def _drive_child_events_reach_the_pane():
    app = _app()
    async with app.run_test():
        app._session_supervisor = _StubSupervisor()
        hidden = await _add_pane(app)

        await app._on_child_message(
            hidden.sid,
            {
                "t": "ev",
                "c": "AssistantMessage",
                "d": {
                    "text": "from the child",
                    "agent_name": "nova",
                    "agent_color": "cyan",
                    "is_subagent": False,
                },
            },
        )
        assert len(hidden.buffer) == 1
        assert hidden.buffer[0].text == "from the child"

        await app._on_child_message(hidden.sid, {"t": "turn_done", "id": "p1", "ok": True})
        assert hidden.status == "idle"


def test_child_events_are_decoded_into_the_pane():
    asyncio.run(_drive_child_events_reach_the_pane())


async def _drive_exit_marks_crashed():
    app = _app()
    async with app.run_test():
        app._session_supervisor = _StubSupervisor()
        pane = await _add_pane(app)
        await app._on_child_message(pane.sid, {"t": "exited", "code": 3, "crashed": True})
        assert pane.status == "crashed"


def test_child_crash_shows_on_the_tab():
    asyncio.run(_drive_exit_marks_crashed())


# ── tab bar ──────────────────────────────────────────────────────────────────


async def _drive_tabs_visibility():
    from textual.widgets import Tabs

    app = _app()
    async with app.run_test() as pilot:
        tabs = app.query_one("#session-tabs", Tabs)
        app._refresh_tabs()
        await pilot.pause()
        assert tabs.display is False, "single session should look unchanged"

        await _add_pane(app)
        app._refresh_tabs()
        await pilot.pause()
        assert tabs.display is True


def test_tab_bar_appears_only_with_multiple_sessions():
    asyncio.run(_drive_tabs_visibility())


async def _drive_goto_session():
    app = _app()
    async with app.run_test() as pilot:
        other = await _add_pane(app)
        app.action_goto_session(2)
        await pilot.pause()
        await asyncio.sleep(0.05)
        assert app._active_pane is other

        app.action_goto_session(1)
        await pilot.pause()
        await asyncio.sleep(0.05)
        assert app._active_pane is app._root_pane

        app.action_goto_session(99)  # out of range -> no change
        await pilot.pause()
        assert app._active_pane is app._root_pane


def test_alt_number_jumps_between_sessions():
    asyncio.run(_drive_goto_session())


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v", "--assert=plain"]))
