import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict

from novacode_cli.context.archives import RecoveryArchiveError, RecoveryStore


def test_archive_roundtrip_multimodal_tool_arguments_artifact_ids(tmp_path):
    store = RecoveryStore(tmp_path)
    messages = [
        HumanMessage(
            content=[
                {"type": "text", "text": "Keep image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AQID"}},
            ],
            id="user",
        ),
        AIMessage(
            content="",
            id="assistant",
            tool_calls=[{"id": "call", "name": "tool", "args": {"nested": [1, 2]}}],
        ),
        ToolMessage(
            content="result",
            id="result",
            tool_call_id="call",
            artifact={"raw": b"\x00\xff", "user_dict": {"__nova_binary__": "literal-user-data"}},
        ),
    ]
    path = store.archive("thread", messages, approvals=[{"id": "approval", "value": "review"}])
    manifest, payload = store.inspect(path.stem)
    assert payload["messages"] == messages_to_dict(messages)
    assert payload["approvals"] == [{"id": "approval", "value": "review"}]
    assert manifest["message_count"] == 3
    assert store.list("thread") == [manifest]
    assert store.list("other") == []
    blob = tmp_path / "blobs" / manifest["payload"]
    blob.write_bytes(b"corrupt")
    with pytest.raises(RecoveryArchiveError, match="checksum"):
        store.inspect(path.stem)
    assert store.list()[0]["complete"] is False


def test_failed_manifest_publication_never_lists_partial_archive(tmp_path, monkeypatch):
    import novacode_cli.context.archives as archives

    real = archives.publish

    def fail(path, content):
        if path.parent.name == "manifests":
            raise OSError("failed archive")
        real(path, content)

    monkeypatch.setattr(archives, "publish", fail)
    store = RecoveryStore(tmp_path)
    with pytest.raises(OSError, match="failed archive"):
        store.archive("thread", [HumanMessage("original")])
    assert store.list() == []


def test_legacy_transcripts_remain_visible_as_incomplete_references(tmp_path):
    store = RecoveryStore(tmp_path / "recovery")
    session = tmp_path / "sessions" / "thread"
    history = session / "conversation_history"
    history.mkdir(parents=True)
    manual = session / "pre-compact-20261012T000000Z.jsonl"
    automatic = history / "20261012T000001Z-deadbeef.md"
    manual.write_text('{"content":"old"}\n', encoding="utf-8")
    automatic.write_text("old summary transcript", encoding="utf-8")
    entries = store.list("thread")
    assert {entry["path"] for entry in entries} == {str(manual), str(automatic)}
    assert {entry["source"] for entry in entries} == {"manual", "automatic"}
    assert all(entry["complete"] is False and entry["restorable"] is False for entry in entries)
    assert all("lacks a complete" in entry["error"] for entry in entries)
    assert store.list("other") == []


@pytest.mark.asyncio
async def test_checkpoint_restore_preserves_pending_approval(tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph
    from langgraph.types import Command, interrupt
    from typing_extensions import TypedDict
    from novacode_cli.session.checkpoint_coordinator import CheckpointCoordinator, CoordinatedGraph

    class State(TypedDict):
        approved: bool

    def approve(state):
        return {"approved": interrupt("Review this change")}

    saver = InMemorySaver()
    builder = StateGraph(State)
    builder.add_node("approve", approve)
    builder.set_entry_point("approve")
    graph = CoordinatedGraph(builder.compile(checkpointer=saver), CheckpointCoordinator(saver))
    config = {"configurable": {"thread_id": "original"}}
    await graph.ainvoke({"approved": False}, config)
    original = await saver.aget_tuple(config)
    packed = {
        "checkpoint": saver.serde.dumps_typed(original.checkpoint),
        "pending": saver.serde.dumps_typed(original.pending_writes),
        "metadata": original.metadata,
    }
    store = RecoveryStore(tmp_path)
    path = store.archive("original", [HumanMessage("Approve carefully")], checkpoint=packed)
    thread = await store.restore(path.stem, graph)
    assert thread != "original"
    restored = {"configurable": {"thread_id": thread}}
    snapshot = await graph.aget_state(restored)
    assert snapshot.tasks[0].interrupts[0].value == "Review this change"
    await graph.ainvoke(Command(resume=True), restored)
    assert (await graph.aget_state(restored)).values["approved"] is True
    assert (await graph.aget_state(config)).values["approved"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["text", "json", "stream-json"])
async def test_headless_recovery_uses_selected_output_format(tmp_path, monkeypatch, capsys, fmt):
    from types import SimpleNamespace
    import novacode_cli.context.archives as archives
    from novacode_cli.headless.runner import run_headless

    store = RecoveryStore(tmp_path)
    path = store.archive("thread", [HumanMessage("keep")])
    monkeypatch.setattr(archives, "default_recovery_store", lambda: store)
    state = SimpleNamespace(
        headless_prompt="/context recovery inspect " + path.stem,
        headless_output_format=fmt,
        session_id="session",
        thread_id="thread",
    )
    assert await run_headless(agent=None, assistant_id=None, session_state=state) == 0
    output = capsys.readouterr().out
    if fmt == "text":
        assert json.loads(output)["message_count"] == 1
    else:
        result = json.loads(output)
        assert result["type"] == "result"
        assert result["is_error"] is False
        assert json.loads(result["result"])["message_count"] == 1
    state.headless_prompt = "/context recovery restore invalid"
    assert await run_headless(agent=None, assistant_id=None, session_state=state) == 1
    output = capsys.readouterr().out
    assert "Use a recovery ID" in output
    if fmt != "text":
        assert json.loads(output)["is_error"] is True
        assert json.loads(output)["exit_code"] == 1


def test_manifest_tampering_invalid_blobs_and_legacy_integrity(tmp_path):
    store = RecoveryStore(tmp_path)
    path = store.archive("thread", [HumanMessage("retain")])
    manifest = json.loads(path.read_text())
    manifest["thread"] = "wrong-thread"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RecoveryArchiveError, match="manifest checksum"):
        store.inspect(path.stem)
    assert store.list()[0]["complete"] is False
    manifest["version"] = 1
    manifest.pop("checksum")
    path.write_text(json.dumps(manifest))
    inspected, payload = store.inspect(path.stem)
    assert "manifest checksum unavailable" in inspected["integrity"]
    assert payload["messages"][0]["data"]["content"] == "retain"
    manifest["payload"] = "../escape"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RecoveryArchiveError, match="Invalid blob"):
        store.inspect(path.stem)
    with pytest.raises(RecoveryArchiveError, match="Unsupported archive value"):
        store.archive("thread", [HumanMessage("retain")], state={"unsupported": object()})


def test_archive_rejects_reused_binary_digest_with_changed_bytes(tmp_path):
    store = RecoveryStore(tmp_path)
    messages = [ToolMessage("result", tool_call_id="call", artifact={"raw": b"binary"})]
    path = store.archive("thread", messages)
    manifest, _ = store.inspect(path.stem)
    digest = next(iter(manifest["blobs"]))
    (tmp_path / "blobs" / digest).write_bytes(b"manual overwrite")
    with pytest.raises(RecoveryArchiveError, match="blob checksum conflict"):
        store.archive("thread", messages)


def test_incomplete_manifest_and_wrong_blob_size_are_reported(tmp_path):
    store = RecoveryStore(tmp_path)
    message = ToolMessage("result", tool_call_id="call", artifact={"raw": b"binary"})
    path = store.archive("thread", [message])
    manifest = json.loads(path.read_text(encoding="utf-8"))
    real_digest = next(iter(manifest["blobs"]))
    manifest["version"] = 99
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RecoveryArchiveError, match="Incomplete or unsupported"):
        store.inspect(path.stem)
    manifest["version"] = 1
    manifest["complete"] = False
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RecoveryArchiveError, match="Incomplete or unsupported"):
        store.inspect(path.stem)
    manifest["complete"] = True
    manifest["blobs"] = "not a blob table"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RecoveryArchiveError, match="manifest references"):
        store.inspect(path.stem)
    manifest["blobs"] = {real_digest: 999}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RecoveryArchiveError, match="length mismatch"):
        store.inspect(path.stem)


@pytest.mark.asyncio
async def test_restore_refuses_approval_without_checkpoint_structure(tmp_path):
    store = RecoveryStore(tmp_path)
    path = store.archive("thread", [HumanMessage("approve")], approvals=[{"pending": True}])
    with pytest.raises(RecoveryArchiveError, match="pending approvals"):
        await store.restore(path.stem, object())


@pytest.mark.asyncio
async def test_shared_recovery_commands_restore_new_thread_and_reject_unpacked_approvals(
    tmp_path, monkeypatch
):
    import novacode_cli.context.archives as archives
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import MessagesState, StateGraph
    from novacode_cli.session.checkpoint_coordinator import CheckpointCoordinator, CoordinatedGraph

    store = RecoveryStore(tmp_path)
    monkeypatch.setattr(archives, "default_recovery_store", lambda: store)
    saver = InMemorySaver()
    builder = StateGraph(MessagesState)
    builder.add_node("work", lambda state: state)
    builder.set_entry_point("work")
    graph = CoordinatedGraph(builder.compile(checkpointer=saver), CheckpointCoordinator(saver))
    original = [HumanMessage("original", id="preserved-id")]
    path = store.archive("source", original)
    args = dict(agent=graph, thread="source")
    listing, thread = await archives.handle_recovery_command("list", **args)
    assert json.loads(listing)[0]["id"] == path.stem and thread is None
    text, restored = await archives.handle_recovery_command("restore " + path.stem, **args)
    assert restored != "source" and restored in text
    recovered = await graph.aget_state({"configurable": {"thread_id": restored}})
    assert messages_to_dict(recovered.values["messages"]) == messages_to_dict(original)
    pending = store.archive("source", original, approvals=["requires review"])
    with pytest.raises(RecoveryArchiveError, match="pending approvals"):
        await store.restore(pending.stem, graph)
    with pytest.raises(ValueError, match="Usage"):
        await archives.handle_recovery_command("delete all", **args)
