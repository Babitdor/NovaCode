"""The home banner description the client needs, and its saved settings.

The terminal UI's layouts are Textual widget trees; the socket client draws the
one identity every banner layout shares. These tests pin the shared table, the
style resolution and the payload `ready` carries, so the client's art and the
server's setting cannot drift apart.
"""

from __future__ import annotations

import pytest

from novacode_cli.ui import home

KEYS = {key for _, key in home.HOME_STYLES}


class _Config:
    """A stand-in for `NovaConfig`, holding only the keys under test."""

    def __init__(self, saved: dict[str, object] | None = None) -> None:
        self._saved = saved or {}

    def get(self, key: str, default: object = None) -> object:
        return self._saved.get(key, default)


class _BrokenConfig:
    """A config file that cannot be read."""

    def __init__(self) -> None:
        message = "config unreadable"
        raise OSError(message)

    def get(self, _key: str, default: object = None) -> object:
        return default


def _install(monkeypatch: pytest.MonkeyPatch, saved: dict[str, object] | None) -> None:
    monkeypatch.setattr(
        "novacode_cli.config.nova_config.NovaConfig",
        lambda: _Config(saved),
    )


def test_every_layout_key_is_unique_and_kebab_safe() -> None:
    assert len(KEYS) == len(home.HOME_STYLES) == 14
    assert all(key.islower() and " " not in key for key in KEYS)
    # The three banner layouts are keys the terminal UI also knows.
    assert set(home.BANNER_STYLES) <= KEYS


@pytest.mark.parametrize(
    ("value", "legacy", "expected"),
    [
        ("signal", True, "signal"),
        ("minimal", True, "minimal"),
        ("studio", False, "studio"),
        ("nonsense", True, "dashboard"),
        (None, True, "dashboard"),
        ("", True, "dashboard"),
        (5, True, "dashboard"),
        (None, False, "classic"),
        ("nonsense", False, "classic"),
    ],
)
def test_a_saved_style_resolves_or_falls_back(
    value: object,
    *,
    legacy: bool,
    expected: str,
) -> None:
    assert home.home_style(value, legacy_dashboard=legacy) == expected


@pytest.mark.parametrize(
    ("saved", "env", "expected"),
    [
        (True, None, True),
        (False, "1", False),
        (True, "0", True),
        (None, None, False),
        (None, "1", True),
        (None, "true", True),
        (None, "YES", True),
        (None, "no", False),
    ],
)
def test_animation_follows_the_saved_flag_then_the_env(
    monkeypatch: pytest.MonkeyPatch,
    saved: bool | None,  # noqa: FBT001 — pytest passes parameters positionally
    env: str | None,
    *,
    expected: bool,
) -> None:
    if env is None:
        monkeypatch.delenv("NOVA_ANIMATIONS", raising=False)
    else:
        monkeypatch.setenv("NOVA_ANIMATIONS", env)
    assert home.matrix_rain_enabled(_Config({"matrix_rain_enabled": saved})) is expected


def test_the_payload_carries_the_saved_layout_animation_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, {"home_style": "signal", "matrix_rain_enabled": True})
    assert home.home_payload("1.2.3") == {
        "style": "signal",
        "animation": True,
        "version": "1.2.3",
    }


def test_the_legacy_flag_still_chooses_classic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_ANIMATIONS", raising=False)
    _install(monkeypatch, {"home_dashboard_enabled": False})
    payload = home.home_payload("9.9.9")
    assert payload["style"] == "classic"
    assert payload["animation"] is False


def test_an_unreadable_config_still_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_ANIMATIONS", raising=False)
    monkeypatch.setattr(
        "novacode_cli.config.nova_config.NovaConfig",
        _BrokenConfig,
    )
    # A client without a banner beats a handshake that fails.
    assert home.home_payload("1.0") == {
        "style": "dashboard",
        "animation": False,
        "version": "1.0",
    }
