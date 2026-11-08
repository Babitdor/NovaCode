"""Failed background runs retain their checkpoint error, without child messages."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from novacode_cli.agents.async_task_errors import bind_run_errors


def client_for(
    *, asynchronous: bool = False, state: dict | None = None, run: dict | None = None
) -> SimpleNamespace:
    mock = AsyncMock if asynchronous else Mock
    return SimpleNamespace(
        runs=SimpleNamespace(get=mock(return_value=run or {"status": "error"})),
        threads=SimpleNamespace(
            get_state=mock(
                return_value=state
                or {
                    "metadata": {"run_id": "r"},
                    "tasks": [
                        {"error": "PermissionDeniedError: 403 - free model window has ended"}
                    ],
                    "values": {"messages": ["PRIVATE CHILD INTERMEDIATE"]},
                }
            )
        ),
    )


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_failed_run_reports_checkpoint_error(*, asynchronous: bool):
    client = client_for(asynchronous=asynchronous)
    bind_run_errors(client)
    bound = client.runs.get
    bind_run_errors(client)
    assert client.runs.get is bound
    result = client.runs.get(thread_id="t", run_id="r")
    if asynchronous:
        result = await result
    assert "403" in result["error"]
    assert "window has ended" in result["error"]
    assert "PRIVATE CHILD" not in str(result)
    client.threads.get_state.assert_called_once_with(thread_id="t")


@pytest.mark.parametrize(
    "run",
    [
        {"status": "success"},
        {"status": "running"},
        {"status": "error", "error": "already supplied"},
    ],
)
def test_other_statuses_and_existing_errors_do_not_fetch_state(run: dict):
    client = client_for(run=run)
    bind_run_errors(client)
    assert client.runs.get("t", "r") == run
    client.threads.get_state.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_unavailable_checkpoint_preserves_error_status(*, asynchronous: bool):
    client = client_for(asynchronous=asynchronous)
    client.threads.get_state.side_effect = ConnectionError("offline")
    bind_run_errors(client)
    result = client.runs.get("t", "r")
    if asynchronous:
        result = await result
    assert result == {"status": "error"}


@pytest.mark.parametrize("metadata", [{}, {"run_id": "a different run"}])
def test_does_not_misattribute_another_runs_checkpoint(metadata: dict):
    client = client_for(state={"metadata": metadata, "tasks": [{"error": "old failure"}]})
    bind_run_errors(client)
    assert client.runs.get("t", "r") == {"status": "error"}


def test_errors_are_bounded_and_secrets_redacted(monkeypatch: pytest.MonkeyPatch):
    secret = "my-private-provider-credential"  # noqa: S105 - synthetic credential
    monkeypatch.setenv("TEST_API_KEY", secret)
    client = client_for(
        state={
            "metadata": {"run_id": "r"},
            "tasks": [
                {"error": secret + " sk-" + "a" * 32 + " " + "x" * 6000},
            ],
        }
    )
    bind_run_errors(client)
    error = client.runs.get("t", "r")["error"]
    assert secret not in error
    assert "sk-" not in error
    assert len(error) == 4000


async def test_cancellation_propagates():
    import asyncio

    client = client_for(asynchronous=True)
    client.threads.get_state.side_effect = asyncio.CancelledError
    bind_run_errors(client)
    with pytest.raises(asyncio.CancelledError):
        await client.runs.get("t", "r")


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_installed_factory_and_check_tool_report_the_failure(
    monkeypatch: pytest.MonkeyPatch, *, asynchronous: bool
):
    import json

    from deepagents.middleware import async_subagents

    from novacode_cli.agents import async_context

    client = client_for(asynchronous=asynchronous)
    factory_name = "get_client" if asynchronous else "get_sync_client"
    monkeypatch.setattr(async_subagents, factory_name, lambda **_kwargs: client)
    # Restore the factory after install_async_model_context replaces it, too.
    other_name = "get_sync_client" if asynchronous else "get_client"
    monkeypatch.setattr(async_subagents, other_name, getattr(async_subagents, other_name))
    monkeypatch.setattr(async_context, "_PATCHED", False)
    async_context.install_async_model_context()
    clients = async_subagents._ClientCache(
        {
            "scout": {"name": "scout", "graph_id": "scout", "url": "http://unused"},
        }
    )
    tool = async_subagents._build_check_tool(clients)
    task: dict[str, Any] = {
        "task_id": "t",
        "thread_id": "t",
        "run_id": "r",
        "agent_name": "scout",
        "status": "running",
        "created_at": "then",
        "last_checked_at": "then",
        "last_updated_at": "then",
    }
    runtime = SimpleNamespace(state={"async_tasks": {"t": task}}, tool_call_id="check-1")
    command = await tool.coroutine("t", runtime) if asynchronous else tool.func("t", runtime)
    message = command.update["messages"][0]
    assert message.tool_call_id == "check-1"
    assert "403" in json.loads(message.content)["error"]
    assert "PRIVATE CHILD" not in message.content
    assert command.update["async_tasks"]["t"]["status"] == "error"
    assert task["status"] == "running"
