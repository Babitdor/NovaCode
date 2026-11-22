"""App-side commands the socket port answers without the Textual app.

Each row here used to be `app-side` in the catalogue: the behaviour lived on
`NovaApp` methods the server cannot reach. The port either delegates to the same
shared module the Textual handler calls or says exactly what it cannot do.
"""

from __future__ import annotations

import webbrowser
from types import SimpleNamespace
from typing import Any

import pytest

from novacode_cli.ui.port_server import ServerPort


class _State:
    """Session state carrying only what these commands read."""

    def __init__(self) -> None:
        self.thread_id = "t1"
        self.session_id = "s1"
        self.auto_approve = False
        self.todos: list[Any] = []
        self.steering_instructions: list[Any] = []
        self.headless_out_fd = None

    async def reload_mcp_servers(self) -> tuple[object, object]:
        """Mirror the agent rebuild the Textual handler performs."""
        return (object(), object())


def _port(sandbox_id: str | None = None) -> ServerPort:
    worker = SimpleNamespace(
        agent=object(),
        session_state=_State(),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
    )
    return ServerPort(list.append.__get__([]), worker, sandbox_id=sandbox_id)


@pytest.mark.parametrize(
    ("command", "module"),
    [
        ("cron", "novacode_cli.commands.cron_handler"),
        ("webhook", "novacode_cli.commands.webhook_handler"),
        ("prompt", "novacode_cli.commands.prompt_handler"),
    ],
)
async def test_unregistered_shared_commands_reach_their_handler(
    command: str, module: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`handle_command` owns these; the port must not refuse them as unported."""
    import importlib

    handler = importlib.import_module(module)
    called: list[str] = []

    async def fake(*args: Any, **kwargs: Any) -> bool:
        called.append(command)
        return True

    monkeypatch.setattr(handler, f"handle_{command}_command", fake)
    port = _port()
    result = await port.run(f"/{command}", "status")
    assert result.ok is True
    assert called == [command]


async def test_ponytail_commands_start_the_helper_turn() -> None:
    from novacode_cli.agents.ponytail import helper_prompt

    port = _port()
    result = await port.run("/ponytail-review", "check the diff")
    assert result.ok is True
    assert result.prompt == helper_prompt("/ponytail-review check the diff")
    assert "check the diff" in result.prompt


async def test_reload_plugins_swaps_the_agent_and_counts_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli.plugins import claude_plugins

    reloaded: list[str] = []
    monkeypatch.setattr(claude_plugins, "list_plugins", lambda: ["a", "b"])
    monkeypatch.setattr("novacode_cli.hooks.reload_hooks", lambda: reloaded.append("hooks"))
    port = _port()
    before = port._worker.agent
    result = await port.run("/reload-plugins", None)
    assert result.ok is True
    assert reloaded == ["hooks"]
    assert port._worker.agent is not before
    assert "Reloaded 2 plugin(s)" in result.blocks[0].text


async def test_cowork_reports_the_url_it_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli.cowork import launcher

    opened: list[str] = []
    monkeypatch.setattr(launcher, "cowork_url", lambda **_kwargs: "http://127.0.0.1:9/cowork")
    monkeypatch.setattr(webbrowser, "open", opened.append)
    result = await _port().run("/cowork", "sort my notes")
    assert result.ok is True
    assert opened == ["http://127.0.0.1:9/cowork"]
    assert result.blocks[0].text == "◆ Nova Cowork ready — http://127.0.0.1:9/cowork"


class _Computer:
    """One guest that answers with a fixed phase."""

    def __init__(self) -> None:
        self.operations: list[str] = []

    def preview_url(self) -> str:
        return "http://127.0.0.1:9/preview"

    async def call(self, operation: str) -> dict[str, Any]:
        self.operations.append(operation)
        return {"phase": "ready", "paused": False}

    def close(self) -> bool:
        return True


async def test_computer_preview_opens_the_viewer_and_reports_the_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli.computer import runtime

    guest = _Computer()
    opened: list[str] = []
    monkeypatch.setattr(runtime, "get_computer", lambda _sid: guest)
    monkeypatch.setattr(webbrowser, "open", opened.append)
    result = await _port().run("/computer", "preview")
    assert result.ok is True
    assert opened == ["http://127.0.0.1:9/preview"]
    assert guest.operations == ["status"]
    assert result.message.startswith("Computer: ready")


async def test_computer_refuses_to_start_guest_vms_inside_a_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli.computer import runtime

    monkeypatch.setattr(runtime, "get_computer", lambda _sid: pytest.fail("must not start"))
    result = await _port(sandbox_id="box").run("/computer", "start")
    assert result.ok is False
    assert result.message == "Computer controls require a local Nova session."


async def test_computer_rejects_unknown_verbs_with_the_usage_line() -> None:
    result = await _port().run("/computer", "format-disk")
    assert result.ok is False
    assert result.message.startswith("Usage: /computer")


def _artifact(identifier: str, title: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=identifier, title=title, type="html", version=2, status="ready"
    )


async def test_artifacts_empty_state_matches_the_textual_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli.artifacts import registry

    monkeypatch.setattr(registry, "get_registry", lambda: SimpleNamespace(list=lambda: []))
    result = await _port().run("/artifacts", None)
    assert result.ok is True
    assert result.blocks[0].text.startswith("No artifacts yet")


async def test_artifacts_opens_the_chosen_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli.artifacts import registry, server

    artifacts = [_artifact("a1", "Report"), _artifact("a2", "Chart")]
    monkeypatch.setattr(registry, "get_registry", lambda: SimpleNamespace(list=lambda: artifacts))
    monkeypatch.setattr(server, "artifact_url", lambda artifact_id: f"http://x/{artifact_id}")
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    asked: list[Any] = []

    async def ask_screen(spec: Any) -> Any:
        asked.append(spec)
        return {"id": "a2"}

    worker = _port()._worker
    port = ServerPort(list.append.__get__([]), worker, ask_screen=ask_screen)
    result = await port.run("/artifacts", None)
    assert asked[0].payload["items"][1]["label"] == "◈ Chart"
    assert opened == ["http://x/a2"]
    assert result.message == "◈ Opening Chart → http://x/a2"


@pytest.mark.parametrize(
    "name",
    [
        "artifacts",
        "computer",
        "cowork",
        "cron",
        "ponytail-audit",
        "ponytail-debt",
        "ponytail-gain",
        "ponytail-help",
        "ponytail-review",
        "prompt",
        "reload-plugins",
        "webhook",
    ],
)
def test_each_new_port_command_has_its_own_catalogue_evidence(name: str) -> None:
    from novacode_cli.server.catalog import get

    spec = get(name)
    assert spec is not None
    assert (spec.route, spec.served, spec.evidence) == ("passthrough", True, "port-command")
