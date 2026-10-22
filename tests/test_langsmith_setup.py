"""The LangSmith environment can be entered at runtime and really activates tracing.

Setting ``LANGSMITH_*`` after the process started is not enough on its own:
``langsmith`` caches the environment (``get_env_var`` and ``get_tracer_project``
are ``lru_cache``d) and keeps a global ``Client``. Without clearing those
caches no tracer is ever attached, so the change silently does nothing.
"""

from __future__ import annotations

import os

import pytest

from novacode_cli.tracking.tracing import (
    LANGSMITH_ENV_VARS,
    apply_langsmith_env,
    get_tracing_config,
    reset_langsmith_caches,
)


class _StubSecretManager:
    store: dict[str, str] = {}

    def __init__(self):
        pass

    def get_secret(self, name):
        return self.store.get(name)

    def store_secret(self, name, value):
        self.store[name] = value
        return True

    def delete_secret(self, name):
        self.store.pop(name, None)
        return True


@pytest.fixture(autouse=True)
def _clean_langsmith_env(monkeypatch, tmp_path):
    """Keep the developer's LangSmith environment out of these tests.

    ``apply_langsmith_env`` writes to ``os.environ`` directly, and
    ``monkeypatch`` does not see that, so the variables are put back by hand.
    """
    import novacode_cli.config.config as config_mod
    from novacode_cli.tracking import tracing

    monkeypatch.setattr(config_mod, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", _StubSecretManager)
    _StubSecretManager.store = {}
    monkeypatch.setattr(tracing, "_tracing_config", tracing.TracingConfig())
    saved = {name: os.environ.get(name) for name in LANGSMITH_ENV_VARS}
    for name in LANGSMITH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    reset_langsmith_caches()
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    reset_langsmith_caches()


def _tracers() -> list:
    """The tracers LangChain would attach to the next model call."""
    from langchain_core.callbacks.manager import CallbackManager

    return [h for h in CallbackManager.configure().handlers if type(h).__name__ == "LangChainTracer"]


def test_apply_sets_every_variable_and_activates_tracing():
    config = apply_langsmith_env(
        {
            "LANGSMITH_TRACING": "true",
            "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
            "LANGSMITH_API_KEY": "lsv2_test_key",
            "LANGSMITH_PROJECT": "MyProject",
        }
    )

    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_ENDPOINT"] == "https://api.smith.langchain.com"
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_test_key"
    assert os.environ["LANGSMITH_PROJECT"] == "MyProject"
    assert config.is_configured()
    assert config.project_name == "MyProject"

    # The point of the change: the next model call carries a tracer.
    tracers = _tracers()
    assert len(tracers) == 1
    assert tracers[0].project_name == "MyProject"


def test_tracing_activates_without_restarting_the_process():
    """The cached environment must not swallow a change made at runtime."""
    from langchain_core.callbacks.manager import CallbackManager

    # Simulate a process that already read the environment (no tracing on).
    assert not [h for h in CallbackManager.configure().handlers if type(h).__name__ == "LangChainTracer"]

    apply_langsmith_env(
        {
            "LANGSMITH_TRACING": "true",
            "LANGSMITH_API_KEY": "lsv2_test_key",
            "LANGSMITH_PROJECT": "LateProject",
        }
    )

    tracers = _tracers()
    assert len(tracers) == 1
    assert tracers[0].project_name == "LateProject"


def test_tracing_without_an_api_key_stays_off():
    config = apply_langsmith_env(
        {
            "LANGSMITH_TRACING": "true",
            "LANGSMITH_API_KEY": "",
            "LANGSMITH_PROJECT": "NoKeyProject",
        }
    )

    assert not config.is_configured()
    assert not _tracers()


def test_disabling_tracing_removes_the_tracer():
    apply_langsmith_env(
        {
            "LANGSMITH_TRACING": "true",
            "LANGSMITH_API_KEY": "lsv2_test_key",
            "LANGSMITH_PROJECT": "MyProject",
        }
    )
    assert _tracers()

    apply_langsmith_env({"LANGSMITH_TRACING": "false"})

    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert not _tracers()


def test_empty_value_removes_the_variable():
    os.environ["LANGSMITH_API_KEY"] = "lsv2_test_key"

    apply_langsmith_env({"LANGSMITH_API_KEY": ""})

    assert "LANGSMITH_API_KEY" not in os.environ


class _FakeConsole:
    """A console that answers the setup prompts with fixed values."""

    def __init__(self, answers):
        self._answers = list(answers)
        self.printed: list = []

    def input(self, prompt="", **kwargs):
        return self._answers.pop(0) if self._answers else ""

    def print(self, *args, **kwargs):
        self.printed.append(args)


async def test_the_cli_setup_applies_what_was_typed(monkeypatch):
    """``/trace setup`` outside the TUI asks the same four questions."""
    from novacode_cli.commands import trace_handler

    fake = _FakeConsole(
        [
            "true",
            "https://api.smith.langchain.com",
            "lsv2_cli_key",
            "CliProject",
        ]
    )
    monkeypatch.setattr(trace_handler, "console", fake)

    await trace_handler.handle_trace_command(["setup"])

    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_ENDPOINT"] == "https://api.smith.langchain.com"
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_cli_key"
    assert os.environ["LANGSMITH_PROJECT"] == "CliProject"
    assert get_tracing_config().is_configured()
    assert _StubSecretManager.store["langsmith_api_key"] == "lsv2_cli_key"
    from novacode_cli.config.nova_config import NovaConfig

    saved = NovaConfig().get_langsmith_settings()
    assert saved["project"] == "CliProject"


async def test_the_cli_setup_keeps_the_current_value_when_the_answer_is_empty(
    monkeypatch,
):
    """An empty answer must not wipe a value that is already in use."""
    from novacode_cli.commands import trace_handler

    os.environ["LANGSMITH_PROJECT"] = "KeptProject"
    fake = _FakeConsole(["", "", "", ""])
    monkeypatch.setattr(trace_handler, "console", fake)

    await trace_handler.handle_trace_command(["setup"])

    assert os.environ["LANGSMITH_PROJECT"] == "KeptProject"


def test_saved_setup_is_restored_for_the_next_session():
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.tracking.tracing import (
        load_persisted_langsmith_settings,
        persist_langsmith_settings,
    )

    persist_langsmith_settings(
        {
            "LANGSMITH_TRACING": "true",
            "LANGSMITH_API_KEY": "lsv2_persisted",
            "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
            "LANGSMITH_PROJECT": "SavedProject",
            "LANGSMITH_WORKSPACE_ID": "workspace-1",
            "LANGSMITH_AGENT_ID": "novacode-cli",
            "LANGSMITH_AGENT_ENVIRONMENT": "production",
        }
    )
    for name in LANGSMITH_ENV_VARS:
        os.environ.pop(name, None)

    config = load_persisted_langsmith_settings()

    assert config.is_configured()
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_persisted"
    assert os.environ["LANGSMITH_PROJECT"] == "SavedProject"
    assert os.environ["LANGSMITH_ENDPOINT"] == "https://api.smith.langchain.com"
    assert os.environ["LANGSMITH_WORKSPACE_ID"] == "workspace-1"
    assert os.environ["LANGSMITH_AGENT_ID"] == "novacode-cli"
    assert os.environ["LANGSMITH_AGENT_ENVIRONMENT"] == "production"
    assert "lsv2_persisted" not in NovaConfig().config_path.read_text(encoding="utf-8")
