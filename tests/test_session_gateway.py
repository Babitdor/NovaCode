"""Tools bind session identity through context rather than model arguments."""

import asyncio

from novacode_cli.sessions.gateway import (
    SendMessage,
    endpoint,
    list_sessions,
    read_session_messages,
    reply_session_message,
    request,
    send_session_message,
)


async def test_tools_fail_clearly_outside_connected_session():
    assert "error" in await list_sessions.ainvoke({})
    assert "error" in await SendMessage.ainvoke({"recipient": "b", "content": "Update"})
    assert "sender" not in send_session_message.args
    assert not read_session_messages.args


async def test_concurrent_tool_contexts_do_not_leak_sender_identity():
    async def run(name):
        async def route(method, payload):
            await asyncio.sleep(0)
            return {"sender": name, "method": method, "payload": payload}

        token = endpoint.set(route)
        try:
            return await request("send", {"target": "other", "text": "hello"})
        finally:
            endpoint.reset(token)

    first, second = await asyncio.gather(run("frontend"), run("backend"))
    assert first["sender"] == "frontend"
    assert second["sender"] == "backend"
    assert endpoint.get() is None


async def test_all_tools_forward_only_schema_arguments():
    calls = []

    async def route(method, payload):
        calls.append((method, payload))
        return {"ok": True}

    token = endpoint.set(route)
    try:
        assert await SendMessage.ainvoke({"recipient": "b", "content": "Update"}) == {
            "ok": True
        }
        assert await send_session_message.ainvoke({"to_session": "b", "message": "Q"}) == {
            "ok": True
        }
        assert await read_session_messages.ainvoke({}) == {"ok": True}
        assert await reply_session_message.ainvoke({"request_id": "m1", "message": "A"}) == {
            "ok": True
        }
    finally:
        endpoint.reset(token)
    assert calls == [
        ("send", {"target": "b", "text": "Update"}),
        ("send", {"target": "b", "text": "Q"}),
        ("read", {}),
        ("reply", {"id": "m1", "text": "A"}),
    ]
