"""`/nova` over the socket: the sprite payload and the persisted toggle.

The Textual overlay and the client draw the same frames, so these tests pin the
wire value (24 frames, 16x8 cells, palette indexes) and the two things a
screenshot cannot: the choice is written where the Textual UI keeps it, and the
frames travel once instead of on every tick.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from novacode_cli.server import frames
from novacode_cli.ui import avatar
from novacode_cli.ui.avatar_frames import COUNTS
from novacode_cli.ui.port_server import ServerPort


class _Config:
    """A NovaConfig stand-in that records the writes."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values: dict[str, Any] = {"nova_avatar": False} if values is None else values
        self.writes: list[tuple[str, Any]] = []

    def get(self, key: str, default: Any = None) -> Any:  # noqa: ANN401 - mirrors NovaConfig
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:  # noqa: ANN401 - mirrors NovaConfig
        self.writes.append((key, value))
        self.values[key] = value


@pytest.fixture
def isolated_config(monkeypatch: pytest.MonkeyPatch) -> _Config:
    from novacode_cli.config import nova_config

    config = _Config()
    monkeypatch.setattr(nova_config, "NovaConfig", lambda: config)
    return config


def _port() -> ServerPort:
    worker = SimpleNamespace(agent=None, session_state=None, session_manager=None)
    return ServerPort(object(), worker)


@pytest.mark.asyncio
async def test_nova_ships_every_frame_once(isolated_config: _Config):
    result = await _port().run("/nova", None)
    assert result.ok is True
    assert result.action == "avatar"
    assert result.message == "Avatar shown at 16x8."
    payload = result.avatar
    assert payload is not None
    assert payload["width"] == avatar.WIDTH == 16
    assert payload["height"] == avatar.HEIGHT == 8
    assert payload["fps"] == avatar.FPS
    states = [frame["state"] for frame in payload["frames"]]
    assert states == [state for state, count in COUNTS.items() for _ in range(count)]
    assert len(payload["frames"]) == sum(COUNTS.values())
    for frame in payload["frames"]:
        assert len(frame["rows"]) == avatar.HEIGHT
        assert all(len(row) == avatar.WIDTH for row in frame["rows"])
        assert len(frame["cells"]) == avatar.HEIGHT
        assert all(len(row) == avatar.WIDTH for row in frame["cells"])
    foreground: list[str] = payload["foreground"]
    background: list[str] = payload["background"]
    assert len(foreground) == len(background)
    assert foreground[0] == background[0] == ""
    for colour in [*foreground, *background]:
        assert colour == "" or (colour.startswith("#") and len(colour) == 7)
    indexes = {index for frame in payload["frames"] for row in frame["cells"] for index in row}
    assert max(indexes) < len(foreground)
    assert isolated_config.values["nova_avatar"] is True


@pytest.mark.asyncio
async def test_nova_persists_the_choice_where_textual_keeps_it(isolated_config: _Config):
    await _port().run("/nova", None)
    assert isolated_config.writes == [("nova_avatar", True)]
    result = await _port().run("/nova", None)
    assert result.avatar is None
    assert result.message == "Avatar hidden."
    assert isolated_config.writes[-1] == ("nova_avatar", False)


@pytest.mark.asyncio
async def test_nova_keeps_a_stored_avatar_on(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config import nova_config

    monkeypatch.setattr(nova_config, "NovaConfig", lambda: _Config({"nova_avatar": True}))
    result = await _port().run("/nova", None)
    assert result.avatar is None
    assert result.message == "Avatar hidden."


@pytest.mark.asyncio
async def test_nova_reports_a_sprite_it_cannot_build(monkeypatch: pytest.MonkeyPatch):
    def explode() -> dict[str, object]:
        message = "no sheet"
        raise OSError(message)

    monkeypatch.setattr(avatar, "payload", explode)
    result = await _port().run("/nova", None)
    assert result.ok is False
    assert result.message == "Avatar unavailable: no sheet"
    assert result.action == "avatar"
    assert result.avatar is None


def test_the_frame_carries_the_sprite_and_omits_it_when_absent():
    payload = avatar.payload()
    carried = frames.command_result("c1", ok=True, action="avatar", avatar=payload)
    assert carried["avatar"] == payload
    assert "avatar" not in frames.command_result("c1", ok=True, action="avatar")


@pytest.mark.asyncio
async def test_the_sprite_matches_the_textual_renderer():
    from novacode_cli.tui.avatar import render

    payload = avatar.payload()
    first = payload["frames"][0]
    lines = render("idle", 0).plain.split("\n")
    assert first["rows"] == lines
    # A blank cell carries the "transparent" palette index, never a colour.
    blank = first["cells"][0][0] if first["rows"][0][0] == " " else None
    if blank is not None:
        assert blank == 0


def test_the_catalogue_serves_nova():
    from novacode_cli.server.catalog import CATALOG

    spec = CATALOG["nova"]
    assert spec.served is True
