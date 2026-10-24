"""Default-on independent attribution with a visible PR logo and valid Git trailers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest, ToolCallRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel, GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

from novacode_cli.agents.attribution import (
    COAUTHOR,
    LOGO_URL,
    PR_FOOTER,
    AttributionMiddleware,
    commit_message,
    pull_request_body,
)
from novacode_cli.config.nova_config import NovaConfig


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config import config

    monkeypatch.setattr(config, "HOME_DIR", tmp_path / ".nova")


def request(system: SystemMessage | None = None):
    return ModelRequest(
        model=FakeListChatModel(responses=["ok"]), messages=[], system_message=system
    )


def test_defaults_and_independent_persistent_opt_out():
    config = NovaConfig()
    assert config.get_attribution() == {"commits": True, "pullRequests": True}
    config.set("attribution", {"commits": False, "pullRequests": True})
    assert NovaConfig().get_attribution() == {"commits": False, "pullRequests": True}
    config.set("attribution", {"commits": True, "pullRequests": False})
    assert NovaConfig().get_attribution() == {"commits": True, "pullRequests": False}
    config.set("attribution", {"commits": False, "pullRequests": False})
    assert not any(NovaConfig().get_attribution().values())


def test_linked_coauthor_identity_persists_and_updates_prompts_and_git_payloads(tmp_path: Path):
    identity = {"name": "NovaCode", "email": "42+nova-fixture@users.noreply.github.com"}
    trailer = f"Co-Authored-By: {identity['name']} <{identity['email']}>"
    config = NovaConfig()
    config.set("attribution", {"coAuthor": identity})
    assert NovaConfig().get_attribution_identity() == identity
    assert config.get_attribution() == {"commits": True, "pullRequests": True}
    prepared = AttributionMiddleware.prepare(request())
    assert trailer in prepared.system_message.text
    assert COAUTHOR not in prepared.system_message.text
    original = ToolCallRequest(
        {
            "id": "commit",
            "name": "git_commit",
            "args": {"message": "Fix issue\n\nSigned-off-by: Human <human@example.invalid>\n"},
        },
        None,
        {},
        None,
    )
    attributed = AttributionMiddleware.prepare_tool(original)
    message = attributed.tool_call["args"]["message"]
    parsed = git("interpret-trailers", "--parse", cwd=tmp_path, stdin=message)
    assert parsed.count(trailer) == 1
    assert "Signed-off-by: Human <human@example.invalid>" in parsed
    assert COAUTHOR not in parsed
    assert AttributionMiddleware.prepare_tool(attributed).tool_call == attributed.tool_call
    config.set("attribution", {"commits": False, "coAuthor": identity})
    assert AttributionMiddleware.prepare_tool(original) is original
    assert trailer not in AttributionMiddleware.prepare(request()).system_message.text


@pytest.mark.parametrize(
    "identity",
    [
        None,
        {},
        [],
        {"name": "NovaCode"},
        {"email": "not-an-email"},
        {"name": "Nova\nForged: Trailer", "email": "nova@example.invalid"},
        {"name": "Nova\u2028Forged: Trailer", "email": "nova@example.invalid"},
        {"name": "Nova <Other>", "email": "nova@example.invalid"},
        {"name": "NovaCode", "email": "nova@example.invalid\nCo-authored-by: Forged"},
        {"email": "nova@"},
        {"email": "@example.invalid"},
        {"email": "nova@@example.invalid"},
        {"name": 42, "email": "nova@example.invalid"},
    ],
)
def test_invalid_coauthor_identity_keeps_safe_unlinked_default(identity: object):
    NovaConfig().set("attribution", {"coAuthor": identity})
    assert NovaConfig().get_attribution_identity() == {
        "name": "NovaCode",
        "email": "noreply@novacode.invalid",
    }


def test_coauthor_can_use_default_name_and_opt_out_does_not_remove_identity():
    identity = {"email": "42+nova-fixture@users.noreply.github.com"}
    NovaConfig().set("attribution", {"commits": False, "pullRequests": False, "coAuthor": identity})
    assert not any(NovaConfig().get_attribution().values())
    assert NovaConfig().get_attribution_identity() == {"name": "NovaCode", **identity}


@pytest.mark.parametrize("value", [None, [], "invalid", {}])
def test_legacy_configs_keep_defaults(value: object):
    NovaConfig().set("attribution", value)
    assert NovaConfig().get_attribution() == {"commits": True, "pullRequests": True}


@pytest.mark.parametrize(
    "system",
    [
        None,
        SystemMessage(content="Coding rules"),
        SystemMessage(
            content=[
                {"type": "text", "text": "Coding rules", "cache_control": {"type": "ephemeral"}}
            ]
        ),
    ],
)
def test_prompt_includes_live_preferences_without_mutating_base(system: SystemMessage | None):
    original = request(system)
    prepared = AttributionMiddleware.prepare(original)
    assert COAUTHOR in prepared.system_message.text
    assert PR_FOOTER in prepared.system_message.text
    assert "do not authorize" in prepared.system_message.text
    if system:
        assert COAUTHOR not in system.text
        if isinstance(system.content, list):
            assert prepared.system_message.content[0] == system.content[0]
    NovaConfig().set("attribution", {"commits": False, "pullRequests": False})
    disabled = AttributionMiddleware.prepare(original)
    assert COAUTHOR not in disabled.system_message.text
    assert LOGO_URL not in disabled.system_message.text
    assert "Do not add automatic" in disabled.system_message.text


async def test_sync_and_async_model_wrappers_apply_preference():
    middleware = AttributionMiddleware()
    sync = Mock(return_value="sync")
    assert middleware.wrap_model_call(request(), sync) == "sync"
    assert COAUTHOR in sync.call_args.args[0].system_message.text
    async_handler = AsyncMock(return_value="async")
    assert await middleware.awrap_model_call(request(), async_handler) == "async"
    assert LOGO_URL in async_handler.call_args.args[0].system_message.text


def git(*args: str, cwd: Path, stdin: str | None = None):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(  # noqa: S603 -- test-owned arguments in an isolated repository.
        ["git", *args],  # noqa: S607 -- Git must be on PATH for Nova's Git features.
        cwd=cwd,
        env=env,
        input=stdin,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout


def test_git_parses_preserved_human_trailers_and_one_nova_coauthor(tmp_path: Path):
    message = (
        "Fix parsing\n\nPreserve exact Windows path: C:\\work\\example.py\n\n"
        "Signed-off-by: Human <human@example.invalid>\n"
        "Co-Authored-By: Peer <peer@example.invalid>\n"
    )
    attributed = commit_message(message)
    parsed = git("interpret-trailers", "--parse", cwd=tmp_path, stdin=attributed)
    assert "Signed-off-by: Human <human@example.invalid>" in parsed
    assert "Co-Authored-By: Peer <peer@example.invalid>" in parsed
    assert parsed.count(COAUTHOR) == 1
    assert commit_message(attributed) == attributed
    assert "C:\\work\\example.py" in attributed


def test_actual_commit_retains_human_author(tmp_path: Path):
    git("init", cwd=tmp_path)
    message = commit_message("A synthetic test commit")
    git(
        "-c",
        "user.name=Human",
        "-c",
        "user.email=human@example.invalid",
        "-c",
        "core.hooksPath=" + str(tmp_path / "no-hooks"),
        "commit",
        "--allow-empty",
        "-F",
        "-",
        cwd=tmp_path,
        stdin=message,
    )
    result = git("log", "-1", "--format=%an <%ae>%n%B", cwd=tmp_path)
    assert result.startswith("Human <human@example.invalid>")
    assert result.count(COAUTHOR) == 1


def test_pr_footer_has_small_repository_hosted_logo_and_is_idempotent():
    body = "## Changes\n\nUser's exact content.\n"
    result = pull_request_body(body)
    assert result.startswith(body.rstrip())
    assert result.count(LOGO_URL) == 1
    assert 'width="24" height="24"' in result
    assert pull_request_body(result) == result
    image = Path(__file__).resolve().parents[1] / "assets" / "nova-attribution.png"
    assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_creation_tools_and_opt_out_preserve_original_payloads():
    @tool("github_create_pull_request")
    def create(body: str) -> str:
        """Create a synthetic PR."""
        return body

    call = {"id": "pr", "name": create.name, "args": {"body": "Review me"}}
    original = ToolCallRequest(call, create, {}, None)
    prepared = AttributionMiddleware.prepare_tool(original)
    assert LOGO_URL in prepared.tool_call["args"]["body"]
    assert original.tool_call["args"] == {"body": "Review me"}
    assert AttributionMiddleware.prepare_tool(prepared).tool_call == prepared.tool_call
    NovaConfig().set("attribution", {"commits": True, "pullRequests": False})
    assert AttributionMiddleware.prepare_tool(original) is original


def test_missing_pr_body_is_added_only_when_supported_by_schema():
    @tool("create_pull_request")
    def supported(title: str, body: str = "") -> str:
        """Synthetic PR with optional body."""
        return title + body

    @tool("create_pull_request")
    def unsupported(title: str) -> str:
        """Synthetic PR without a body field."""
        return title

    call = {"id": "pr", "name": "create_pull_request", "args": {"title": "Title"}}
    prepared = AttributionMiddleware.prepare_tool(ToolCallRequest(call, supported, {}, None))
    assert PR_FOOTER in prepared.tool_call["args"]["body"]
    for candidate in (unsupported, None):
        original = ToolCallRequest(call, candidate, {}, None)
        assert AttributionMiddleware.prepare_tool(original) is original


@pytest.mark.parametrize("body", [None, 42, ["description"]])
def test_invalid_arguments_are_left_for_schema_validation(body: object):
    original = ToolCallRequest(
        {"id": "pr", "name": "create_pull_request", "args": {"body": body}}, None, {}, None
    )
    assert AttributionMiddleware.prepare_tool(original) is original


@pytest.mark.parametrize(
    "name", ["git_commit", "github_push_files", "github_create_or_update_file"]
)
def test_structured_commit_creation_and_opt_out(name: str):
    original = ToolCallRequest(
        {"id": "commit", "name": name, "args": {"message": "Fix behavior"}}, None, {}, None
    )
    prepared = AttributionMiddleware.prepare_tool(original)
    assert prepared.tool_call["args"]["message"].count(COAUTHOR) == 1
    assert original.tool_call["args"]["message"] == "Fix behavior"
    NovaConfig().set("attribution", {"commits": False, "pullRequests": True})
    assert AttributionMiddleware.prepare_tool(original) is original


async def test_unrelated_tools_and_shell_commands_pass_through_unchanged():
    middleware = AttributionMiddleware()
    original = ToolCallRequest(
        {"id": "s", "name": "shell", "args": {"command": "git commit -F 'message with spaces.md'"}},
        None,
        {},
        None,
    )
    sync = Mock(return_value="ok")
    assert middleware.wrap_tool_call(original, sync) == "ok"
    assert sync.call_args.args[0] is original
    async_handler = AsyncMock(return_value="ok")
    assert await middleware.awrap_tool_call(original, async_handler) == "ok"
    assert async_handler.call_args.args[0] is original


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, *_args: object, **_kwargs: object):
        return self


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("asynchronous", [True, False])
async def test_real_agent_executes_attributed_payload(*, enabled: bool, asynchronous: bool):
    NovaConfig().set("attribution", {"commits": enabled, "pullRequests": enabled})
    received = []

    @tool("github_create_pull_request")
    def create(body: str) -> str:
        """Capture a synthetic PR without network calls."""
        received.append(body)
        return "created"

    graph = create_agent(
        ScriptedModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "pr",
                                "name": create.name,
                                "args": {"body": "User content"},
                            }
                        ],
                    ),
                    AIMessage(content="Done"),
                ]
            )
        ),
        tools=[create],
        middleware=[AttributionMiddleware()],
    )
    state = {"messages": [HumanMessage(content="Create the synthetic PR")]}
    if asynchronous:
        await graph.ainvoke(state)
    else:
        graph.invoke(state)
    assert len(received) == 1
    assert received[0].startswith("User content")
    assert (LOGO_URL in received[0]) is enabled


async def test_mcp_code_mode_nested_creation_receives_logo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.security import policy, session_allow
    from tests.test_mcp_code_mode import execute, graph, inventory

    monkeypatch.setattr(policy, "_read_json", lambda _path: {})
    monkeypatch.setattr(policy, "_CACHED_POLICY", policy.load_policy(tmp_path))
    monkeypatch.setattr(session_allow, "_SESSION_ALLOW", None)
    received = []

    @tool("github_create_pull_request")
    def create(body: str) -> str:
        """Capture a nested synthetic PR."""
        received.append(body)
        return "created"

    mcp = inventory(tmp_path, create)
    messages = await execute(
        graph(
            mcp,
            'return await mcp.call("db", "read", {body: "Nested PR"});',
            middleware=[AttributionMiddleware()],
        )
    )
    assert messages[0].status == "success"
    assert len(received) == 1
    assert LOGO_URL in received[0]


@pytest.mark.timeout(30)
def test_local_and_background_agents_receive_one_attribution_layer():
    from novacode_cli.agents.core_agent import _harden_subagent_specs
    from novacode_cli.context.snapshot import background_context_middleware

    specs = [{"name": "test", "system_prompt": "p", "middleware": []}]
    hardened = _harden_subagent_specs(_harden_subagent_specs(specs))
    assert sum(isinstance(m, AttributionMiddleware) for m in hardened[0]["middleware"]) == 1
    assert sum(isinstance(m, AttributionMiddleware) for m in background_context_middleware()) == 1
