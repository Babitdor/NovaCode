"""Shared test fixtures."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

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
def _restore_credential_env_vars() -> Iterator[None]:
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


@pytest.fixture
def no_blocking_io(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail the test if the TUI makes a blocking call on the event loop.

    BlockBuster raises ``BlockingError`` when a patched blocking call (file read,
    subprocess, socket, ``time.sleep``) runs while an asyncio loop is active.
    Nova's TUI shares that loop with the agent, so a synchronous read or
    subprocess in a handler freezes the whole UI rather than one request.

    Scoped to the ``novacode_cli.tui`` package, because BlockBuster only fires
    when a frame from a scanned module is on the stack: a package scans its whole
    directory, so this covers ``app.py``, ``screens.py`` and the rest while
    leaving Textual's own internals alone.

    Opt-in, NOT autouse, because the modal screens still do synchronous
    filesystem work in some of their action handlers. Arming this run-wide today
    fails ~25 tests that are unrelated to any change under review. Measured
    sites, all in ``novacode_cli/tui/screens.py`` plus one callee:

      - ``_create_agent``      -> ``ensure_project_agents_dir``, ``exists``, write
      - ``_delete_agent``      -> unlink / rmtree
      - ``_reload`` (skills)   -> ``skills_prefs.load_disabled`` -> ``exists``
      - ``_reload`` (wiki)     -> ``WikiManager`` listing
      - ``_enter_prune`` / ``_archive_marked`` -> prefs + moves
      - ``_save`` (agents)     -> ``set_agent_tools`` writes ``agent.md``

    Convert those the same way the preview readers were (a sync helper run with
    ``asyncio.to_thread``, or a ``run_worker``), then make this autouse: the
    scaffolding (including the marker below) is already in place.

    A test that arms BlockBuster itself marks ``@pytest.mark.own_blockbuster_guard``
    and this stands down: two BlockBusters patch the same functions, and
    ``BlockBusterFunction.deactivate`` restores the captured original with no
    reference count, so nesting them is unsafe.
    """
    if request.node.get_closest_marker("own_blockbuster_guard"):
        yield
        return
    try:
        from blockbuster import blockbuster_ctx
    except ImportError:  # pragma: no cover - the guard is test-only
        yield
        return
    with blockbuster_ctx(scanned_modules=["novacode_cli.tui"]):
        yield


@pytest.fixture(scope="session", autouse=True)
def _guard_the_real_user_config() -> Iterator[None]:
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
