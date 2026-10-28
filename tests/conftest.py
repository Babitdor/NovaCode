"""Shared test fixtures."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest


@pytest.fixture(autouse=True)
def _isolate_prime_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prime/handoff tests and incidental runs never write the user's project records."""
    from langgraph.store.memory import InMemoryStore

    from novacode_cli import handoff, prime_memory

    store = InMemoryStore()
    monkeypatch.setattr(prime_memory, "get_durable_store", lambda: store)
    monkeypatch.setattr(handoff, "get_durable_store", lambda: store)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

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
    "TYPESAFE_API_KEY",
    "SYSTEM_ONE_API_KEY",
)


@pytest.fixture(autouse=True)
def _isolate_directory_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline agent graphs must never discover or execute the user's plugins."""
    from novacode_cli.plugins import claude_plugins, marketplaces

    directory = tmp_path / "plugin-home"
    project = tmp_path / "plugin-project"
    monkeypatch.setattr(claude_plugins, "PLUGINS_DIR", directory / "plugins")
    monkeypatch.setattr(claude_plugins, "MANIFEST", directory / "plugins/installed.json")
    monkeypatch.setattr(claude_plugins, "project_root", lambda: project)
    monkeypatch.setattr(claude_plugins, "SESSION_PLUGINS", [])
    monkeypatch.setattr(marketplaces, "MARKETPLACES_DIR", directory / "marketplaces")
    monkeypatch.setattr(marketplaces, "MANIFEST", directory / "marketplaces/marketplaces.json")


@pytest.fixture(autouse=True)
def _isolate_learning_auto_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep generated proposals pending regardless of the developer's config."""
    from novacode_cli.hermes import ledger, skill_proposals

    monkeypatch.setattr(ledger, "auto_approve_enabled", lambda: False)
    # This module imports the helper by name, so patch its bound reference too.
    monkeypatch.setattr(skill_proposals, "auto_approve_enabled", lambda: False)


@pytest.fixture(autouse=True)
def _isolate_shell_logs(tmp_path, monkeypatch):
    """Full outputs of truncated test commands never land in the user's ~/.nova/logs."""
    from novacode_cli.shell import output

    monkeypatch.setattr(output, "shell_log_dir", lambda: tmp_path / "shell-logs")


@pytest.fixture(autouse=True)
def _isolate_recovery_archives(tmp_path, monkeypatch):
    """Test compaction never publishes archives into the user's recovery store."""
    from novacode_cli.context import archives
    store = archives.RecoveryStore(tmp_path / "recovery")
    monkeypatch.setattr(archives, "default_recovery_store", lambda: store)


@pytest.fixture(autouse=True)
def _isolate_prompt_overrides(tmp_path, monkeypatch):
    """A user's active prompts must never alter offline fixture behavior."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from novacode_cli import prompts
    from novacode_cli.hermes import prompt_evolution
    directory = tmp_path / "prompt-history"
    monkeypatch.setattr(prompts, "PROMPT_HISTORY_DIR", directory)
    monkeypatch.setattr(prompt_evolution, "PROMPT_HISTORY_DIR", directory)
    monkeypatch.setattr(prompts, "_override_env", Environment(
        loader=FileSystemLoader(str(directory)), autoescape=select_autoescape(default=False),
    ))


@pytest.fixture(autouse=True)
def _restore_async_graph_imports():
    """Discard graphs imported while a test replaced the graph builder/model."""
    prefix = "novacode_cli.agents.async_agents."
    before = {name for name in tuple(sys.modules) if name.startswith(prefix)}
    yield
    for name in list(sys.modules):
        if name.startswith(prefix) and name not in before:
            module = sys.modules.pop(name, None)
            parent, _, attribute = name.rpartition(".")
            package = sys.modules.get(parent)
            if package is not None and getattr(package, attribute, None) is module:
                delattr(package, attribute)


@pytest.fixture(autouse=True)
def _forget_the_cached_context_budget() -> Iterator[None]:
    """The budget setting is cached for a few seconds; no test may inherit one."""
    from novacode_cli.context import _analysis

    _analysis._budget_read = None
    yield
    _analysis._budget_read = None


@pytest.fixture(autouse=True)
def _restore_credential_env_vars() -> Iterator[None]:
    """Undo credential exports a test made directly through ``os.environ``.

    ``monkeypatch`` cannot see those: it only reverts what it set itself, so a
    test that saved a key left it exported for the whole session. That is how a
    leaked ``TAVILY_API_KEY`` once flipped the tool roster another test asserts
    on. Restores exact prior presence (set, unset, or empty) for the credential
    vars only, leaving the rest of the environment to monkeypatch.
    """
    from novacode_cli.config.provider_auth import credential_names

    saved = {
        name: os.environ.get(name) for name in (*_CREDENTIAL_ENV_VARS, *credential_names().values())
    }
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
    from novacode_cli.config.nova_config import NovaConfig

    path = Path.home() / ".nova" / "Nova.config.json"
    before = path.read_bytes() if path.exists() else None
    original_save = NovaConfig._save

    def guarded_save(config):
        if config.config_path.resolve() == path.resolve():
            raise AssertionError(
                "test attempted to write the real user config; isolate NovaConfig first"
            )
        return original_save(config)

    NovaConfig._save = guarded_save
    try:
        yield
    finally:
        NovaConfig._save = original_save
    after = path.read_bytes() if path.exists() else None
    assert before == after, (
        f"a test modified the real user config at {path}; "
        "stub Novaconfig on the name the code resolves, or patch config.HOME_DIR"
    )


@pytest.fixture
def isolated_plan_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Keep approval tests out of the real workspace and personal plan archive."""
    from novacode_cli import plan_archive
    from novacode_cli.config.config import settings

    project = tmp_path / "approval-project"
    project.mkdir(exist_ok=True)
    global_plans = tmp_path / "home" / ".nova" / "plans"
    monkeypatch.setattr(settings, "project_root", project)
    monkeypatch.setattr(plan_archive, "global_plans_root", lambda: global_plans)
    return project, global_plans


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


@pytest.fixture(autouse=True)
def _no_leaked_agent_server_plan():
    """A planned agent server makes async agents count as available everywhere.

    The plan is process-global, so one test that plans a server (and leaves it
    planned, as a failed launch deliberately does) would change what every later
    test sees.
    """
    import sys

    yield
    launcher = sys.modules.get("novacode_cli.agents.server_launcher")
    if launcher is not None:
        launcher._state["planned"] = None
        launcher._state["process"] = None


@pytest.fixture(autouse=True)
def _no_builtin_lessons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memory tests assert on exactly the lessons they wrote.

    The lessons Nova ships (``novacode_cli.memory.builtin_lessons``) would be
    recalled alongside them; ``tests/test_builtin_lessons.py`` turns them back on.
    """
    monkeypatch.setenv("NOVA_BUILTIN_LESSONS", "0")
