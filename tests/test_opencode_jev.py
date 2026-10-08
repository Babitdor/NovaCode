"""Zen Jev shares only the Zen credential; lookalike endpoints cannot borrow it."""

import json

import httpx
import pytest
from textual.app import App
from textual.widgets import Input, Select, Static, Switch

from novacode_cli.agents.model_router import ModelRouter
from novacode_cli.agents.tool_verdicts import create_system_one_client
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.tui.screens import RouterScreen
from tests.test_decision_settings import isolated, open_decisions, save_and_settle  # noqa: F401

ENDPOINT = "https://opencode.ai/zen/v1/systemone"


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        (ENDPOINT, "zen-key"),
        ("https://opencode.ai.evil.test/zen/v1/systemone", "custom-key"),
        ("https://opencode.ai/other", "custom-key"),
        ("http://opencode.ai/zen/v1/systemone", "custom-key"),
    ],
)
def test_zen_jev_credential_is_scoped_to_exact_endpoint(monkeypatch, endpoint, expected):
    keys = {
        "OPENCODE_ZEN_API_KEY": "zen-key",
        "TYPESAFE_API_KEY": "typesafe-key",
        "SYSTEM_ONE_API_KEY": "custom-key",
    }
    monkeypatch.setattr(
        "novacode_cli.config.credentials.credential_value", lambda name: keys.get(name, "")
    )
    config = NovaConfig()
    config.set_tool_verdict_endpoint(endpoint)
    config.set_router_decision_endpoint(endpoint)
    assert create_system_one_client(config).api_key == expected
    assert ModelRouter(config)._decision_api_key() == expected


def test_zen_jev_post_uses_documented_model_body_and_zen_auth(monkeypatch):
    monkeypatch.setattr(
        "novacode_cli.config.credentials.credential_value",
        lambda name: "zen-key" if name == "OPENCODE_ZEN_API_KEY" else "",
    )
    config = NovaConfig()
    config.set_tool_verdict_settings(enabled=True, endpoint=ENDPOINT, model="jev-1.13-free")
    client = create_system_one_client(config)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"answers": {"retain": {"noul": 0.9}}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client._client = transport
        assert client.ask({"task": "Fix authentication"}, {"retain": {"type": "noul"}}) == {
            "retain": 0.9
        }
    request = calls[0]
    assert str(request.url) == ENDPOINT
    assert request.headers["authorization"] == "Bearer zen-key"
    assert request.headers["user-agent"].startswith("NovaCode/")
    assert b'"model":"jev-1.13-free"' in request.content


@pytest.mark.parametrize(
    "preset,model", [("opencode_zen", "jev-1.13"), ("opencode_zen_free", "jev-1.13-free")]
)
async def test_zen_jev_presets_in_decisions_and_router(preset, model):
    app = App()
    async with app.run_test(size=(110, 44)) as pilot:
        screen, panel = await open_decisions(app, pilot)
        panel.query_one("#decision-preset", Select).value = preset
        await pilot.pause()
        assert panel.query_one("#decision-endpoint", Input).value == ENDPOINT
        assert panel.query_one("#decision-model", Input).value == model
        app.pop_screen()
        router = RouterScreen(NovaConfig())
        app.push_screen(router)
        await pilot.pause()
        router.query_one("#router-preset", Select).value = preset
        await pilot.pause()
        assert router.query_one("#router-endpoint", Input).value == ENDPOINT
        assert router.query_one("#router-decision-model", Input).value == model


@pytest.mark.parametrize("has_zen_key", [True, False])
async def test_zen_decisions_save_requires_zen_key_and_preserves_chat_config(
    monkeypatch, has_zen_key
):
    monkeypatch.setattr(
        "novacode_cli.config.credentials.credential_value",
        lambda name: (
            "zen-key"
            if has_zen_key and name == "OPENCODE_ZEN_API_KEY"
            else "go-key"
            if name == "OPENCODE_API_KEY"
            else ""
        ),
    )
    app = App()
    async with app.run_test(size=(110, 44)) as pilot:
        screen, panel = await open_decisions(app, pilot)
        panel.query_one("#decision-preset", Select).value = "opencode_zen_free"
        await pilot.pause()
        panel.query_one("#decision-enabled", Switch).value = True
        if has_zen_key:
            await save_and_settle(screen, pilot)
            config = NovaConfig()
            assert config.get_tool_verdicts_enabled() is True
            assert config.get_tool_verdict_endpoint() == ENDPOINT
            assert config.get_tool_verdict_model() == "jev-1.13-free"
            assert config.get_model_config() is None
        else:
            await panel.save().wait()
            await pilot.pause()
            assert "OpenCode Zen API key" in str(
                panel.query_one("#decision-status", Static).render()
            )
            assert NovaConfig().get_tool_verdicts_enabled() is False


def test_router_classifies_through_zen_jev_and_selects_configured_route(monkeypatch):
    from langchain_core.messages import HumanMessage

    monkeypatch.setattr(
        "novacode_cli.config.credentials.credential_value",
        lambda name: "zen-key" if name == "OPENCODE_ZEN_API_KEY" else "",
    )
    config = NovaConfig()
    config.set_router_decision_endpoint(ENDPOINT)
    config.set_router_decision_model("jev-1.13-free")
    config.set_router_routes(
        [
            {
                "id": "general",
                "provider": "ollama",
                "model": "local",
                "criteria": "General questions",
            },
            {
                "id": "coding",
                "provider": "opencode_zen",
                "model": "glm-5.3",
                "criteria": "Coding tasks",
            },
        ]
    )
    config.set_router_default_route("general")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "answers": {
                    "route": {
                        "choice": "coding",
                        "confidence": 0.95,
                        "probabilities": {"coding": 0.95, "general": 0.05},
                    }
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(
            "novacode_cli.agents.tool_verdicts.SystemOneClient._session", lambda self: transport
        )
        result = ModelRouter(config).decide([HumanMessage("Fix the bug in auth.py")])
    assert result.route_id == "coding" and not result.is_fallback
    body = json.loads(calls[0].content)
    assert body["model"] == "jev-1.13-free"
    assert body["questions"]["route"]["type"] == "choice"
    assert body["questions"]["route"]["criteria"]["coding"] == "Coding tasks"
    assert calls[0].headers["authorization"] == "Bearer zen-key"
