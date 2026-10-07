"""External transcript ingestion, bounded continuation and durable mode changes."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.message import add_messages

from novacode_cli.session.adapters import HarnessMessage, ImportedSession, read_records
from novacode_cli.session.adapters.claude_code import ClaudeCodeAdapter
from novacode_cli.session.adapters.codex import CodexAdapter
from novacode_cli.session.adapters.harness_native import NativeAdapter
from novacode_cli.session.imported_context import MESSAGE_ID, ImportedContext, estimated_tokens
from novacode_cli.session.session_persistence import SessionManager
from novacode_cli.tui.session_import import (
    ExternalSessionsScreen,
    TranscriptScreen,
    context_budget,
    dispatch_import_command,
    install_context,
    load_import,
    parse_import,
)
from tests.test_tui_sessions import isolated_session_config  # noqa: F401


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def codex_source(path: Path, identity: str = "c-123") -> Path:
    return write_jsonl(
        path,
        [
            {"type": "session_meta", "payload": {"id": identity, "cwd": "/project"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix auth"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Fix auth"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call1",
                    "arguments": '{"cmd":"pytest auth.py"}',
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call1",
                    "output": "2 passed",
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "reasoning", "encrypted_content": "private"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Decided to use a lock. Still unresolved: timeout.",
                        }
                    ],
                },
            },
        ],
    )


def test_codex_tools_provenance_and_event_deduplication(tmp_path: Path):
    path = codex_source(tmp_path / "rollout.jsonl")
    adapter = CodexAdapter(tmp_path)
    source = path.read_bytes()
    session = adapter.load(str(path))
    assert session.session_id == "c-123"
    assert session.cwd == "/project"
    assert [m.role for m in session.messages] == ["user", "assistant", "tool", "assistant"]
    assert session.messages[2].tool_name == "exec_command"
    assert "private" not in str(session.to_dict())
    assert path.read_bytes() == source
    assert adapter.discover()[0].message_count == 4
    assert adapter.load("c-1").session_id == "c-123"


def test_claude_blocks_sidechains_and_export(tmp_path: Path):
    path = write_jsonl(
        tmp_path / "claude.jsonl",
        [
            {
                "sessionId": "a-123",
                "cwd": "/project",
                "uuid": "u",
                "type": "user",
                "message": {"role": "user", "content": "fix auth"},
            },
            {
                "sessionId": "a-123",
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "private"},
                        {
                            "type": "tool_use",
                            "id": "t",
                            "name": "Bash",
                            "input": {"command": "pytest"},
                        },
                        {"type": "text", "text": "I tried a lock"},
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "content": [{"type": "text", "text": "passed"}],
                        }
                    ],
                },
            },
            {"isSidechain": True, "type": "user", "message": {"role": "user", "content": "hidden"}},
        ],
    )
    session = ClaudeCodeAdapter(tmp_path).load(str(path))
    assert len(session.messages) == 4
    assert session.messages[-1].role == "tool"
    assert session.messages[-1].tool_name == "Bash"
    assert "private" not in str(session.to_dict())
    assert "hidden" not in str(session.to_dict())
    export = tmp_path / "export.md"
    export.write_text("# User\nFix auth\n# Assistant\nUse a lock\n", encoding="utf-8")
    assert [m.role for m in ClaudeCodeAdapter(tmp_path).load(str(export)).messages] == [
        "user",
        "assistant",
    ]
    export.write_text("Unrecognized export headings; keep all data", encoding="utf-8")
    assert "keep all data" in ClaudeCodeAdapter(tmp_path).load(str(export)).messages[0].content


def test_incomplete_live_tail_and_malformed_middle(tmp_path: Path):
    path = tmp_path / "live.jsonl"
    path.write_text('{"type":"user"}\n{"incomplete":', encoding="utf-8")
    assert read_records(path) == [{"type": "user"}]
    path.write_text('{"type":"user"}\nbad\n{"type":"assistant"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        read_records(path)


def test_latest_ambiguity_and_windows_path_parsing(tmp_path: Path):
    codex_source(tmp_path / "one.jsonl", "same-one")
    codex_source(tmp_path / "two.jsonl", "same-two")
    adapter = CodexAdapter(tmp_path)
    rows = adapter.discover()
    with pytest.raises(ValueError, match="ambiguous"):
        adapter.load("same")
    loaded = load_import("codex", "--last", registry={"codex": adapter})
    assert loaded.session.session_id == max(rows, key=lambda row: row.updated_at).session_id
    assert parse_import('/import claude "C:\\Project Files\\export.md" --mode full') == (
        "claude",
        "C:\\Project Files\\export.md",
        "full",
    )
    with pytest.raises(ValueError, match="Supply"):
        parse_import("/import codex")


@pytest.mark.parametrize("mode", ["compact", "full", "relevant"])
def test_context_modes_preserve_raw_source_and_restore(tmp_path: Path, mode: str):
    source = CodexAdapter(tmp_path).load(str(codex_source(tmp_path / "rollout.jsonl")))
    context = ImportedContext(source, mode, "auth", "head-123")
    text = context.build()
    assert "Inspect the current workspace" in text
    assert "head-123" in text
    assert "codex:c-123" in text
    assert "tool" in text.lower()
    context.save(tmp_path / "nova", "target")
    restored = ImportedContext.load(tmp_path / "nova", "target")
    assert restored.session.to_dict() == source.to_dict()
    assert restored.mode == mode
    assert "estimates" in restored.describe()


def test_large_context_budget_and_full_refusal():
    source = ImportedSession("claude", "id", None, [HarnessMessage("user", "漢字" * 10000)])
    with pytest.raises(ValueError, match="exceeds"):
        ImportedContext(source, "full").build(1000)
    for mode in ("compact", "relevant"):
        assert estimated_tokens(ImportedContext(source, mode).build(1000)) <= 1000
    assert len(source.messages[0].content) == 20000
    app = SimpleNamespace(
        token_tracker=SimpleNamespace(context_window_size=8000, current_context=6000)
    )
    assert context_budget(app) == 400


def test_native_adapter(tmp_path: Path):
    manager = SessionManager(tmp_path)
    manager.save_session(
        "n",
        "t",
        [
            HumanMessage("native task"),
            AIMessage(
                "", tool_calls=[{"id": "call", "name": "execute", "args": {"cmd": "pytest"}}]
            ),
            ToolMessage("passed", tool_call_id="call"),
        ],
        "nova",
    )
    adapter = NativeAdapter(manager)
    assert adapter.discover()[0].session_id == "n"
    assert adapter.load("n").messages[0].role == "user"
    assert adapter.load("n").messages[1].tool_name == "execute"
    assert adapter.load("n").messages[2].tool_name == "execute"


@pytest.mark.asyncio
async def test_install_and_mode_change_replace_only_reference(tmp_path: Path):
    history = [HumanMessage("Nova request", id="keep")]
    calls = []

    async def update(_config: dict, values: dict, as_node: str) -> None:
        nonlocal history
        assert as_node == "model"
        history = add_messages(history, values["messages"])

    async def save() -> None:
        calls.append("saved")

    async def breakdown() -> None:
        pass

    app = SimpleNamespace(
        agent=SimpleNamespace(aupdate_state=update),
        session_state=SimpleNamespace(thread_id="t", session_id="s"),
        session_manager=SessionManager(tmp_path / "nova"),
        token_tracker=None,
        _save_session=save,
        _log=calls.append,
        _update_context_breakdown=breakdown,
        _refresh_status=lambda: None,
    )
    context = ImportedContext(
        CodexAdapter(tmp_path).load(str(codex_source(tmp_path / "source.jsonl")))
    )
    await install_context(app, context)
    await dispatch_import_command(app, "/context imported full")
    assert len(history) == 2
    assert history[0].id == "keep"
    assert history[1].id == MESSAGE_ID
    assert isinstance(history[1], HumanMessage)
    assert "Mode: full" in history[1].content
    assert ImportedContext.load(app.session_manager.sessions_dir, "s").mode == "full"
    app.session_state.session_id = "cleared-session"
    await dispatch_import_command(app, "/context imported")
    assert "no imported context" in str(calls[-1])


@pytest.mark.asyncio
async def test_external_browser_view_has_no_import_side_effects(tmp_path: Path):
    from tests.test_tui_subagent_panel import _app

    adapter = CodexAdapter(tmp_path)
    path = codex_source(tmp_path / "source.jsonl")
    original = path.read_bytes()
    app = _app()
    updates = []

    async def update(_config: dict, values: dict, as_node: str) -> None:
        assert as_node == "model"
        updates.append(values)

    app.agent.aupdate_state = update
    async with app.run_test(size=(110, 40)) as pilot:
        screen = ExternalSessionsScreen({"codex": adapter})
        await app.push_screen(screen)
        for _ in range(8):
            await pilot.pause()
        assert len(screen.rows) == 1
        await pilot.click("#external-view")
        for _ in range(8):
            await pilot.pause()
        assert isinstance(app.screen, TranscriptScreen)
        assert path.read_bytes() == original
        assert not updates
        await pilot.press("escape")
        await pilot.click("#external-search")
        await pilot.press("z", "z", "z")
        assert not screen.filtered
        await pilot.press("escape")


def test_cli_import_flags_validate_before_startup(monkeypatch: pytest.MonkeyPatch):
    import sys

    from novacode_cli.main import parse_args

    monkeypatch.setattr(sys, "argv", ["nova", "--import", "claude", "--latest"])
    args = parse_args()
    assert args.import_provider == "claude"
    assert args.latest
    for arguments in (
        ["--latest"],
        ["--import", "codex"],
        ["--import", "codex", "--latest", "--continue"],
        ["--import", "codex", "--latest", "--import-session", "id"],
    ):
        monkeypatch.setattr(sys, "argv", ["nova", *arguments])
        with pytest.raises(SystemExit, match="2"):
            parse_args()


def test_installed_adapter_registry_isolates_conflicts_and_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    import novacode_cli.session.adapters as module

    custom = SimpleNamespace(provider="custom", discover=list, load=lambda _id: None)
    conflict = SimpleNamespace(provider="codex", discover=list, load=lambda _id: None)

    def broken() -> None:
        message = "Broken installed adapter"
        raise RuntimeError(message)

    monkeypatch.setattr(
        module,
        "entry_points",
        lambda **_kwargs: [
            SimpleNamespace(name="custom", load=lambda: lambda: custom),
            SimpleNamespace(name="conflict", load=lambda: lambda: conflict),
            SimpleNamespace(name="broken", load=broken),
        ],
    )
    registry = module.adapters()
    assert registry["custom"] is custom
    assert isinstance(registry["codex"], CodexAdapter)
    assert len(registry) == 4


def test_resume_rehydrates_reference_and_corruption_keeps_nova_history(tmp_path: Path):
    from novacode_cli.session.imported_context import restore_imported_reference

    context = ImportedContext(
        ImportedSession("claude", "original", "/old", [HarnessMessage("user", "source task")])
    )
    context.save(tmp_path, "s")
    messages = [HumanMessage("new Nova request", id="keep")]
    restored = restore_imported_reference(messages, tmp_path, "s")
    assert [message.id for message in restored] == ["keep", MESSAGE_ID]
    assert "source task" in restored[-1].content
    assert len(restore_imported_reference(restored, tmp_path, "s")) == 2
    (tmp_path / "s" / "imported-context.json").write_text("bad", encoding="utf-8")
    assert restore_imported_reference(restored, tmp_path, "s") == messages


@pytest.mark.asyncio
async def test_real_slash_import_persists_only_inert_context_and_clear_resets(tmp_path: Path):
    from tests.test_tui_subagent_panel import _app

    app = _app()
    app.session_state.session_id = "import-target"
    app.session_manager = SessionManager(tmp_path / "nova")
    history = [HumanMessage("Original Nova request", id="keep")]

    async def update(_config: dict, values: dict, as_node: str) -> None:
        nonlocal history
        assert as_node == "model"
        history = add_messages(history, values["messages"])

    async def state(_config: dict) -> SimpleNamespace:
        return SimpleNamespace(values={"messages": history})

    app.agent.aupdate_state = update
    app.agent.aget_state = state
    path = codex_source(tmp_path / "source.jsonl")
    async with app.run_test(size=(110, 40)) as pilot:
        await app._run_slash(f'/import codex "{path}"')
        await pilot.pause()
        saved = app.session_manager.load_session("import-target")
        assert [message.id for message in saved.messages] == ["keep", MESSAGE_ID]
        assert all(isinstance(message, HumanMessage) for message in saved.messages)
        assert "exec_command" in saved.messages[-1].content
        await app._run_slash("/context imported relevant auth")
        assert len(history) == 2
        assert "Mode: relevant" in history[-1].content
        await app._run_clear()
        assert (
            ImportedContext.load(app.session_manager.sessions_dir, app.session_state.session_id)
            is None
        )


@pytest.mark.asyncio
async def test_compare_sends_one_bounded_nova_request(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.tui.session_import as module

    def load(provider: str, selector: str) -> ImportedContext:
        return ImportedContext(
            ImportedSession(
                provider,
                selector,
                None,
                [
                    HarnessMessage("user", "Task"),
                    HarnessMessage("tool", "Test passed", tool_name="Bash"),
                ],
            )
        )

    monkeypatch.setattr(module, "load_import", load)
    prompts = []

    async def stream(prompt: str) -> None:
        prompts.append(prompt)

    app = SimpleNamespace(token_tracker=None, _stream_prompt=stream, _log=lambda _text: None)
    await dispatch_import_command(app, "/compare claude:a codex:b")
    assert len(prompts) == 1
    assert "claude:a" in prompts[0]
    assert "codex:b" in prompts[0]
    assert "Bash" in prompts[0]
    assert estimated_tokens(prompts[0]) <= 12000


def test_source_limit_is_enforced_during_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import novacode_cli.session.adapters as module

    monkeypatch.setattr(module, "MAX_SOURCE_BYTES", 20)
    path = tmp_path / "oversized.jsonl"
    path.write_text("x" * 21, encoding="utf-8")
    with pytest.raises(ValueError, match="import limit"):
        read_records(path)
