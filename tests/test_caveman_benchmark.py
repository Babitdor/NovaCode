"""Benchmark gates reject incomplete samples and unsafe generated code."""

from __future__ import annotations

from scripts.benchmark_caveman import check_function, quality, summarize


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
