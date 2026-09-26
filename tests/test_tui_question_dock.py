"""The docked ``ask_user_question`` answer list.

Boots ``NovaApp`` and drives the real ``QuestionDock`` through Textual's test
pilot, so the assertions cover the shipped CSS (the selected-row highlight and
the accent label) rather than just widget state. Skipped if ``textual`` is
missing.

Runnable directly (``python tests/test_tui_question_dock.py``) or via pytest.
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

FLAT = [
    "planner — Has a 12-step agenda and a Gantt chart for the snacks.",
    "refactor-cleaner — Rearranges the furniture until the room is minimal.",
]

PAYLOAD = {
    "question": "Your 97 subagents throw a party. Who's running it?",
    "context": "One host, or nobody makes the toast.",
    "choices": [
        {
            "label": "planner",
            "description": "Has a 12-step agenda and a Gantt chart for the snacks.",
            "value": "planner",
            "display": FLAT[0],
        },
        {
            "label": "refactor-cleaner",
            "description": "Rearranges the furniture until the room is minimal.",
            "value": "refactor-cleaner",
            "display": FLAT[1],
        },
    ],
    "options": FLAT,
    "question_type": "structured",
}


class _Chunk:
    def __init__(self, mid, blocks):
        self.id = mid
        self._blocks = blocks
        self.usage_metadata = {"input_tokens": 20, "output_tokens": 8}

    @property
    def content_blocks(self):
        return self._blocks


class _StateVal:
    def __init__(self, msgs):
        self.values = {"messages": msgs}


class _FakeAgent:
    async def aget_state(self, config):
        return _StateVal([])

    async def astream(self, inp, **kw):
        yield ((), "messages", (_Chunk("m1", [{"type": "text", "text": "Hi from Nova"}]), {}))
        return
        yield  # pragma: no cover

    async def aupdate_state(self, **kw):
        pass


class _SS:
    thread_id = "t1"
    auto_approve = True
    plan_mode_enabled = False
    todos: list = []
    steering_instructions: list = []


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


async def _wait_until(pilot, predicate, *, timeout: float = 5.0) -> bool:
    """Pump the event loop until *predicate* is true (or time runs out)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 — widget not mounted yet
            pass
        await pilot.pause()
        await asyncio.sleep(0.02)
    return False


def _dock(app):
    return app.query_one("#question-dock")


def _rows(app):
    return list(_dock(app).query(".question-option"))


def _label_text(row):
    return str(row.query_one(".q-label").render())


def _desc_text(row):
    return str(row.query_one(".q-desc").render())


async def _open(app, pilot, payload=None, *, rows: int = 3):
    """Open the dock and wait until it is visible and populated."""
    task = asyncio.create_task(app._show_question_dock(payload if payload is not None else PAYLOAD))
    assert await _wait_until(pilot, lambda: _dock(app).has_class("active")), "dock never activated"
    assert await _wait_until(pilot, lambda: len(_rows(app)) == rows), "rows never mounted"
    return task


# ── layout ────────────────────────────────────────────────────────────────────


async def test_renders_one_row_per_option_plus_custom():
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        rows = _rows(app)
        assert len(rows) == 3, "two options plus the always-present custom row"
        assert _label_text(rows[0]) == "1. planner"
        assert _label_text(rows[1]) == "2. refactor-cleaner"
        assert _label_text(rows[2]).startswith("3. ")
        assert "Type your own answer" in _label_text(rows[2])

        assert str(app.query_one("#question-text").render()) == PAYLOAD["question"]
        assert str(app.query_one("#question-context").render()) == PAYLOAD["context"]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_description_is_its_own_indented_line():
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        row = _rows(app)[0]
        # A separate widget per line, not the "label — description" join, so it
        # can be dimmed and indented independently.
        assert _desc_text(row) == "Has a 12-step agenda and a Gantt chart for the snacks."
        assert row.query_one(".q-desc").styles.padding.left == 4
        assert "—" not in _label_text(row)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_hints_row_renders():
    """The legend is Textual markup with a $var, so a bad tag would render raw."""
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        hints = str(app.query_one("#question-hints").render())
        assert "select" in hints and "confirm" in hints and "dismiss" in hints
        assert "[color=" not in hints, "markup leaked instead of resolving"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ── selection ─────────────────────────────────────────────────────────────────


async def test_first_row_selected_initially():
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        assert _rows(app)[0].has_class("selected")
        assert not _rows(app)[1].has_class("selected")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_down_and_up_move_the_selection():
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        await pilot.press("down")
        await pilot.pause()
        assert _rows(app)[1].has_class("selected")
        assert not _rows(app)[0].has_class("selected")
        await pilot.press("up")
        await pilot.pause()
        assert _rows(app)[0].has_class("selected")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_selection_wraps_around():
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        await pilot.press("up")
        await pilot.pause()
        assert _rows(app)[2].has_class("selected"), "up from the first row wraps to the last"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_selected_row_is_visually_distinct():
    """The highlight is the whole affordance, so prove it reaches the styles."""
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        await pilot.press("down")
        await pilot.pause()
        rows = _rows(app)
        unselected, selected = rows[0], rows[1]

        # A dark band behind the selected row.
        assert selected.styles.background != unselected.styles.background
        # The selected label takes the accent colour, the other stays plain.
        assert (
            selected.query_one(".q-label").styles.color
            != unselected.query_one(".q-label").styles.color
        )
        assert "bold" in str(selected.query_one(".q-label").styles.text_style)
        # Descriptions stay muted either way, so the colour is on the label only.
        assert (
            selected.query_one(".q-desc").styles.color
            == unselected.query_one(".q-desc").styles.color
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ── answering ────────────────────────────────────────────────────────────────


async def test_enter_on_an_option_returns_it():
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("enter")
        result = await asyncio.wait_for(task, timeout=5)
        # The answer is the flat display string, so the tool's value_map resolves
        # a TUI pick exactly as it resolves a remote or console one.
        assert result["response"]["answer"] == FLAT[1]
        assert result["response"]["selected_index"] == 1


async def test_digit_key_picks_directly():
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        await pilot.press("2")
        result = await asyncio.wait_for(task, timeout=5)
        assert result["response"]["answer"] == FLAT[1]
        assert result["response"]["selected_index"] == 1


async def test_custom_row_swaps_the_list_for_the_input():
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        await pilot.press("down")
        await pilot.press("down")  # onto the custom row
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#question-input").has_class("active")
        assert app.query_one("#question-options").has_class("hidden")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_typed_answer_is_returned():
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        # Walk to the custom row and open the field, then type into it.
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        for key in ("u", "s", "e"):
            await pilot.press(key)
        await pilot.press("enter")
        result = await asyncio.wait_for(task, timeout=5)
        assert result["response"]["answer"] == "use"
        assert result["response"]["selected_index"] is None


async def test_letter_key_on_the_list_does_not_hijack():
    """Free text is entered only after choosing the custom row, so a stray
    keystroke on a real option cannot throw the list away."""
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        await pilot.press("x")
        await pilot.pause()
        assert not app.query_one("#question-input").has_class("active")
        assert len(_rows(app)) == 3
        # And the list still answers normally afterwards.
        await pilot.press("enter")
        result = await asyncio.wait_for(task, timeout=5)
        assert result["response"]["answer"] == FLAT[0]


async def test_escape_from_the_input_returns_to_the_list():
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        await pilot.press("down")
        await pilot.press("down")  # onto the custom row
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#question-input").has_class("active")
        await pilot.press("escape")
        await pilot.pause()
        assert not app.query_one("#question-input").has_class("active")
        assert not app.query_one("#question-options").has_class("hidden")
        # The question is still open — escape from the field backs out, it does
        # not dismiss the whole question.
        assert _dock(app).has_class("active")
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ── dismissal ────────────────────────────────────────────────────────────────


async def test_escape_dismisses_without_cancelling_the_turn():
    """The app binds escape to cancel_turn; the dock must swallow it first."""
    app = _app()
    cancelled: list[bool] = []
    app.action_cancel_turn = lambda: cancelled.append(True)
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        await pilot.press("escape")
        result = await asyncio.wait_for(task, timeout=5)
        assert result["response"]["answer"] == ""
        assert result["response"]["selected_index"] is None
        assert not cancelled, "escape reached cancel_turn instead of dismissing"


async def test_dock_is_hidden_again_after_answering():
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(app, pilot)
        await pilot.press("enter")
        await asyncio.wait_for(task, timeout=5)
        await pilot.pause()
        assert not _dock(app).has_class("active")
        # Focus must go back to the prompt, or the keyboard dies after a question.
        assert app.focused is not None
        assert app.focused.id == "prompt"


# ── payload compatibility ────────────────────────────────────────────────────


async def test_flat_options_payload_still_works():
    """Remote bridges, the cowork server and older callers send only `options`."""
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(
            app,
            pilot,
            {"question": "Pick one?", "options": ["alpha", "beta"], "question_type": "structured"},
        )
        rows = _rows(app)
        assert len(rows) == 3
        assert _label_text(rows[0]) == "1. alpha"
        assert not rows[0].query(".q-desc"), "a flat option has no description line"
        await pilot.press("enter")
        result = await asyncio.wait_for(task, timeout=5)
        assert result["response"]["answer"] == "alpha"


async def test_brackets_in_options_do_not_crash_the_render():
    """The hazard QuestionModal guarded against; the rows here are plain Statics."""
    app = _app()
    async with app.run_test() as pilot:
        task = await _open(
            app,
            pilot,
            {
                "question": "pick [a] or [b]?",
                "choices": [
                    {"label": "use [x]", "description": "keeps [brackets]", "display": "use [x]"},
                ],
                "options": ["use [x]"],
            },
            rows=2,
        )
        rows = _rows(app)
        assert _label_text(rows[0]) == "1. use [x]"
        assert _desc_text(rows[0]) == "keeps [brackets]"
        await pilot.press("enter")
        result = await asyncio.wait_for(task, timeout=5)
        assert result["response"]["answer"] == "use [x]"


async def test_optionless_payload_still_opens():
    app = _app()
    async with app.run_test() as pilot:
        task = asyncio.create_task(app._show_question_dock({"question": "Anything?"}))
        assert await _wait_until(pilot, lambda: _dock(app).has_class("active"))
        await pilot.pause()
        # Only the custom row exists, so enter opens the input rather than
        # indexing off the end of an empty list.
        assert len(_rows(app)) == 1
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#question-input").has_class("active")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
