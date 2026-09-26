"""`/resume` restores the session's model, not the live one.

`/resume` is an in-app command with its own implementation, separate from the
`--continue` startup path in ``main.py`` (which was already covered by
``test_session_model_restore.py``). It restored the session's id, history,
todos and sandbox but never its model, so the chat kept running on whatever
model the *previous* conversation used. Two consequences, both user-visible:

* The picker header shows ``s.model_name``, so the label contradicted the
  model actually answering.
* ``_save_session`` records ``self.model_name`` / ``self._model_provider``, so
  the next autosave overwrote the resumed session's recorded model. The
  original model was then unrecoverable from that session.

These tests pin the restore and, more importantly, the three fallbacks: a
legacy session must fall back *silently*, an unavailable model must fall back
*with a warning*, and a failing switch must never abort the resume.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False

pytestmark = pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")

RECORDED_PROVIDER = "openrouter"
RECORDED_MODEL = "stealth/space-bunny-alpha"
LIVE_MODEL = "deepseek-v4.1-flash"
LIVE_PROVIDER = "opencode"


class _FakeAgent:
    """Minimal agent: the resume path only calls aupdate_state on it."""

    async def aupdate_state(self, config: Any, values: Any) -> None:
        return None

    async def aget_state(self, config: Any) -> Any:
        class _S:
            values = {"messages": []}

        return _S()


class _SS:
    """SessionState stand-in recording what the resume asked of it."""

    session_id = "live-session"
    thread_id = "t-live"
    is_continued = False
    todos: list = []
    steering_instructions: list = []
    worker = False
    headless = False

    def __init__(self) -> None:
        self.switched_to: list = []
        self.switch_error: Exception | None = None

    def reset_conversation(self) -> None:
        self.thread_id = "t-resumed"

    async def switch_model(self, new_model: Any) -> tuple[Any, Any]:
        if self.switch_error is not None:
            raise self.switch_error
        self.switched_to.append(new_model)
        return _FakeAgent(), "backend-for-new-model"


def _build_app(ss: _SS, **kw: Any) -> Any:
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    class _SM:
        """session_manager stand-in; restore_session itself is stubbed."""

        async def list_sessions(self, limit: int = 0) -> list:
            return []

        def load_recent_messages(self, sid: str) -> list:
            return []

    return NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=ss,
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name=LIVE_MODEL,
        model_provider=LIVE_PROVIDER,
        # Required: _run_resume bails with "Session resume is unavailable"
        # when this is None, before the model decision is ever reached.
        session_manager=_SM(),
        **kw,
    )


def _fake_model(name: str) -> Any:
    class _M:
        model_name = name

    return _M()


class _Restored:
    """Stubs create_model_for_session, returning a fixed (model, warning)."""

    def __init__(self, model: Any, warning: str | None) -> None:
        self._model = model
        self._warning = warning
        self.calls: list[tuple] = []

    def __call__(self, provider: Any, model_name: Any) -> tuple[Any, str | None]:
        self.calls.append((provider, model_name))
        return self._model, self._warning


def _patch_restore(
    monkeypatch: pytest.MonkeyPatch, session_meta: Any, result: tuple[Any, str | None]
) -> _Restored:
    """Patch the three collaborators _run_resume touches off-session."""
    import novacode_cli.tui.app as app_mod

    restored = _Restored(result[0], result[1])
    monkeypatch.setattr(
        "novacode_cli.config.model_create.create_model_for_session", restored, raising=True
    )

    class _Data:
        def __init__(self) -> None:
            self.meta = session_meta
            self.todos: list = []
            self.workspace_state = None
            self.messages: list = []

    monkeypatch.setattr(app_mod, "_truncate", lambda s, n: s or "-", raising=False)
    monkeypatch.setattr(
        "novacode_cli.session.session_restore.restore_session",
        lambda sm, tid, ws: (_Data(), ["old warning"]),
    )
    # The prompt builder is exercised by its own tests; stub the heavy I/O it
    # needs so this stays a unit test of the model decision.
    monkeypatch.setattr(
        "novacode_cli.session.session_prompt_builder.build_continuation_prompt",
        lambda **kw: [],
    )
    monkeypatch.setattr("novacode_cli.session.session_prompt_builder.load_NOVA_md", lambda ws: "")
    monkeypatch.setattr("novacode_cli.tracking.workspace_anchoring.scan_workspace", lambda ws: {})
    monkeypatch.setattr("novacode_cli.config.config.get_default_coding_instructions", lambda: "sys")
    return restored


class _Meta:
    def __init__(self, provider: Any, model: Any) -> None:
        self.session_id = "resumed-session"
        self.model_provider = provider
        self.model_name = model


async def _drive(
    monkeypatch: pytest.MonkeyPatch,
    ss: _SS,
    meta: _Meta,
    result: tuple[Any, str | None],
) -> tuple[Any, _Restored, list]:
    restored = _patch_restore(monkeypatch, meta, result)
    app = _build_app(ss)
    logged: list = []
    # Capture what the user is actually told. The resume reports failures by
    # logging them, so this is the only honest way to assert on surfacing.
    monkeypatch.setattr(app, "_log", lambda t, *a, **kw: logged.append(str(t)))
    async with app.run_test() as pilot:
        # Pass the id explicitly. The bare form would open the picker, which is
        # tested separately in test_tui_app.py; this test is about the model
        # decision that follows the resume.
        await app._run_resume("/resume resumed-session")
        await pilot.pause()
    return app, restored, logged


# ── the restore itself ────────────────────────────────────────────────────────


def test_resume_restores_the_recorded_model_and_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The core fix: a resumed session adopts ITS model, not the live one."""
    ss = _SS()
    app, restored, _ = asyncio.run(
        _drive(
            monkeypatch,
            ss,
            _Meta(RECORDED_PROVIDER, RECORDED_MODEL),
            (_fake_model(RECORDED_MODEL), None),
        )
    )

    # The recorded pair was what we asked the factory for...
    assert restored.calls == [(RECORDED_PROVIDER, RECORDED_MODEL)]
    # ...the agent was actually rebuilt on it, not merely relabelled...
    assert len(ss.switched_to) == 1
    # ...and both the displayed name and the saved provider moved with it.
    assert app.model_name == RECORDED_MODEL
    assert app._model_provider == RECORDED_PROVIDER
    # Sanity: this is a real change away from the pre-resume live model.
    assert LIVE_MODEL != RECORDED_MODEL


# ── the three fallbacks, all non-fatal ────────────────────────────────────────


def test_legacy_session_falls_back_silently_and_does_not_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No recorded provider: keep the live model, warn nothing, skip the rebuild."""
    ss = _SS()
    app, restored, _ = asyncio.run(
        _drive(monkeypatch, ss, _Meta(None, None), (_fake_model(RECORDED_MODEL), None))
    )

    # A legacy session must not pay for an agent rebuild it cannot use.
    assert ss.switched_to == []
    assert app.model_name == LIVE_MODEL
    assert app._model_provider == LIVE_PROVIDER


def test_unavailable_model_warns_and_keeps_the_live_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded model that cannot be rebuilt: warn, keep running, still resume."""
    warning = "This session used openrouter:stealth/x, but it could not be restored."
    ss = _SS()
    app, _, logged = asyncio.run(
        _drive(monkeypatch, ss, _Meta(RECORDED_PROVIDER, RECORDED_MODEL), (None, warning))
    )

    assert ss.switched_to == []
    assert app.model_name == LIVE_MODEL
    # The session still resumed: the identity swap happened regardless.
    assert app.session_state.session_id == "resumed-session"
    # And the reason is surfaced to the user, not swallowed. Resume renders the
    # collected warnings through _log, so that is where the text must appear.
    assert any("could not be restored" in line for line in logged)


def test_switch_failure_does_not_abort_the_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising switch_model must not cost the user their conversation."""
    ss = _SS()
    ss.switch_error = RuntimeError("agent rebuild exploded")
    app, _, logged = asyncio.run(
        _drive(
            monkeypatch,
            ss,
            _Meta(RECORDED_PROVIDER, RECORDED_MODEL),
            (_fake_model(RECORDED_MODEL), None),
        )
    )

    # The resume completed: identity, history seeding, and thread all advanced.
    assert app.session_state.session_id == "resumed-session"
    assert app.session_state.thread_id == "t-resumed"
    # The failed model left the live one in place rather than half-adopting.
    assert app.model_name == LIVE_MODEL
    # And the user is told why, instead of silently getting the wrong model.
    assert any("Model restore failed" in line for line in logged)
