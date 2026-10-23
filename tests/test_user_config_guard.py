"""A mis-isolated test must fail before changing the user's saved settings."""

from pathlib import Path

import pytest

from novacode_cli.config.nova_config import NovaConfig


def test_real_config_writes_are_blocked_before_mutation():
    path = Path.home() / ".nova" / "Nova.config.json"
    before = path.read_bytes() if path.exists() else None
    config = NovaConfig()
    # Force the protected destination; no saved fields are printed or changed.
    config.config_path = path
    with pytest.raises(AssertionError, match="real user config"):
        config.set_vision_model_config("test-provider", "test-model")
    assert (path.read_bytes() if path.exists() else None) == before
