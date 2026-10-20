"""Container command and verified-cleanup contracts without a Docker daemon."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from novacode_cli.teams.adapters.container import container_argv, stop_owned_container
from novacode_cli.teams.models import TeamError

if TYPE_CHECKING:
    from pathlib import Path


def test_container_command_is_scoped_bounded_and_contains_no_credentials(tmp_path: Path):
    labels = {"nova.agent": "a", "nova.team": "t", "nova.root": "r", "nova.attempt": "n"}
    profile = {"image": "nova/worker@sha256:" + "a" * 64, "network": "bridge", "user": "1000:1000"}
    command = container_argv(profile, tmp_path, "nova-team-a", labels, ["OPENAI_API_KEY"])
    assert "--pull=never" in command
    assert "--cap-drop=ALL" in command
    assert "--read-only" in command
    assert "--pids-limit=128" in command
    assert "-i" in command
    assert "-t" not in command
    assert str(tmp_path.resolve()) in " ".join(command)
    assert "/var/run/docker.sock" not in " ".join(command)
    assert command[-2:] == ["-m", "novacode_cli.teams.worker"]
    with pytest.raises(TeamError):
        container_argv({**profile, "image": "--privileged"}, tmp_path, "name", labels, [])
    with pytest.raises(TeamError):
        container_argv({**profile, "network": "host"}, tmp_path, "name", labels, [])


async def test_foreign_container_is_never_stopped_and_unknown_is_not_reported_dead():
    labels = {"nova.agent": "ours", "nova.root": "root"}
    runner = AsyncMock(return_value=(0, json.dumps({"nova.agent": "other"}), ""))
    assert not await stop_owned_container("name", labels, runner)
    assert runner.await_count == 1
    runner = AsyncMock(return_value=(1, "", "Docker daemon unavailable"))
    assert not await stop_owned_container("name", labels, runner)


async def test_owned_container_is_stopped_and_verified():
    labels = {"nova.agent": "ours", "nova.root": "root"}
    runner = AsyncMock(
        side_effect=[(0, json.dumps(labels), ""), (0, "stopped", ""), (0, "false", "")]
    )
    assert await stop_owned_container("name", labels, runner)
    assert any("stop" in call.args for call in runner.await_args_list)


async def test_missing_container_is_verified_absent():
    runner = AsyncMock(return_value=(1, "", "Error: No such container: name"))
    assert await stop_owned_container("name", {"nova.agent": "a"}, runner)
