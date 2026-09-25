"""A resumed session keeps its own model and provider.

Three separate gaps let this break:

1. ``NovaApp`` derived the provider from the *global* config even when the
   session had a different model, so the next save re-recorded the wrong one.
2. The bare ``--continue`` flag (``True``, meaning "the latest session") never
   resolved a session id, so it never restored a model at all.
3. ``_model_provider`` was app-global rather than per-pane, so with several
   sessions open the provider of one leaked into another's save.
"""

from __future__ import annotations

import pytest

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False


class _SS:
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


def _app(**kw):
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    return NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name=kw.pop("model_name", "m"),
        **kw,
    )


# ── gap 1: the restored provider must win over global config ─────────────────


@pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")
def test_restored_provider_is_used_not_the_global_one(monkeypatch):
    """A resumed session must record ITS provider, not the global default."""
    import novacode_cli.utils.model_info as mi

    # The global config says "anthropic"; the resumed session used "ollama".
    monkeypatch.setattr(mi, "get_current_provider", lambda: "anthropic")

    app = _app(model_provider="ollama")
    assert app._model_provider == "ollama", (
        "the global provider overrode the session's own recorded provider"
    )


@pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")
def test_no_recorded_provider_falls_back_to_global_config(monkeypatch):
    """A fresh (non-resumed) session still derives the provider as before."""
    import novacode_cli.utils.model_info as mi

    monkeypatch.setattr(mi, "get_current_provider", lambda: "anthropic")
    app = _app()
    assert app._model_provider == "anthropic"


# ── gap 2: the bare --continue flag must resolve the latest session ──────────


class _FakeMeta:
    def __init__(self, provider, model):
        self.model_provider = provider
        self.model_name = model
        self.session_id = "latest-1"


class _FakeSessionManager:
    def __init__(self, meta):
        self._meta = meta
        self.loaded: list[str] = []

    def get_latest_session(self, project_root=None):
        return self._meta

    def load_session_meta(self, session_id):
        self.loaded.append(session_id)
        return self._meta


def test_bare_continue_resolves_the_latest_session_for_the_model():
    """`continue_session=True` must look up the latest session, not skip it.

    Guarding on ``isinstance(continue_session, str)`` alone skipped the bare
    ``--continue`` flag, so `nova --continue` never restored its model.
    Exercises the real helper `main()` uses.
    """
    from novacode_cli.session.session_restore import resolve_resume_session_id

    mgr = _FakeSessionManager(_FakeMeta("ollama", "glm-5:cloud"))

    # The bare flag resolves to the latest session...
    resolved = resolve_resume_session_id(continue_session=True, session_manager=mgr)
    assert resolved == "latest-1", "bare --continue did not resolve a session id"

    # ...and the recorded model is then readable for that id.
    meta = mgr.load_session_meta(resolved)
    assert (meta.model_provider, meta.model_name) == ("ollama", "glm-5:cloud")


def test_concrete_session_id_wins_over_the_latest():
    from novacode_cli.session.session_restore import resolve_resume_session_id

    mgr = _FakeSessionManager(_FakeMeta("ollama", "glm-5:cloud"))
    assert resolve_resume_session_id(continue_session="picked-123", session_manager=mgr) == "picked-123"


def test_no_continue_flag_resolves_to_nothing():
    from novacode_cli.session.session_restore import resolve_resume_session_id

    mgr = _FakeSessionManager(_FakeMeta("ollama", "glm-5:cloud"))
    assert resolve_resume_session_id(continue_session=False, session_manager=mgr) is None


def test_bare_continue_with_no_sessions_resolves_to_nothing():
    """`nova --continue` in a fresh project must not crash."""
    from novacode_cli.session.session_restore import resolve_resume_session_id

    class _Empty:
        def get_latest_session(self, project_root=None):
            return None

    assert resolve_resume_session_id(continue_session=True, session_manager=_Empty()) is None


# ── gap 3: per-pane provider isolation ───────────────────────────────────────


@pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")
def test_model_provider_is_per_pane_state():
    """Each pane must own its provider, or two sessions cross-contaminate."""
    from novacode_cli.tui.session_pane import STATEFUL_ATTRS, fresh_state

    assert "_model_provider" in STATEFUL_ATTRS, (
        "_model_provider is app-global, so one session's provider leaks into another"
    )
    assert fresh_state(model_provider="ollama")["_model_provider"] == "ollama"
    assert fresh_state()["_model_provider"] is None


@pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")
def test_two_panes_keep_separate_providers():
    """The reported case: two sessions, two providers, both must survive."""
    from novacode_cli.tui.session_pane import SessionPane

    app = _app(model_provider="opencode")
    app.model_name = "deepseek-v4.1-flash"
    pane_a = SessionPane(sid="a", title="a", scroll=None)
    pane_a.save_from(app)

    # A second conversation switches to a different provider.
    app._model_provider = "nvidia"
    app.model_name = "deepseek-v4.1-flash"
    pane_b = SessionPane(sid="b", title="b", scroll=None)
    pane_b.save_from(app)

    # Switching back must restore the first conversation's provider.
    pane_a.load_into(app)
    assert app._model_provider == "opencode"
    pane_b.load_into(app)
    assert app._model_provider == "nvidia"


# ── the wiring from startup to the TUI ──────────────────────────────────────
#
# Every link matters: a gap anywhere means the restored provider never reaches
# the object that records it on the next save.


def test_provider_is_threaded_from_main_to_the_tui():
    """main() -> _run_agent_session -> run_tui -> NovaApp, all accepting it."""
    import inspect

    import novacode_cli.main as M
    import novacode_cli.tui.app as A

    assert "model_provider" in inspect.signature(M._run_agent_session).parameters
    assert "model_provider" in inspect.signature(A.run_tui).parameters
    assert "model_provider" in inspect.signature(A.NovaApp.__init__).parameters

    # And it is actually forwarded, not just accepted.
    launch_src = inspect.getsource(M._run_agent_session)
    assert "model_provider=model_provider" in launch_src, (
        "_run_agent_session accepts model_provider but never passes it to run_tui"
    )
    main_src = inspect.getsource(M.main)
    assert main_src.count("model_provider=session_provider") == 2, (
        "main() must pass the resolved provider to both the local and sandbox launches"
    )
