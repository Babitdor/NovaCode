"""Focused tests for Nova's stacked guardrail settings and helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import novacode_cli.ui_events as ev
from novacode_cli.commands import CommandContext
from novacode_cli.commands.guardrails_handler import handle_guardrails_command
from novacode_cli.core.agent_loop import iterate_agent_events
from novacode_cli.security.guardrails import (
    DEFAULT_PII_TYPES,
    ContentFilterMiddleware,
    Guardrails,
    PIIRedactor,
    blocked_keyword_in,
    build_pii_middleware,
    review_response,
)

if TYPE_CHECKING:
    from pathlib import Path

    from novacode_cli.config.nova_config import NovaConfig


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point NovaConfig at an empty home so tests never read the real config."""
    home = tmp_path / ".nova"
    monkeypatch.setattr("novacode_cli.config.config.HOME_DIR", home)
    return home


def _novaconfig() -> NovaConfig:
    from novacode_cli.config.nova_config import NovaConfig

    return NovaConfig()


def test_defaults_are_off_and_types_are_safe(home: Path) -> None:  # noqa: ARG001 - temp HOME_DIR via fixture
    guardrails = _novaconfig().get_guardrails()

    assert guardrails.pii_enabled is False
    assert guardrails.safety_review_enabled is False
    assert guardrails.blocked_keywords == ()
    assert guardrails.pii_types == DEFAULT_PII_TYPES
    assert guardrails.apply_to_input is True
    assert guardrails.apply_to_output is True
    assert guardrails.apply_to_tool_results is True


def test_hand_edited_config_is_normalized(home: Path) -> None:  # noqa: ARG001 - temp HOME_DIR via fixture
    config = _novaconfig()
    config.set(
        "guardrails",
        {
            "pii": {
                "enabled": "yes",
                "types": {
                    "email": "mask",
                    "credit_card": "nonsense",
                    "unknown": "redact",
                    "ip": False,
                },
                "apply_to_output": "off",
            },
            "blocked_keywords": [
                " Secret Project ",
                "secret project",
                "",
                "x" * 500,
            ],
            "safety_review": {"enabled": "on"},
        },
    )

    guardrails = _novaconfig().get_guardrails()

    assert guardrails.pii_enabled is True
    assert guardrails.pii_types == (("email", "mask"), ("credit_card", "mask"))
    assert guardrails.apply_to_input is True  # default
    assert guardrails.apply_to_output is False
    assert guardrails.apply_to_tool_results is True  # default
    assert guardrails.blocked_keywords == ("Secret Project",)
    assert guardrails.safety_review_enabled is True


def test_pii_middleware_is_stacked_for_each_type() -> None:
    guardrails = Guardrails(
        pii_enabled=True,
        pii_types=(("email", "redact"), ("credit_card", "mask")),
    )

    middleware = build_pii_middleware(guardrails)

    assert [m.pii_type for m in middleware] == ["email", "credit_card"]
    assert [m.strategy for m in middleware] == ["redact", "mask"]
    assert all(m.apply_to_input for m in middleware)
    assert all(m.apply_to_output for m in middleware)
    assert all(m.apply_to_tool_results for m in middleware)


def test_pii_redactor_covers_display_text() -> None:
    redactor = PIIRedactor({"email": "redact", "credit_card": "mask"})

    text = "Mail ada@example.com or use 4111 1111 1111 1111."
    redacted = redactor.redact(text)

    assert "ada@example.com" not in redacted
    assert "[REDACTED_EMAIL]" in redacted
    assert "4111 1111 1111 1111" not in redacted
    assert "1111" in redacted


def test_blocking_type_detects_only_block_rules() -> None:
    redactor = PIIRedactor({"email": "block", "credit_card": "mask"})

    assert redactor.blocking_type("mail ada@example.com") == "email"
    assert redactor.blocking_type("card 4111 1111 1111 1111") is None


def test_content_filter_blocks_requests_and_outputs() -> None:
    middleware = ContentFilterMiddleware(["secret project"])

    request = middleware.before_agent(
        {"messages": [HumanMessage(content="Discuss the Secret Project")]}, None
    )
    assert request is not None
    assert request["jump_to"] == "end"
    assert "blocked" in request["messages"][0].content

    answer = middleware.after_agent(
        {
            "messages": [
                HumanMessage(content="hello"),
                AIMessage(content="Details about secret project", id="ai-1"),
            ]
        },
        None,
    )
    assert answer is not None
    assert len(answer["messages"]) == 2
    assert "withheld" in answer["messages"][-1].content


def test_blocked_keyword_helpers_are_case_insensitive() -> None:
    assert blocked_keyword_in("Talk about SECRET project", ["secret"]) == "secret"
    assert blocked_keyword_in("nothing here", ["secret"]) is None


class _StubModel:
    def __init__(self, content: str | Exception) -> None:
        self.content = content

    async def ainvoke(self, _messages: list) -> AIMessage:
        if isinstance(self.content, Exception):
            raise self.content
        return AIMessage(content=self.content)


async def test_review_response_allows_explicitly() -> None:
    verdict = await review_response(
        request="Write a function",
        response="Here is a function.",
        model=_StubModel('{"allow": true, "reason": "clean"}'),
    )

    assert verdict.allowed is True
    assert verdict.reason == "clean"
    assert verdict.skipped is False


async def test_review_response_blocks_explicitly() -> None:
    verdict = await review_response(
        request="Write a function",
        response="Use the leaked key sk-live-123.",
        model=_StubModel('{"allow": false, "reason": "leaked credential"}'),
    )

    assert verdict.allowed is False
    assert verdict.reason == "leaked credential"


async def test_review_response_fails_open_when_unavailable() -> None:
    verdict = await review_response(
        request="Write a function",
        response="Here is a function.",
        model=_StubModel(RuntimeError("provider down")),
    )

    assert verdict.allowed is True
    assert verdict.skipped is True
    assert "provider down" not in verdict.reason


async def test_review_response_reports_malformed_verdict() -> None:
    verdict = await review_response(
        request="Write a function",
        response="Here is a function.",
        model=_StubModel("I think it is fine."),
    )

    assert verdict.allowed is True
    assert verdict.skipped is True


async def test_command_toggles_pii_and_persists(home: Path) -> None:  # noqa: ARG001 - temp HOME_DIR via fixture
    ctx = CommandContext(
        cmd="guardrails",
        cmd_args="on",
        agent=None,
        token_tracker=None,
        session_state=None,
        assistant_id="nova-agent",
    )

    assert await handle_guardrails_command(ctx) is True

    guardrails = _novaconfig().get_guardrails()
    assert guardrails.pii_enabled is True


async def test_command_manages_keywords_and_types(home: Path) -> None:  # noqa: ARG001 - temp HOME_DIR via fixture
    async def run(args: str) -> None:
        await handle_guardrails_command(
            CommandContext(
                cmd="guardrails",
                cmd_args=args,
                agent=None,
                token_tracker=None,
                session_state=None,
                assistant_id="nova-agent",
            )
        )

    await run("keyword add Secret Project")
    await run("type email hash")

    guardrails = _novaconfig().get_guardrails()
    assert guardrails.blocked_keywords == ("Secret Project",)
    assert dict(guardrails.pii_types)["email"] == "hash"

    await run("keyword remove secret project")
    await run("type remove email")

    guardrails = _novaconfig().get_guardrails()
    assert guardrails.blocked_keywords == ()
    assert "email" not in dict(guardrails.pii_types)


def test_invalid_pii_type_or_strategy_is_rejected() -> None:
    guardrails = Guardrails()

    with pytest.raises(ValueError, match="Unknown PII type"):
        guardrails.with_pii_type("passport", "redact")
    with pytest.raises(ValueError, match="Unknown PII strategy"):
        guardrails.with_pii_type("email", "scramble")


# ── end-to-end through the agent loop ────────────────────────────────────────


class _GuardrailsState:
    def __init__(self) -> None:
        self.values = {"messages": []}


class _GuardrailsSession:
    thread_id = "t1"


async def test_agent_loop_refuses_blocked_term_before_model_call(home: Path) -> None:  # noqa: ARG001 - temp HOME_DIR via fixture
    """A blocked term is refused before the model runs, via the loop preflight."""
    _novaconfig().set_guardrails(Guardrails(blocked_keywords=("secret project",)))

    class RefusingAgent:
        async def aget_state(self, config):  # noqa: ANN202, ANN001, ARG002 - LangGraph state double
            return _GuardrailsState()

        async def astream(self, inp, **kw):  # noqa: ANN202, ANN001, ANN003, ARG002 - never reached
            message = "the model must not run for a blocked request"
            raise AssertionError(message)

        async def aupdate_state(self, **kw):  # noqa: ANN202, ANN003 - LangGraph state double
            pass

    gathered = [
        event
        async for event in iterate_agent_events(
            "Discuss the Secret Project",
            RefusingAgent(),
            "nova-agent",
            _GuardrailsSession(),
        )
    ]

    assert any(isinstance(e, ev.AssistantMessage) for e in gathered)
    refusal = next(e for e in gathered if isinstance(e, ev.AssistantMessage))
    assert "blocked" in refusal.text
    assert not any(isinstance(e, ev.Error) for e in gathered)
    assert any(isinstance(e, ev.Done) and e.had_response for e in gathered)
