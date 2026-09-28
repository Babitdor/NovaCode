"""Shared test fixtures."""

import os

import pytest

#: Env vars that carry credentials. Anything that saves a credential writes
#: these directly into ``os.environ`` (see `config/credentials.py`), and callers
#: build models by reading the environment, so a leaked value changes which
#: providers and tools a LATER test believes are configured.
_CREDENTIAL_ENV_VARS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENCODE_API_KEY",
    "NVIDIA_API_KEY",
    "TAVILY_API_KEY",
    "LANGSMITH_API_KEY",
    "OPENAI_BASE_URL",
    # Voice services. Kept in step with `SERVICE_API_KEY_ENV`: a value exported
    # here makes a later test's provider read as "env set" instead of "stored".
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
)


@pytest.fixture(autouse=True)
def _restore_credential_env_vars():
    """Undo credential exports a test made directly through ``os.environ``.

    ``monkeypatch`` cannot see those: it only reverts what it set itself, so a
    test that saved a key left it exported for the whole session. That is how a
    leaked ``TAVILY_API_KEY`` once flipped the tool roster another test asserts
    on. Restores exact prior presence (set, unset, or empty) for the credential
    vars only, leaving the rest of the environment to monkeypatch.
    """
    saved = {name: os.environ.get(name) for name in _CREDENTIAL_ENV_VARS}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(scope="session", autouse=True)
def _guard_the_real_user_config():
    """Fail the run if anything writes the developer's real Nova.config.json.

    Stubbing `NovaConfig` only works when the patch lands on the name the module
    under test actually resolves. `voice_handler` imports it *inside* the
    function, so patching `voice_handler.NovaConfig` misses and the write goes to
    ``~/.nova/Nova.config.json`` — which is how a credential was once cleared
    from the author's real config by a test run. This asserts the file is
    untouched, so that mistake surfaces as a failure instead of as lost data.
    """
    from pathlib import Path

    path = Path.home() / ".nova" / "Nova.config.json"
    before = path.read_bytes() if path.exists() else None
    yield
    after = path.read_bytes() if path.exists() else None
    assert before == after, (
        f"a test modified the real user config at {path}; "
        "stub Novaconfig on the name the code resolves, or patch config.HOME_DIR"
    )


@pytest.fixture(autouse=True)
def _reset_shell_jobs():
    """Isolate the process-global background-job registry between tests.

    Jobs, observers, and completion callbacks are a module singleton; without a
    reset, a running job (or a stale TUI observer) from one test leaks into the
    next — e.g. starting the ⚙ tasks-bar ticker inside an unrelated TUI test.
    """
    from novacode_cli.shell.jobs import get_registry

    get_registry().reset()
    yield
    get_registry().reset()


@pytest.fixture(autouse=True)
def _isolate_tui_slash_commands():
    """Undo runtime mutation of the global slash-command list.

    ``tui/app.py`` builds ``_TUI_SLASH_COMMANDS`` from the built-in table, then
    ``_load_plugin_commands()`` APPENDS the developer's installed plugin commands
    to it at app mount. Once any test boots a NovaApp those entries persist for
    the rest of the process and leak into every later test's autocomplete.

    Looked up via ``sys.modules`` so this costs nothing for the many tests that
    never import the (heavy) TUI module.
    """
    import sys

    yield

    mod = sys.modules.get("novacode_cli.tui.app")
    if mod is None:
        return
    # Rebuild from the built-in table rather than restoring a snapshot: a
    # snapshot taken at setup already contains whatever an earlier test leaked,
    # so it would preserve the pollution instead of removing it.
    mod._TUI_SLASH_COMMANDS[:] = [f"/{name}" for name in mod.TUI_COMMANDS]


@pytest.fixture(autouse=True)
def _reap_leaked_session_children():
    """Kill any spawned-session child process a test left behind.

    ``SessionSupervisor`` tracks live children in a module-global set for its
    atexit sweep. A test that fails before closing one would leave a real
    process holding pipes open, stalling later tests; reap them here instead.
    """
    yield

    from novacode_cli.sessions import supervisor as sup

    for child in list(sup._live):
        proc = getattr(child, "proc", None)
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 — already gone
                pass
        sup._live.discard(child)
