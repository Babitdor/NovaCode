"""Historical trial queries use SmithDB and remain read-only and paginated."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from langsmith import Client
from langsmith._httpx import httpx

from novacode_cli.tracking.run_queries import find_trial_run_ids

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.mark.parametrize("ids", [[], ["first"], ["first", "second", "third"]])
async def test_trial_queries_resolve_project_preserve_history_and_detect_ambiguity(ids: list[str]):
    consumed = []
    arguments = {}

    async def query(**kwargs: object) -> AsyncIterator[SimpleNamespace]:
        arguments.update(kwargs)
        for run_id in ids:
            consumed.append(run_id)
            yield SimpleNamespace(id=run_id)

    client = SimpleNamespace(
        aread_project=AsyncMock(return_value=SimpleNamespace(id="project-id")),
        runs=SimpleNamespace(query=query),
    )
    matches = await find_trial_run_ids(client, "project-name", 'trial-"quoted"')

    assert matches == ids[:2]
    assert consumed == ids[:2]
    client.aread_project.assert_awaited_once_with(project_name="project-name")
    assert arguments == {
        "project_ids": ["project-id"],
        "filter": 'and(eq(metadata_key, "trial_name"), eq(metadata_value, "trial-\\"quoted\\""))',
        "is_root": True,
        "min_start_time": "1970-01-01T00:00:00Z",
        "selects": ["ID"],
        "page_size": 2,
    }


async def test_query_errors_are_not_reported_as_missing_traces():
    client = SimpleNamespace(aread_project=AsyncMock(side_effect=ConnectionError("offline")))
    with pytest.raises(ConnectionError, match="offline"):
        await find_trial_run_ids(client, "project", "trial")


async def test_installed_sdk_uses_v2_and_follows_cursor_without_network(
    monkeypatch: pytest.MonkeyPatch,
):
    requests = []

    async def send(
        _client: httpx.AsyncClient, request: httpx.Request, **_kwargs: object
    ) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        page = (
            {"items": [{"id": "first"}], "next_cursor": "opaque-cursor"}
            if "cursor" not in body
            else {"items": [{"id": "second"}], "next_cursor": "unused"}
        )
        return httpx.Response(200, json=page, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    client = Client(
        api_key="offline-test",
        api_url="https://smith.test",
        info={"version": "0.19.0"},
        auto_batch_tracing=False,
    )
    try:
        monkeypatch.setattr(
            Client, "aread_project", AsyncMock(return_value=SimpleNamespace(id="project-id"))
        )
        try:
            assert await find_trial_run_ids(client, "project", "trial") == ["first", "second"]
        finally:
            await client._get_langsmith_api().close()
    finally:
        client.close()

    assert len(requests) == 2
    assert all(
        request.method == "POST" and request.url.path == "/api/v2/runs/query"
        for request in requests
    )
    assert json.loads(requests[1].content)["cursor"] == "opaque-cursor"
    assert json.loads(requests[0].content)["min_start_time"] == "1970-01-01T00:00:00Z"
