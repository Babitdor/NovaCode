"""Regression coverage for cross-provider sandbox behavior."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_provider_execute_methods_accept_timeout():
    from novacode_cli.integrations.daytona import DaytonaBackend
    from novacode_cli.integrations.modal import ModalBackend
    from novacode_cli.integrations.runloop import RunloopBackend

    modal_sb = MagicMock()
    modal_process = MagicMock()
    modal_process.stdout.read.return_value = "ok"
    modal_process.stderr.read.return_value = ""
    modal_process.returncode = 0
    modal_sb.exec.return_value = modal_process
    modal = ModalBackend(modal_sb)
    modal.execute("true", timeout=7)
    modal_sb.exec.assert_called_once()
    assert modal_sb.exec.call_args.kwargs["timeout"] == 7

    runloop_client = MagicMock()
    runloop_client.devboxes.execute_and_await_completion.return_value = SimpleNamespace(
        stdout="ok", stderr="", exit_status=0
    )
    runloop = RunloopBackend("box", runloop_client)
    runloop.execute("true", timeout=8)
    assert runloop_client.devboxes.execute_and_await_completion.call_args.kwargs["timeout"] == 8

    daytona_sb = MagicMock()
    daytona_sb.process.exec.return_value = SimpleNamespace(result="ok", exit_code=0)
    daytona = DaytonaBackend(daytona_sb)
    daytona.execute("true", timeout=9)
    daytona_sb.process.exec.assert_called_once_with("true", timeout=9)


def test_daytona_download_preserves_provider_error():
    from novacode_cli.integrations.daytona import DaytonaBackend

    sb = MagicMock()
    sb.fs.download_files.return_value = [
        SimpleNamespace(source="/missing", result=None, error="not found")
    ]
    fake_daytona = SimpleNamespace(FileDownloadRequest=lambda **kwargs: SimpleNamespace(**kwargs))
    with patch.dict(sys.modules, {"daytona": fake_daytona}):
        response = DaytonaBackend(sb).download_files(["/missing"])[0]

    assert response.content == b""
    assert response.error == "not found"


def test_daytona_orphan_reclaim_deletes_sandbox():
    from novacode_cli.integrations.sandbox_registry import _terminate_record

    sandbox = MagicMock()
    client = MagicMock()
    client.get.return_value = sandbox
    fake_daytona = SimpleNamespace(
        Daytona=MagicMock(return_value=client),
        DaytonaConfig=MagicMock(return_value=object()),
    )
    record = {"provider": "daytona", "sandbox_id": "orphan-id"}
    with patch.dict(os.environ, {"DAYTONA_API_KEY": "token"}, clear=True):
        with patch.dict(sys.modules, {"daytona": fake_daytona}):
            assert _terminate_record(record) is True

    client.get.assert_called_once_with("orphan-id")
    sandbox.delete.assert_called_once()


def test_docker_execution_uses_container_side_timeout():
    from novacode_cli.integrations.docker import DockerBackend

    container = MagicMock()
    container.id = "123456789abc"
    container.exec_run.return_value = SimpleNamespace(output=b"", exit_code=124)

    response = DockerBackend(container).execute("sleep 30", timeout=0.25)

    assert response.exit_code == 124
    command = container.exec_run.call_args.kwargs.get("cmd")
    if command is None:
        command = container.exec_run.call_args.args[0]
    assert command[:2] == ["bash", "-c"]
    assert "timeout --signal=TERM --kill-after=2s 0.25s" in command[2]
    assert "sleep 30" in command[2]
