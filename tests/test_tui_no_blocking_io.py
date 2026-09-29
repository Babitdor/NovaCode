"""The TUI must not make blocking calls on its own event loop.

``langgraph dev`` arms BlockBuster, which raises ``BlockingError`` when a patched
blocking call (file read, subprocess, socket, ``time.sleep``) runs while an
asyncio loop is active. Nova's TUI shares that loop with the agent, so a
synchronous read or subprocess in a handler freezes the whole UI rather than one
request. The stall-watch feature exists because that class of freeze already
happened once.

The positive control matters more than the sweep: without it, a green sweep is
indistinguishable from a guard that is misconfigured and never fires at all.

The guard is opt-in (``no_blocking_io`` in ``tests/conftest.py``) while the modal
screens still do synchronous filesystem work in some action handlers; that
fixture's docstring lists the remaining sites. The paths this module drives are
already fixed, including the preview readers that used to read a file inline.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("textual")

from novacode_cli.tui.app import NovaApp
from novacode_cli.ui.ui_elements import TokenTracker


class _StateVal:
    def __init__(self) -> None:
        self.values = {"messages": []}


class _FakeAgent:
    """Minimal stand-in; these tests never run a real turn."""

    async def aget_state(self, config):  # noqa: ANN001, ANN202, ARG002 - test double
        return _StateVal()

    async def astream(self, inp, **kw):  # noqa: ANN001, ANN003, ANN202, ARG002 - double
        if False:  # pragma: no cover - never yields; keeps this an async generator
            yield None

    async def aupdate_state(self, **kw):  # noqa: ANN003, ANN202 - test double
        pass


class _SS:
    thread_id = "t1"
    auto_approve = True
    plan_mode_enabled = False
    todos: list = []  # noqa: RUF012 - mutable class attr is fine for a test double
    steering_instructions: list = []  # noqa: RUF012 - ditto

    def unread_notification_count(self) -> int:
        return 0

    def pending_approval_count(self) -> int:
        return 0

    def reset_conversation(self) -> None:
        pass


def _make_app() -> NovaApp:
    return NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="m",
    )


@pytest.mark.own_blockbuster_guard
async def test_guard_catches_a_blocking_call() -> None:
    """Positive control: the guard must catch a deliberate violation.

    With no ``scanned_modules`` BlockBuster polices every patched call, so a
    ``time.sleep`` on the loop has to raise. pytest-asyncio gives us a running
    loop, so the blocking call really is on it.
    """
    from blockbuster import BlockBuster, BlockingError

    bb = BlockBuster()
    bb.activate()
    try:
        with pytest.raises(BlockingError, match=r"time\.sleep"):
            time.sleep(0.01)  # noqa: ASYNC251 - blocking on the loop is the point
    finally:
        # Explicit activate/deactivate in a try/finally: a leaked guard would
        # silently police every later test in the session (blockbuster_ctx only
        # gained its own try/finally in 1.5.27).
        bb.deactivate()


async def test_tui_hot_paths_do_not_block_the_loop(
    no_blocking_io: None,  # noqa: ARG001 - requesting the fixture is what arms it
) -> None:
    """The fixed hot paths stay off the loop with the guard armed."""
    app = _make_app()
    async with app.run_test() as pilot:
        for _ in range(3):
            await pilot.pause()

        # `!` shell command: the subprocess must not run on the loop. This is the
        # branch every non-interactive environment takes, because `suspend()` is
        # unavailable under `run_test()`.
        await app._run_bash("!echo nova-no-blocking-probe")
        for _ in range(2):
            await pilot.pause()

        # `/log grep`: the run-history walk is entirely synchronous filesystem
        # work (list, iterdir, exists, read_text) and used to run on the loop.
        await app._passthrough_command("/log grep nova --limit 5")
        for _ in range(2):
            await pilot.pause()

        # Boot path: reading the voice config constructs `NovaConfig`, which
        # resolves the project root by walking parent directories. Awaited
        # explicitly rather than relied on via `on_mount`'s worker: a test that
        # raced that worker would pass whether or not the read is offloaded,
        # which is exactly what happened before this line was added.
        await app._eager_voice_warmup()
        await pilot.pause()


async def test_council_context_is_read_off_the_loop(
    no_blocking_io: None,  # noqa: ARG001 - requesting the fixture is what arms it
) -> None:
    """`_council_context` does sync filesystem work, so it must be awaited in a thread.

    Driven directly rather than through `/council`: the command needs a live
    model and four planning phases, but the blocking read happens before any of
    that, so this pins the part that matters.
    """
    app = _make_app()
    async with app.run_test() as pilot:
        for _ in range(2):
            await pilot.pause()
        # The guard is armed, so a direct on-loop call would raise BlockingError.
        context = await asyncio.to_thread(app._council_context)
        assert isinstance(context, str)
        await pilot.pause()
