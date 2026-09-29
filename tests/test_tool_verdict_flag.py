"""The decision-model pruning flag: off changes nothing, on wires the verdict edit.

The two things that matter here are that the default is genuinely inert (the
reducer Nova runs today is unchanged, object for object) and that turning it on
actually swaps in the verdict-driven edit. Anything else is covered by
``test_tool_verdicts.py`` and ``test_verdict_tool_uses_edit.py``.
"""

from __future__ import annotations

import pytest

from novacode_cli.agents.core_agent import _tool_result_clearing
from novacode_cli.agents.tool_offload import OffloadingToolUsesEdit, VerdictToolUsesEdit
from novacode_cli.agents.tool_verdicts import (
    DEFAULT_KEEP_THRESHOLD,
    ToolVerdictCache,
)
from novacode_cli.config.nova_config import NovaConfig

WINDOW = 200_000


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A NovaConfig whose file lives in tmp, so tests never touch ~/.nova."""
    monkeypatch.setattr(NovaConfig, "_instance", None, raising=False)
    nova = NovaConfig()
    monkeypatch.setattr(nova, "_save", lambda: None, raising=False)
    monkeypatch.setattr(nova, "_load", lambda: None, raising=False)
    nova._config = {}
    return nova


# ── the default is inert ───────────────────────────────────────────────────


def test_the_flag_is_off_by_default(config) -> None:
    """The measurement says it loses, so nothing may switch it on implicitly."""
    assert config.get_tool_verdicts_enabled() is False


def test_flag_off_returns_the_same_edit_as_before(config) -> None:
    """Object for object: the reducer Nova runs today is unchanged."""
    edit = _tool_result_clearing(WINDOW, None)
    assert type(edit) is OffloadingToolUsesEdit
    assert edit.keep == 5
    assert edit.exclude_tools == ["think"]
    assert edit.trigger > 0


# ── turning it on swaps the selection, nothing else ────────────────────────


def test_flag_on_wires_the_verdict_edit(config) -> None:
    edit = _tool_result_clearing(WINDOW, None, ToolVerdictCache(path=None))
    assert type(edit) is VerdictToolUsesEdit
    assert edit.keep_threshold == DEFAULT_KEEP_THRESHOLD


def test_the_verdict_edit_keeps_every_other_setting_identical(config) -> None:
    """The pool, the retention and the trigger must not move with the flag."""
    plain = _tool_result_clearing(WINDOW, None)
    judged = _tool_result_clearing(WINDOW, None, ToolVerdictCache(path=None))
    assert judged.trigger == plain.trigger
    assert judged.keep == plain.keep
    assert judged.exclude_tools == plain.exclude_tools
    assert judged.clear_tool_inputs == plain.clear_tool_inputs
    assert judged.placeholder == plain.placeholder


def test_the_verdict_edit_still_offloads(tmp_path) -> None:
    """Recoverability is not optional: the payload must land on disk.

    ``_tool_result_clearing`` takes an already-resolved directory (the caller
    applies ``cleared_dir``), so the test passes one directly rather than
    wrapping it twice.
    """
    from novacode_cli.agents.tool_offload import cleared_dir

    target = cleared_dir(tmp_path / "agent")
    edit = _tool_result_clearing(WINDOW, target, ToolVerdictCache(path=None))

    assert edit.offload_dir == target
    plain = _tool_result_clearing(WINDOW, target)
    assert plain.offload_dir == target
    assert edit.placeholder == plain.placeholder, "the sentinel must match too"


# ── thresholds do not transfer between models ──────────────────────────────


def test_the_threshold_is_configurable_and_validated(config) -> None:
    assert config.get_tool_verdict_keep_threshold() == DEFAULT_KEEP_THRESHOLD

    config.set_tool_verdict_keep_threshold(0.7)
    assert config.get_tool_verdict_keep_threshold() == 0.7

    # A threshold outside (0, 1) is not a threshold; fall back rather than clear
    # nothing (1.0) or everything (0.0).
    for bad in (0.0, 1.0, -0.5, 2.0):
        config.set_tool_verdict_keep_threshold(bad)
        assert config.get_tool_verdict_keep_threshold() == DEFAULT_KEEP_THRESHOLD


def test_the_endpoint_and_model_default_to_the_separated_4b(config) -> None:
    """tev1:4b, not the 0.8B: the smaller one straddles a 0.5 threshold."""
    assert config.get_tool_verdict_model() == "tev1:4b"
    assert config.get_tool_verdict_endpoint().endswith("/v1/systemone")

    config.set_tool_verdict_model("tev1:0.8b")
    config.set_tool_verdict_endpoint("http://example.test:9/v1/systemone")
    assert config.get_tool_verdict_model() == "tev1:0.8b"
    assert config.get_tool_verdict_endpoint() == "http://example.test:9/v1/systemone"


def test_an_empty_endpoint_or_model_falls_back(config) -> None:
    config.set_tool_verdict_endpoint("   ")
    config.set_tool_verdict_model("")
    assert config.get_tool_verdict_endpoint() == NovaConfig.TOOL_VERDICT_DEFAULT_ENDPOINT
    assert config.get_tool_verdict_model() == NovaConfig.TOOL_VERDICT_DEFAULT_MODEL


# ── the scorer middleware ──────────────────────────────────────────────────


def test_the_middleware_queues_scoring_and_never_blocks_the_call() -> None:
    """A model call must proceed whether or not scoring starts or raises."""
    import asyncio

    from novacode_cli.agents.tool_verdicts import (
        FakeDecisionClient,
        VerdictScorer,
        build_verdict_middleware,
    )

    class Exploding:
        def maybe_score(self, _messages):  # noqa: ANN001
            raise RuntimeError("scorer is broken")

    class Request:
        messages: list = []

    async def run(scorer) -> str:
        middleware = build_verdict_middleware(scorer)

        async def handler(_request):
            return "ran"

        return await middleware.awrap_model_call(Request(), handler)

    # A working scorer: the call still runs.
    working = VerdictScorer(
        FakeDecisionClient(0.1), ToolVerdictCache(path=None), min_interval_seconds=0.0
    )
    assert asyncio.run(run(working)) == "ran"

    # A broken scorer: the call STILL runs. Scoring is never worth a turn.
    assert asyncio.run(run(Exploding())) == "ran"


# ── the switch itself must fail closed ─────────────────────────────────────


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("false", False),
        ("no", False),
        ("0", False),
        ("on", True),
        ("", False),
        ("maybe", False),
        (None, False),
        ([], False),
        ({"enabled": True}, False),
    ],
)
def test_only_an_explicit_yes_enables_the_flag(config, stored, expected) -> None:
    """``bool("false")`` is ``True``, and this switch turns on a measured-worse path.

    A hand-edited value must be able to turn the feature *off*, and anything the
    parser does not recognise has to leave it off rather than on: fail closed, so
    a typo cannot silently start pruning the context.
    """
    config._config["tool_verdicts_enabled"] = stored
    assert config.get_tool_verdicts_enabled() is expected


def test_the_setter_writes_a_real_boolean(config) -> None:
    """A round trip through the config file must not produce a truthy string."""
    config.set_tool_verdicts_enabled(True)
    assert config._config["tool_verdicts_enabled"] is True
    config.set_tool_verdicts_enabled(False)
    assert config._config["tool_verdicts_enabled"] is False
