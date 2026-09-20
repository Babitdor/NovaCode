"""OpenCode / OpenRouter key resolution: a key saved to the system keychain
(settings.<provider>_api_key) must reach the model even when it isn't in
os.environ — otherwise the provider silently 401s on every restart."""

from __future__ import annotations

import pytest

from novacode_cli.config import model_create as mc
from novacode_cli.config.config import settings


@pytest.fixture
def _clean_env(monkeypatch):
    # Simulate a fresh session: no provider keys in the environment.
    for var in ("OPENCODE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # Restore the real settings values after the test.
    oc, orr = settings.opencode_api_key, settings.openrouter_api_key
    yield
    settings.opencode_api_key, settings.openrouter_api_key = oc, orr


def test_opencode_key_from_keyring_reaches_model(_clean_env):
    settings.opencode_api_key = "sk-keyring-oc"
    m = mc.create_model_from_config("opencode", "glm-5.3")
    assert m is not None  # not rejected despite empty env
    assert m.openai_api_key.get_secret_value() == "sk-keyring-oc"
    assert "opencode.ai" in str(m.root_client.base_url)


def test_openrouter_key_from_keyring_reaches_model(_clean_env):
    settings.openrouter_api_key = "sk-keyring-or"
    m = mc.create_model_from_config("openrouter", "deepseek/deepseek-chat")
    assert m is not None
    assert m.openai_api_key.get_secret_value() == "sk-keyring-or"


def test_no_key_anywhere_returns_none(_clean_env):
    settings.opencode_api_key = None
    assert mc.create_model_from_config("opencode", "glm-5.3") is None


# ── x-opencode-session ──────────────────────────────────────────────────────
#
# The OpenCode Go gateway REJECTS any request without this header:
#   400 MissingSessionID — "Request is missing x-opencode-session and cannot be
#   routed efficiently"
# It keys routing and prompt caching off the value, so it has to be stable
# across a conversation; a fresh id per request would defeat the cache the
# header exists to enable. Verified against the live gateway: with the header a
# call returns normally, without it reproduces the 400 exactly.


def _session_header(model) -> str | None:
    return dict(model.root_client.default_headers).get("x-opencode-session")


def test_opencode_requests_carry_a_session_header(_clean_env, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-test")
    m = mc.build_chat_model("opencode", "glm-5.3")
    assert _session_header(m), "no x-opencode-session — every call 400s"


def test_the_session_id_is_stable_across_rebuilds(_clean_env, monkeypatch):
    """A per-request id would defeat the prompt caching this header enables."""
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-test")
    first = _session_header(mc.build_chat_model("opencode", "glm-5.3"))
    second = _session_header(mc.build_chat_model("opencode", "glm-5.2"))
    assert first == second, "the id changed across a /model switch"


def test_the_session_id_can_be_pinned_for_a_resumed_session(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-test")
    monkeypatch.setenv("OPENCODE_SESSION_ID", "pinned-abc")
    assert mc.opencode_session_id() == "pinned-abc"
    assert _session_header(mc.build_chat_model("opencode", "glm-5.3")) == "pinned-abc"


def test_a_blank_pin_falls_back_to_the_generated_id(monkeypatch):
    monkeypatch.setenv("OPENCODE_SESSION_ID", "   ")
    assert mc.opencode_session_id().startswith("nova-")


def test_other_openai_compatible_providers_do_not_get_the_header(
    _clean_env, monkeypatch
):
    """OpenRouter and OpenAI must not be sent an OpenCode-specific header."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa")
    assert _session_header(mc.build_chat_model("openrouter", "openai/gpt-4o")) is None
    assert _session_header(mc.build_chat_model("openai", "gpt-4o")) is None


# ── message `name` ──────────────────────────────────────────────────────────
#
# OpenCode Go is a gateway and routes each model to a different upstream. Some
# of those reject a `name` on a message outright:
#   400 invalid_request_error — messages[2]: "name" is not supported by this
#   endpoint
# which kills the turn. Nova never sets `name`; LangChain puts it there from the
# agent/subagent name, so it is traceability metadata, not meaning. Stripping it
# is scoped to this client — `name` is legal for OpenAI itself.


def _payload_messages(model, messages):
    return model._get_request_payload(messages)["messages"]


def test_opencode_strips_the_name_field(_clean_env, monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage

    monkeypatch.setenv("OPENCODE_API_KEY", "sk-test")
    model = mc.build_chat_model("opencode", "glm-5.3")
    sent = _payload_messages(
        model,
        [
            HumanMessage("a"),
            AIMessage("b", name="nova"),
            HumanMessage("c", name="babit"),
        ],
    )
    assert not any("name" in m for m in sent), f"name reached the wire: {sent}"
    # The content and roles must be untouched.
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert [m["content"] for m in sent] == ["a", "b", "c"]


def test_stripping_name_leaves_tool_calls_intact(_clean_env, monkeypatch):
    """tool_call_id is what pairs a result to its call — it must survive."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    monkeypatch.setenv("OPENCODE_API_KEY", "sk-test")
    model = mc.build_chat_model("opencode", "glm-5.3")
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "f", "args": {}, "id": "call_1", "type": "tool_call"}],
    )
    sent = _payload_messages(
        model,
        [HumanMessage("x"), ai, ToolMessage(content="r", tool_call_id="call_1", name="f")],
    )
    assert sent[1]["tool_calls"], "tool calls were lost"
    assert sent[2]["tool_call_id"] == "call_1"


def test_other_providers_keep_name(_clean_env, monkeypatch):
    """The strictness is OpenCode's; OpenAI accepts name and may use it."""
    from langchain_core.messages import HumanMessage

    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    for provider, model_name in (("openai", "gpt-4o"), ("openrouter", "openai/gpt-4o")):
        model = mc.build_chat_model(provider, model_name)
        sent = _payload_messages(model, [HumanMessage("c", name="babit")])
        assert sent[0].get("name") == "babit", f"{provider} lost its name field"
