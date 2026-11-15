"""Offline proof that benchmark assertions reject shortcuts and preserve failed usage."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts.benchmark_code_mode import baseline, fixtures, passed, run, summary


def test_fixture_workspace_boundary_exact_edit_and_assertions(tmp_path):
    tools, _, state, facts = fixtures(tmp_path)
    tools = {t.name: t for t in tools}
    with pytest.raises(ValueError, match="Outside fixture"):
        tools["read_file"].invoke({"file_path": "/../outside.txt"})
    assert not passed("native_discovery", '{"checksum":436}', state, tmp_path, facts)
    tools["native_checksum"].invoke({"value": "nova"})
    assert passed("native_discovery", '{"checksum":436}', state, tmp_path, facts)
    original = (tmp_path / "edit.txt").read_bytes()
    with pytest.raises(ValueError, match="exactly one"):
        tools["edit_file"].invoke(
            {"file_path": "/edit.txt", "old_string": "missing", "new_string": "changed"}
        )
    assert (tmp_path / "edit.txt").read_bytes() == original
    assert not passed("exact_reads", json.dumps(facts), state, tmp_path, facts)


def test_fixture_partial_failure_is_a_model_visible_error_and_large_result_is_one_shot(tmp_path):
    tools, _, state, facts = fixtures(tmp_path)
    tools = {t.name: t for t in tools}
    tools["record_write"].invoke({})
    result = tools["controlled_failure"].func(SimpleNamespace(tool_call_id="failure"))
    assert result.status == "error"
    tools["read_file"].invoke({"file_path": "/writes.txt"})
    assert passed("partial_failure", '{"writes":1}', state, tmp_path, facts)
    assert tools["oversized"].invoke({}).endswith("exact-tail-7f94λ")
    with pytest.raises(ValueError, match="expired"):
        tools["oversized"].invoke({})
    assert not passed("oversized_recovery", '{"tail":"exact-tail-7f94λ"}', state, tmp_path, facts)


def test_failed_usage_is_separate_and_baseline_keeps_original_surface():
    rows = [
        {
            "variant": "current",
            "task": "native_discovery",
            "success": success,
            "input_tokens": tokens,
            "output_tokens": 2,
            "round_trips": 1,
        }
        for success, tokens in [(True, 10), (False, 100)]
    ]
    result = summary(rows)["current"]["native_discovery"]
    assert result["successful"]["input_tokens"] == 10
    assert result["failed"]["input_tokens"] == 100
    old_mode, old_script = baseline()
    assert "tools.search" not in old_mode().tools[0].description
    assert "search:" not in old_script.__globals__["BOOTSTRAP"]


async def test_existing_measurements_cannot_be_overwritten(tmp_path):
    output = tmp_path / "results.json"
    output.write_text("existing measurements", encoding="utf-8")
    with pytest.raises(FileExistsError, match="preserved"):
        await run(["improved"], output)
    assert output.read_text(encoding="utf-8") == "existing measurements"
