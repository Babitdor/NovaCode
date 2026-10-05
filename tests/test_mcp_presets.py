"""Preset catalog compatibility and installation configuration checks."""

from copy import deepcopy
from unittest.mock import patch

import pytest

from novacode_cli.mcp.presets import MCP_PRESETS, create_config_from_preset


@pytest.mark.parametrize("name", list(MCP_PRESETS))
def test_preset_installation_resolves_inputs_without_mutating_template(name: str):
    preset = MCP_PRESETS[name]
    before = deepcopy(preset)
    inputs = {preset["setup_key"]: "example-value"} if "setup_key" in preset else {}
    with patch("novacode_cli.mcp.config.shutil.which", return_value="mock-binary"):
        config = create_config_from_preset(name, inputs)
    assert config is not None
    assert config.command is None or "{" not in config.command
    assert all("{" not in arg for arg in config.args)
    assert all("{" not in value for value in config.env.values())
    assert config.description == preset["description"]
    assert preset == before


def test_catalog_keeps_configured_servers_and_excludes_retired_presets():
    assert {"playwright", "serena", "apify", "cua-driver", "perplexity"} <= MCP_PRESETS.keys()
    assert (
        not {
            "github",
        "laya-browser",
            "brave-search",
            "google-drive",
            "postgres",
            "sqlite",
            "fetch",
            "time",
            "memory",
            "filesystem",
            "stripe",
            "everything",
        }
        & MCP_PRESETS.keys()
    )


def test_perplexity_uses_official_package_and_api_key_environment():
    with patch("novacode_cli.mcp.config.shutil.which", return_value="mock-binary"):
        config = create_config_from_preset("perplexity", {"perplexity_api_key": "test-api-key"})
    assert config.command == "npx"
    assert config.args == ["-y", "@perplexity-ai/mcp-server"]
    assert config.env == {"PERPLEXITY_API_KEY": "test-api-key"}


def test_cua_executable_path_with_spaces():
    with patch("novacode_cli.mcp.config.Path.is_file", return_value=True):
        config = create_config_from_preset(
            "cua-driver", {"cua_driver_path": r"C:\Program Files\Cua\cua-driver.exe"}
        )
    assert config.command == r"C:\Program Files\Cua\cua-driver.exe"
    assert config.args == ["mcp"]
