"""Benchmark gates reject incomplete samples and unsafe generated code."""

from __future__ import annotations

from scripts.benchmark_caveman import check_function, check_reuse, prepare, quality, summarize


def test_corrected_function_is_executed_with_independent_defaults():
    assert check_function("""```python
def add_item(item, bucket=None):
    if bucket is None:
        bucket = []
    bucket.append(item)
    return bucket
```""")
    assert not check_function("""```python
def add_item(item, bucket=[]):
    bucket.append(item)
    return bucket
```""")
    assert check_function("""```python
def add_item(item, bucket=None):
    bucket = [] if bucket is None else bucket
    bucket.append(item)
    return bucket
```""")


def test_function_validator_refuses_code_outside_the_tiny_contract():
    assert not check_function("""```python
def add_item(item, bucket=None, *, extra=[]):
    if bucket is None:
        bucket = []
    bucket.append(item)
    return bucket
```""")
    assert not check_function("""```python
def add_item(item, bucket=None):
    import os
    return os.environ
```""")


def test_safety_requires_explicit_uncertainty_and_exact_command():
    assert quality("safety", "Check backups before rm -rf ./data. Recovery is not guaranteed.")
    assert not quality("safety", "rm -rf ./data. Recovery is guaranteed.")


def test_log_report_accepts_formatting_but_preserves_exact_assertion_and_exit_status():
    facts = "tests/parser.py:42\nAssertionError: expected 17, got 18\n"
    assert quality("repeated_logs", facts + "**Exit code:** `1`")
    assert quality("repeated_logs", facts + "| Exit code | `1` |")
    assert not quality("repeated_logs", facts + "Exit code: 2")
    assert not quality("repeated_logs", "tests/parser.py:42\nexpected 17\nExit code: 1")


def test_requested_timeout_keeps_its_value_and_unit():
    assert quality("portuguese", "Pool de conexões. ECONNRESET. Timeout de 30 segundos.")
    assert not quality("portuguese", "Pool de conexões. ECONNRESET. Timeout de 30000 ms.")


def test_truncated_and_incorrect_responses_do_not_establish_savings():
    rows = [
        {
            "case": "c",
            "profile": "style",
            "repeat": 0,
            "level": "off",
            "complete": True,
            "quality_pass": True,
            "usage": {"input_tokens": 10, "output_tokens": 30, "total_tokens": 40},
        },
        {
            "case": "c",
            "profile": "style",
            "repeat": 0,
            "level": "ultra",
            "complete": False,
            "quality_pass": True,
            "usage": {"input_tokens": 15, "output_tokens": 5, "total_tokens": 20},
        },
    ]
    result = summarize(rows)["style:ultra"]
    assert result["valid_pairs"] == 0
    assert result["attempted_pairs"] == 1
    assert result["total_tokens"]["reduction_percent"] is None
    rows[1]["complete"] = True
    rows[1]["quality_pass"] = False
    assert summarize(rows)["style:ultra"]["valid_pairs"] == 0


def test_input_overhead_counts_against_output_savings():
    rows = [
        {
            "case": "c",
            "profile": "style",
            "repeat": 0,
            "level": "off",
            "complete": True,
            "quality_pass": True,
            "usage": {"input_tokens": 10, "output_tokens": 30, "total_tokens": 40},
        },
        {
            "case": "c",
            "profile": "style",
            "repeat": 0,
            "level": "full",
            "complete": True,
            "quality_pass": True,
            "usage": {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
        },
    ]
    result = summarize(rows)["style:full"]
    assert result["output_tokens"]["reduction_percent"] > 0
    assert result["total_tokens"]["reduction_percent"] == -50


def test_reuse_validator_preserves_order_zero_and_input_without_general_execution():
    assert check_reuse("""```python
def unique_nonempty(values):
    return stable_unique([v for v in values if v is not None and v != ''])
```""")
    assert not check_reuse("""```python
def unique_nonempty(values):
    return stable_unique([v for v in values if v])
```""")
    helper_first = """```python
def unique_nonempty(values):
    return [v for v in stable_unique(values) if v is not None and v != '']
```"""
    assert check_reuse(helper_first)
    assert quality("reuse_helper", helper_first)
    assert not quality("reuse_helper", helper_first + "\nSkipped tests. No risks.")
    assert not quality("reuse_helper", helper_first + "\n" + helper_first)
    assert not check_reuse("""```python
def unique_nonempty(values):
    return __import__('os').environ
```""")
    assert not check_reuse("""```python
def unique_nonempty(values):
    return stable_unique([v for v in values for x in values])
```""")


def test_ponytail_pairs_are_separate_from_caveman_and_include_overhead():
    base = {"case": "c", "profile": "style", "repeat": 0, "complete": True, "quality_pass": True}
    rows = [
        {
            **base,
            "mode": "off",
            "level": "off",
            "usage": {"input_tokens": 10, "output_tokens": 30, "total_tokens": 40},
        },
        {
            **base,
            "mode": "caveman",
            "level": "ultra",
            "usage": {"input_tokens": 15, "output_tokens": 10, "total_tokens": 25},
        },
        {
            **base,
            "mode": "ponytail",
            "level": "ultra",
            "usage": {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
        },
    ]
    result = summarize(rows)
    assert result["style:ultra"]["valid_pairs"] == 1
    assert result["style:ultra"]["total_tokens"]["reduction_percent"] == 37.5
    assert result["ponytail:style:ultra"]["valid_pairs"] == 1
    assert result["ponytail:style:ultra"]["total_tokens"]["reduction_percent"] == -50


def test_benchmark_uses_live_middleware_without_writing_preferences():
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.messages import HumanMessage

    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.prompts import render_template

    config = NovaConfig()
    config.set_compression_mode("ponytail")
    config.set_ponytail_level("ultra")
    saved = config.config_path.read_bytes()
    model = FakeListChatModel(responses=["ok"])
    base = render_template("core_agent_system.jinja")
    messages = [HumanMessage("Explain the implementation")]
    with patch.object(NovaConfig, "_save", side_effect=AssertionError("Config write")):
        for mode, level, marker in [
            ("off", "off", None),
            ("caveman", "full", '<nova_caveman_style level="full">'),
            ("caveman", "ultra", '<nova_caveman_style level="ultra">'),
            ("ponytail", "full", "PONYTAIL MODE ACTIVE — level: full"),
            ("ponytail", "ultra", "PONYTAIL MODE ACTIVE — level: ultra"),
        ]:
            request = prepare(model, base, messages, config, level, "style", mode)
            assert request.model is model
            assert request.messages == messages
            if marker:
                assert marker in request.system_message.text
                if mode == "ponytail":
                    assert (
                        "never add a footer outside the requested format"
                        in request.system_message.text
                    )
                    assert "When prose is allowed" in request.system_message.text
                    assert (
                        "End your reply with one or two lines:" not in request.system_message.text
                    )
            else:
                assert "MODE ACTIVE" not in request.system_message.text
                assert "<nova_caveman_style" not in request.system_message.text
    assert config.config_path.read_bytes() == saved
