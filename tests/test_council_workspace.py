"""Session presets never change saved models or the planning council."""

from dataclasses import replace

import pytest

from novacode_cli.council_settings import default_settings
from novacode_cli.council_workspace import PRESETS, apply_web_preset, panel_descriptor


@pytest.mark.parametrize("key", list(PRESETS))
def test_preset_preserves_models_judge_and_planning_seat(key):
    original = default_settings()
    original = replace(
        original,
        members=tuple(
            replace(m, provider="openai", model=f"model-{m.id}") for m in original.members
        ),
    )
    snapshot = original.to_dict()
    modified = apply_web_preset(original, key)
    assert modified is not original
    assert modified.judge is original.judge
    assert modified.members[-1] is original.members[-1]
    assert [(m.provider, m.model) for m in modified.members] == [
        (m.provider, m.model) for m in original.members
    ]
    assert original.to_dict() == snapshot
    panel = panel_descriptor(modified)
    assert len(panel["agents"]) == 5
    assert panel["chairman"]["id"] == "chairman"
    assert all(a["model"].startswith("openai:model-") for a in panel["agents"])
    assert "Consensus engine" == panel["chairman"]["role"]


def test_saved_preset_unchanged_and_unknown_rejected():
    original = default_settings()
    assert apply_web_preset(original, "saved") is original
    with pytest.raises(ValueError, match="Unknown"):
        apply_web_preset(original, "not-a-preset")
