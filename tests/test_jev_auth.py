"""Jev credentials and transport boundaries; no real secrets or API calls."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from novacode_cli.agents.tool_verdicts import SystemOneClient, parse_answers
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.config.provider_auth import credential_names, provider_display_name

if TYPE_CHECKING:
    from pathlib import Path

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(NovaConfig, "_load", lambda _self: None)
    monkeypatch.setattr(NovaConfig, "_save", lambda _self: None)
    return NovaConfig()


def test_jev_and_custom_credentials_are_registered():
    assert credential_names()["jev"] == "TYPESAFE_API_KEY"
    assert credential_names()["systemone"] == "SYSTEM_ONE_API_KEY"
    assert "Jev" in provider_display_name("jev")


def test_selecting_jev_activates_the_official_endpoint(config: NovaConfig):
    config.set_tool_verdict_settings(enabled=True, endpoint=JEV_ENDPOINT, model="jev-latest")
    assert config.get_tool_verdicts_enabled() is True
    assert config.get_tool_verdict_endpoint() == JEV_ENDPOINT
    assert config.get_tool_verdict_model() == "jev-latest"
    assert "TYPESAFE_API_KEY" not in config._config


def test_empty_model_does_not_change_settings(config: NovaConfig):
    before = dict(config._config)
    with pytest.raises(ValueError, match="model cannot be empty"):
        config.set_tool_verdict_settings(enabled=True, endpoint=JEV_ENDPOINT, model=" ")
    assert config._config == before


@pytest.mark.parametrize(
    ("endpoint", "model", "expected_key"),
    [
        (JEV_ENDPOINT, "jev-latest", "stored-jev"),
        ("http://localhost:11434/v1/systemone", "kev1", "stored-custom"),
        ("https://api.typesafe.ai.evil.test/v1/systemone", "future-model", "stored-custom"),
        ("https://api.typesafe.ai/other", "future-model", "stored-custom"),
    ],
)
def test_factory_separates_credentials_and_preserves_model(
    config: NovaConfig,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    model: str,
    expected_key: str,
):
    from novacode_cli.agents import tool_verdicts

    monkeypatch.setattr(
        "novacode_cli.config.credentials.credential_value",
        lambda var: {"TYPESAFE_API_KEY": "stored-jev", "SYSTEM_ONE_API_KEY": "stored-custom"}[var],
    )
    config.set_tool_verdict_endpoint(endpoint)
    config.set_tool_verdict_model(model)
    client = tool_verdicts.create_system_one_client(config)
    assert client.endpoint == endpoint
    assert client.model == model
    assert client.api_key == expected_key


def test_missing_jev_key_never_calls_transport(config: NovaConfig, monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.agents import tool_verdicts

    monkeypatch.setattr("novacode_cli.config.credentials.credential_value", lambda _var: "")
    config.set_tool_verdict_endpoint(JEV_ENDPOINT)
    client = tool_verdicts.create_system_one_client(config)
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"answers": {"q": {"noul": 0.9}}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client._client = transport
        with pytest.raises(ValueError, match="/auth"):
            client.ask({}, {"q": {"type": "noul"}})
    assert calls == []


def test_local_endpoint_works_without_a_key(config: NovaConfig, monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.agents import tool_verdicts

    monkeypatch.setattr("novacode_cli.config.credentials.credential_value", lambda _var: "")
    client = tool_verdicts.create_system_one_client(config)
    assert client.api_key == "nova"
    assert client.model == "tev1:4b"


def test_custom_endpoint_does_not_borrow_a_jev_key(
    config: NovaConfig, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.agents import tool_verdicts

    monkeypatch.setattr(
        "novacode_cli.config.credentials.credential_value",
        lambda var: "private-jev-key" if var == "TYPESAFE_API_KEY" else "",
    )
    config.set_tool_verdict_endpoint("https://custom.test/v1/systemone")
    assert tool_verdicts.create_system_one_client(config).api_key == "nova"


def test_jev_request_matches_the_official_contract():
    import json

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == JEV_ENDPOINT
        assert request.headers["authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {
            "model": "jev-latest",
            "state": {"context": "test"},
            "questions": {"keep": {"type": "noul", "instructions": "Keep this result?"}},
        }
        return httpx.Response(200, json={"answers": {"keep": {"type": "noul", "noul": 0.9}}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = SystemOneClient(
            endpoint=JEV_ENDPOINT, model="jev-latest", api_key="test-key", client=transport
        )
        assert client.ask(
            {"context": "test"}, {"keep": {"type": "noul", "instructions": "Keep this result?"}}
        ) == {"keep": 0.9}


def test_http_errors_do_not_echo_secrets():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(401, text="test-secret"))
    ) as transport:
        client = SystemOneClient(api_key="test-secret", client=transport)
        with pytest.raises(ValueError, match="401") as error:
            client.ask({}, {"q": {"type": "noul"}})
        assert "401" in str(error.value)
        assert "test-secret" not in str(error.value)


@pytest.mark.parametrize("probability", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_probabilities_are_rejected(probability: float):
    with pytest.raises(ValueError, match="probability"):
        parse_answers({"answers": {"q": {"noul": probability}}})


def test_probability_parser_invariants():
    import math

    hypothesis = pytest.importorskip("hypothesis")
    strategies = pytest.importorskip("hypothesis.strategies")

    @hypothesis.given(strategies.floats())
    def check(value: float) -> None:
        payload = {"answers": {"q": {"noul": value}}}
        if math.isfinite(value) and 0 <= value <= 1:
            assert parse_answers(payload) == {"q": value}
        else:
            with pytest.raises(ValueError, match="probability"):
                parse_answers(payload)

    check()


def test_model_switch_does_not_reuse_previous_verdicts(config: NovaConfig, tmp_path: Path):
    from langchain_core.messages import ToolMessage

    from novacode_cli.agents.tool_verdicts import ToolVerdictCache, verdict_cache_path

    message = ToolMessage(content="a result", tool_call_id="t1")
    original_path = verdict_cache_path(tmp_path, config)
    original = ToolVerdictCache(path=original_path)
    original.begin()
    original.put(message, 0.1)
    config.set_tool_verdict_settings(enabled=True, endpoint=JEV_ENDPOINT, model="jev-latest")
    new_path = verdict_cache_path(tmp_path, config)
    assert new_path != original_path
    assert ToolVerdictCache(path=new_path).get(message) is None
    assert ToolVerdictCache(path=original_path).get(message) == 0.1
    config.set_tool_verdict_model("future-model")
    assert verdict_cache_path(tmp_path, config) != new_path
    assert verdict_cache_path(None, config) is None
