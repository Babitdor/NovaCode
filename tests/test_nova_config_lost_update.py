"""A stale NovaConfig must not put back a model the user switched away from."""

from __future__ import annotations

from novacode_cli.config.nova_config import NovaConfig


def _at(tmp_path) -> NovaConfig:
    cfg = NovaConfig()
    cfg.config_dir = tmp_path
    cfg.config_path = tmp_path / "Nova.config.json"
    cfg._load()
    return cfg


def test_saving_another_setting_keeps_a_model_switched_elsewhere(tmp_path) -> None:
    first = _at(tmp_path)
    first.set_model_config("nvidia", "nvidia/nemotron-3-super-120b-a12b")

    stale = _at(tmp_path)  # e.g. a long-lived ModelManager, or another Nova process
    _at(tmp_path).set_model_config("ollama", "deepseek-v4.1-flash:cloud")  # user's /model

    stale.set("theme", "flexoki")  # an unrelated save by the stale instance

    fresh = _at(tmp_path)
    assert fresh.get_model_config() == {"provider": "ollama", "model": "deepseek-v4.1-flash:cloud"}
    assert fresh.get("theme") == "flexoki"


def test_a_stale_instance_reads_the_current_model(tmp_path) -> None:
    stale = _at(tmp_path)
    _at(tmp_path).set_model_config("ollama", "deepseek-v4.1-flash:cloud")
    assert stale.get_model_config()["model"] == "deepseek-v4.1-flash:cloud"


def test_deleting_a_key_still_works(tmp_path) -> None:
    cfg = _at(tmp_path)
    cfg.set_model_config("ollama", "x")
    cfg.clear_model_config()
    assert _at(tmp_path).get_model_config() is None
