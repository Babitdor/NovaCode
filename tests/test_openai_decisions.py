import httpx
import pytest

from novacode_cli.agents.openai_decisions import OpenAIDecisionsClient


def client_for(answers, calls):
    def respond(request):
        import json

        calls.append((request, json.loads(request.content)))
        return httpx.Response(200, json={"answers": answers})

    return OpenAIDecisionsClient(
        api_key="dummy", client=httpx.Client(transport=httpx.MockTransport(respond))
    )


def test_keep_predicates_are_batched_and_normalized():
    calls = []
    client = client_for([{"type": "predicate", "name": "result_t1", "probability": 0.8}], calls)
    assert client.ask(
        {"messages": []}, {"result_t1": {"type": "noul", "instructions": "Keep output?"}}
    ) == {"result_t1": 0.8}
    request, body = calls[0]
    assert str(request.url) == "https://api.openai.com/v1/decisions"
    assert body["model"] == "gpt-6-luna"
    assert isinstance(body["input"], str)
    assert body["questions"] == [
        {"type": "predicate", "name": "result_t1", "instructions": "Keep output?"}
    ]


def test_route_choices_use_route_ids():
    calls = []
    client = client_for(
        [{"type": "choice", "name": "route", "choice": "coding", "confidence": 0.9}], calls
    )
    assert (
        client.ask_route({"messages": []}, {"coding": "Code", "general": "Other"})["answers"][
            "route"
        ]["choice"]
        == "coding"
    )
    assert calls[0][1]["questions"][0]["choices"] == [
        {"value": "coding", "description": "Code"},
        {"value": "general", "description": "Other"},
    ]


@pytest.mark.parametrize(
    "answer",
    [
        {"type": "refusal", "name": "result_t1"},
        {"type": "predicate", "name": "result_t1", "probability": True},
        {"type": "predicate", "name": "result_t1", "probability": float("nan")},
        {"type": "predicate", "name": "result_t1", "probability": 1.1},
        {"type": "predicate", "name": "unknown", "probability": 0.1},
    ],
)
def test_invalid_verdicts_raise(answer):
    client = client_for([answer], [])
    with pytest.raises(ValueError):
        client.ask({}, {"result_t1": {"type": "noul", "instructions": "Keep?"}})


def test_missing_and_duplicate_answers_raise():
    answer = {"type": "predicate", "name": "a", "probability": 0.2}
    for answers in ([], [answer, answer]):
        with pytest.raises(ValueError):
            client_for(answers, []).ask({}, {"a": {"type": "noul", "instructions": "Keep?"}})


def test_missing_key_does_not_send_request():
    with pytest.raises(ValueError, match="API key"):
        OpenAIDecisionsClient(api_key="").ask({}, {})


def test_factories_isolate_openai_key_and_router_uses_native_choices(monkeypatch):
    from types import SimpleNamespace

    from langchain_core.messages import HumanMessage

    from novacode_cli.agents.model_router import ModelRouter
    from novacode_cli.agents.tool_verdicts import create_system_one_client
    from novacode_cli.config.nova_config import NovaConfig

    requested = []

    def key(name):
        requested.append(name)
        return "dummy"

    monkeypatch.setattr("novacode_cli.config.credentials.credential_value", key)
    cfg = SimpleNamespace(
        get_tool_verdict_endpoint=lambda: NovaConfig.TOOL_VERDICT_OPENAI_ENDPOINT,
        get_tool_verdict_model=lambda: NovaConfig.TOOL_VERDICT_OPENAI_MODEL,
        get_router_decision_endpoint=lambda: NovaConfig.TOOL_VERDICT_OPENAI_ENDPOINT,
        get_router_decision_model=lambda: NovaConfig.TOOL_VERDICT_OPENAI_MODEL,
        get_router_routes=lambda: [{"id": "coding", "criteria": "Code"}],
        get_router_default_route=lambda: "coding",
        get_router_min_confidence=lambda: 0.5,
    )
    assert isinstance(create_system_one_client(cfg), OpenAIDecisionsClient)
    router = ModelRouter(cfg)
    assert isinstance(router._decision_client(), OpenAIDecisionsClient)
    assert requested == ["OPENAI_API_KEY", "OPENAI_API_KEY"]
    router._client = client_for(
        [{"type": "choice", "name": "route", "choice": "coding", "confidence": 0.9}], []
    )
    decision = router.decide([HumanMessage(content="Implement feature")])
    assert decision.route_id == "coding" and not decision.is_fallback
    router._client = client_for(
        [{"type": "choice", "name": "route", "choice": "unknown", "confidence": 0.9}], []
    )
    assert router.decide([HumanMessage(content="Implement feature")]).is_fallback


def test_redirect_is_rejected():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(302, headers={"Location": "https://audit.invalid"})
    )
    client = OpenAIDecisionsClient(api_key="dummy", client=httpx.Client(transport=transport))
    with pytest.raises(ValueError, match="302"):
        client.ask({}, {"a": {"instructions": "Keep?"}})
