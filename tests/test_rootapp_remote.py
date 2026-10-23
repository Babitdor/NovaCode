"""Rootapp protocol, sender isolation and session integration without a live bot."""

from __future__ import annotations

import asyncio
import io
import json
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import psutil
import pytest
from rich.console import Console

from novacode_cli.remote.bridge import BridgeConfig, RemoteBridgeManager, RemotePlatform
from novacode_cli.remote.rootapp_bridge import RootappBridge
from novacode_cli.remote.rootapp_setup import ASSETS


@pytest.fixture
def isolated_remote(monkeypatch, tmp_path):
    from novacode_cli.remote import config

    monkeypatch.setattr(config, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "_CONFIG_FILE", tmp_path / "remote.json")
    return config


@pytest.fixture
def bridge(isolated_remote):
    config = BridgeConfig(
        RemotePlatform.ROOTAPP,
        "not-a-real-dev-token",
        "channel-1",
        allowed_user_ids={"user-1"},
        bot_id="bot-1",
    )
    return RootappBridge(config, asyncio.Queue(maxsize=10))


async def authenticate(session, url, bridge):
    socket = await session.ws_connect(url)
    await socket.send_json({"type": "auth", "secret": bridge._secret})
    assert (await socket.receive_json(timeout=2))["type"] == "connected"
    await socket.send_json({"type": "ready"})
    assert await bridge.wait_for_ready(timeout=2)
    return socket


@pytest.mark.parametrize("secret", ["wrong", "snowman-\u2603", None, {"secret": "wrong"}])
async def test_websocket_authentication_and_disconnect_fail_pending(bridge, secret):
    url = await bridge._listen()
    try:
        async with aiohttp.ClientSession() as session:
            bad = await session.ws_connect(url)
            await bad.send_json({"type": "auth", "secret": secret})
            assert (await bad.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
            assert not bridge.is_connected
            socket = await authenticate(session, url, bridge)
            command = asyncio.create_task(bridge._rpc("typing", "channel-1"))
            assert (await socket.receive_json(timeout=2))["method"] == "typing"
            await socket.close()
            with pytest.raises(RuntimeError, match="disconnected"):
                await command
            assert not bridge._pending
    finally:
        await bridge.stop()


async def test_channel_sender_filter_dedup_and_session_reply_routing(bridge):
    bridge._connected = True
    bridge._on_status = AsyncMock()
    event = {"channel_id": "channel-1", "user_id": "user-1", "id": "message-1", "text": "fix this"}
    await bridge._incoming({**event, "channel_id": "another-channel"})
    await bridge._incoming({**event, "user_id": "not-authorized"})
    assert bridge._queue.empty()
    bridge._on_status.assert_awaited_once()
    bridge._owners["answer-1"] = "session-b"
    await bridge._incoming({**event, "parent_ids": ["answer-1"]})
    await bridge._incoming(event)
    assert bridge._queue.qsize() == 1
    message = bridge._queue.get_nowait()
    assert message.platform == RemotePlatform.ROOTAPP
    assert message.sender_id == "user-1"
    assert message.reply_to_owner == "session-b"
    assert message.text == "fix this"
    bridge._config.allowed_user_ids.clear()
    await bridge._incoming({**event, "id": "message-2"})
    assert bridge._queue.empty(), "revoked accounts must stop immediately"


async def test_editors_chunk_edit_delete_questions_and_record_owners(bridge):
    bridge._connected = True
    bridge._rpc = AsyncMock(side_effect=lambda method, channel, **kw: "answer-1")
    target = await bridge.stream_target(sid="session-b")
    await target.reply_fn("x" * 4500)
    assert bridge._rpc.await_count == 3
    assert bridge._owners["answer-1"] == "session-b"
    bridge._rpc.reset_mock()
    await target.answer_edit_fn("first")
    await target.answer_edit_fn("changed", final=True)
    await target.answer_edit_fn("")
    assert [call.args[0] for call in bridge._rpc.await_args_list] == ["create", "edit", "delete"]
    remove = await target.ask_fn("Which option?")
    await remove()
    await target.typing_fn()
    assert bridge._rpc.await_args.args[0] == "typing"
    await bridge.stop()
    with pytest.raises(RuntimeError, match="stopped"):
        await target.answer_edit_fn("later")


async def test_missing_sdk_reports_startup_failure_and_manager_cleans_up(
    monkeypatch, isolated_remote
):
    from novacode_cli.remote import rootapp_bridge

    monkeypatch.setattr(rootapp_bridge, "node_executable", lambda: "node")
    manager = RemoteBridgeManager(asyncio.Queue())
    success, error = await manager.start_rootapp("token", "channel-1", "bot-1")
    assert not success
    assert "setup rootapp" in error
    assert not manager.is_running
    assert not manager._bridges


async def test_runtime_secret_removed_when_stopped(bridge, isolated_remote):
    from novacode_cli.remote.rootapp_setup import sdk_home

    sdk_home().mkdir()
    runtime = await asyncio.to_thread(bridge._prepare_runtime, "ws://127.0.0.1:12345/rootapp")
    assert json.loads((runtime / "connection.json").read_text())["secret"] == bridge._secret
    assert "not-a-real-dev-token" not in (runtime / "connection.json").read_text()
    assert json.loads((runtime / "root-manifest.json").read_text())["id"] == "bot-1"
    await bridge.stop()
    assert not (runtime / "connection.json").exists()


async def test_sdk_diagnostic_does_not_expose_credentials(bridge):
    reader = asyncio.StreamReader()
    reader.feed_data(
        b'UNAUTHENTICATED token="not-a-real-dev-token" Authorization: Bearer sensitive\n'
    )
    reader.feed_eof()
    await bridge._drain_diagnostics(reader)
    assert "Dev Token" in bridge.last_error
    assert "not-a-real-dev-token" not in bridge.last_error
    assert "sensitive" not in bridge.last_error
    assert bridge._ready_event.is_set()


async def test_remote_commands_save_reuse_revoke_and_forget(isolated_remote):
    from novacode_cli.commands.commands import _handle_remote_command

    manager = RemoteBridgeManager(asyncio.Queue())
    manager.start_rootapp = AsyncMock(return_value=(True, ""))
    state = SimpleNamespace(
        _remote_bridge_manager=manager, auto_approve=False, _pre_remote_auto_approve=None
    )
    output = io.StringIO()
    console = Console(file=output, color_system=None)
    command = "start rootapp --token development-secret --bot-id bot-1 --channel channel-1 --user-id user-1"
    assert await _handle_remote_command(command, state, console)
    assert isolated_remote.load_remote_config()["rootapp"]["allowed_user_ids"] == ["user-1"]
    assert "development-secret" not in output.getvalue()
    await _handle_remote_command("start rootapp", state, console)
    manager.start_rootapp.assert_awaited_with("development-secret", "channel-1", "bot-1")
    await _handle_remote_command("allow-user rootapp another-user", state, console)
    await _handle_remote_command("deny-user rootapp user-1", state, console)
    assert isolated_remote.load_remote_config()["rootapp"]["allowed_user_ids"] == ["another-user"]
    await _handle_remote_command("forget rootapp", state, console)
    assert "rootapp" not in isolated_remote.load_remote_config()


async def test_rootapp_command_rejects_unknown_options_without_exposing_token(isolated_remote):
    from novacode_cli.commands.rootapp_remote import handle_rootapp_remote

    manager = SimpleNamespace(start_rootapp=AsyncMock())
    output = io.StringIO()
    await handle_rootapp_remote(
        "start rootapp --token secret --unknown option",
        SimpleNamespace(),
        manager,
        Console(file=output),
    )
    manager.start_rootapp.assert_not_awaited()
    assert "secret" not in output.getvalue()


def test_devhost_ports_are_distinct(bridge):
    ports = bridge._host_ports()
    assert len(ports) == 4
    assert len(set(ports.values())) == 4
    assert all(0 < int(value) < 65536 for value in ports.values())


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is optional")
async def test_cancelling_devhost_stops_its_forked_companion(bridge, monkeypatch, tmp_path):
    """Load the shipped bot entry via an SDK stub and a real fork/IPC channel."""
    from novacode_cli.remote import rootapp_bridge
    from novacode_cli.remote.rootapp_setup import sdk_home

    node = shutil.which("node")
    version = await asyncio.create_subprocess_exec(
        node, "--version", stdout=asyncio.subprocess.PIPE
    )
    out, _ = await version.communicate()
    if int(out.decode().lstrip("v").split(".")[0]) < 22:
        pytest.skip("companion requires Node 22+")
    child = tmp_path / "fake-sdk.cjs"
    child.write_text(
        r"""
      const Module = require('node:module');
      const {EventEmitter} = require('node:events');
      const messages = new EventEmitter();
      const api = {
        rootServer: {community:{channelMessages:messages},lifecycle:{start:async fn=>fn()}},
        ChannelMessageEvent:{ChannelMessageCreated:'created'},MessageType:{System:2}
      };
      const original = Module._load;
      Module._load = function(name, ...rest) {
        if(name === '@rootsdk/server-bot') return api;
        if(name === 'ws') return WebSocket;
        return original.call(this,name,...rest);
      };
      require(process.argv[2]);
    """,
        encoding="utf-8",
    )
    host = tmp_path / "fake-host.cjs"
    host.write_text(
        "const {fork} = require('node:child_process');\n"
        + "const child = fork("
        + json.dumps(str(child))
        + ", [process.cwd()+'/bot.cjs'], {stdio:'ignore'});\n"
        + "require('node:fs').writeFileSync('child-pid', String(child.pid));\n",
        encoding="utf-8",
    )
    sdk_home().mkdir()
    monkeypatch.setattr(rootapp_bridge, "devhost_entry", lambda: host)
    monkeypatch.setattr(rootapp_bridge, "node_executable", lambda: node)
    task = asyncio.create_task(bridge.run())
    try:
        # Includes Windows ACL setup and two real Node process starts. Loaded
        # CI runners can exceed four seconds; production allows 45 seconds.
        assert await bridge.wait_for_ready(timeout=20), bridge.last_error
        pid = int((bridge._runtime / "child-pid").read_text())
        runtime = bridge._runtime
        assert psutil.pid_exists(pid)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not bridge.is_connected
    assert bridge._process.returncode is not None
    assert not (runtime / "connection.json").exists()
    # Windows may retain the exited child's PID briefly after taskkill returns.
    for _ in range(100):
        if not psutil.pid_exists(pid):
            break
        await asyncio.sleep(0.02)
    assert not psutil.pid_exists(pid)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is optional")
async def test_real_node_companion_round_trip_with_fake_sdk(bridge):
    """Exercise shipped JS + real WebSocket + Python runtime; no Root account."""
    node = shutil.which("node")
    version = await asyncio.create_subprocess_exec(
        node, "--version", stdout=asyncio.subprocess.PIPE
    )
    out, _ = await version.communicate()
    if int(out.decode().lstrip("v").split(".")[0]) < 22:
        pytest.skip("companion requires Node 22+")
    url = await bridge._listen()
    script = r"""
      const {EventEmitter} = require('node:events');
      const {startRelay} = require(process.argv[1]);
      const messages = new EventEmitter();
      messages.create = async req => {
        if(req.content === 'fail') throw {errorCode: 'permission_denied'};
        return {id:'answer-1'};
      };
      messages.edit = async req => ({id: req.id});
      messages.delete = async () => {};
      messages.setTypingIndicator = async req => {
        if(req.isTyping !== true) throw {errorCode:'missing_isTyping'};
      };
      startRelay({community:{channelMessages:messages}}, {
        ChannelMessageEvent:{ChannelMessageCreated:'created'},MessageType:{System:2}
      }, {url:process.argv[2],secret:process.argv[3]}, WebSocket);
      setInterval(() => messages.emit('created', {
        id:'incoming',userId:'user-1',channelId:'channel-1',
        messageType:1,messageContent:'/nova fix the layout',parentMessages:[]
      }), 100);
    """
    process = await asyncio.create_subprocess_exec(
        node,
        "-e",
        script,
        str(ASSETS / "transport.cjs"),
        url,
        bridge._secret,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await bridge.wait_for_ready(timeout=3)
        message = await asyncio.wait_for(bridge._queue.get(), 3)
        assert message.text == "fix the layout"
        await message.reply_fn("working")
        await message.typing_fn()
        await message.answer_edit_fn("first")
        await message.answer_edit_fn("edited", final=True)
        await message.answer_edit_fn("")
        with pytest.raises(RuntimeError, match="permission_denied"):
            await message.reply_fn("fail")
        assert not bridge._pending
    finally:
        process.terminate()
        await process.communicate()
        await bridge.stop()
