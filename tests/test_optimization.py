"""Startup isolation, source freshness and workspace continuation regressions."""

# Fixture payloads and fixed local subprocesses follow the existing test style.
# ruff: noqa: ANN001, ANN002, ANN202, S603, S607

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "arguments", [["--help"], ["--version"], ["mcp", "add", "--help"], ["skills", "list", "--help"]]
)
def test_information_commands_do_not_import_runtime(arguments, tmp_path):
    code = """
import sys
class Guard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('novacode_cli.config', 'novacode_cli.main',
                                'langchain', 'deepagents', 'textual')):
            raise AssertionError('runtime import: ' + fullname)
sys.meta_path.insert(0, Guard())
from novacode_cli.entrypoint import cli_main
cli_main()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, *arguments],
        capture_output=True,
        env=dict(
            os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONIOENCODING="utf-8"
        ),
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout
    assert not (tmp_path / ".nova").exists()


@pytest.fixture
def semantic(monkeypatch):
    from novacode_cli.tools import code_search_tools as module

    module._reset_index()
    built = []

    def build(root):
        index = SimpleNamespace(root=root)
        built.append(index)
        return index

    # Use the real file walker, but avoid embedding downloads and actual index builds.
    import semble

    monkeypatch.setattr(semble.SembleIndex, "from_path", build)
    yield module, built
    module._reset_index()


@pytest.mark.parametrize("change", ["edit", "add", "delete", "rename"])
def test_semantic_refreshes_immediately_after_nested_changes(semantic, tmp_path, change):
    module, built = semantic
    deep = tmp_path / "src" / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    source = deep / "日本語.py"
    source.write_text("def old(): pass", encoding="utf-8")
    initial = module._get_index(tmp_path)
    assert module._get_index(tmp_path) is initial
    if change == "edit":
        source.write_text("def changed(): pass", encoding="utf-8")
    elif change == "add":
        (deep / "added.py").write_text("def added(): pass", encoding="utf-8")
    elif change == "delete":
        source.unlink()
    else:
        source.rename(deep / "renamed.py")
    assert module._get_index(tmp_path) is not initial
    assert len(built) == 2


def test_semantic_respects_ignore_changes_and_workspace_switches(semantic, tmp_path):
    module, built = semantic
    (tmp_path / "main.py").write_text("def main(): pass", encoding="utf-8")
    ignore = tmp_path / ".gitignore"
    ignore.write_text("ignored.py\n", encoding="utf-8")
    initial = module._get_index(tmp_path)
    (tmp_path / "ignored.py").write_text("def ignored(): pass", encoding="utf-8")
    assert module._get_index(tmp_path) is initial
    ignore.write_text("", encoding="utf-8")
    assert module._get_index(tmp_path) is not initial
    other = tmp_path / "other"
    other.mkdir()
    assert module._get_index(other).root == str(other.resolve())
    assert len(built) == 3


def test_semantic_detects_equal_size_edit_with_restored_timestamp(semantic, tmp_path):
    module, _ = semantic
    source = tmp_path / "source.py"
    source.write_text("def old(): pass", encoding="utf-8")
    original_stat = source.stat()
    initial = module._get_index(tmp_path)
    source.write_text("def new(): pass", encoding="utf-8")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert module._get_index(tmp_path) is not initial


def test_semantic_failed_or_racing_refresh_never_returns_stale_index(
    semantic, tmp_path, monkeypatch
):
    module, _ = semantic
    file = tmp_path / "main.py"
    file.write_text("def old(): pass", encoding="utf-8")
    assert module._get_index(tmp_path) is not None
    file.write_text("def changed(): pass", encoding="utf-8")

    def racing_build(root):
        file.write_text("def concurrent_change(): pass", encoding="utf-8")
        return SimpleNamespace(root=root)

    import semble

    monkeypatch.setattr(semble.SembleIndex, "from_path", racing_build)
    assert module._get_index(tmp_path) is None
    assert module._index is None


def test_git_state_is_fresh_and_preserves_unstaged_status(tmp_path):
    from novacode_cli.tracking.workspace_anchoring import scan_workspace

    def git(*arguments):
        return subprocess.run(
            ["git", *arguments], cwd=tmp_path, capture_output=True, check=True, timeout=10
        )

    git("init")
    nested = tmp_path / "nested"
    nested.mkdir()
    source = nested / "file.py"
    source.write_text("original", encoding="utf-8")
    git("add", ".")
    git(
        "-c",
        "user.name=Benchmark",
        "-c",
        "user.email=benchmark@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    assert not scan_workspace(tmp_path)["has_uncommitted_changes"]
    source.write_text("edited", encoding="utf-8")
    assert scan_workspace(tmp_path)["modified_files"] == ["nested/file.py"]
    (nested / "added.py").write_text("new", encoding="utf-8")
    assert "nested/added.py" in scan_workspace(tmp_path)["untracked_files"]
    source.unlink()
    assert "nested/file.py" in scan_workspace(tmp_path)["modified_files"]
    unicode_path = nested / "日本語 file.py"
    unicode_path.write_text("unicode", encoding="utf-8")
    assert "nested/日本語 file.py" in scan_workspace(tmp_path)["untracked_files"]
    git("add", ".")
    git(
        "-c",
        "user.name=Benchmark",
        "-c",
        "user.email=benchmark@example.invalid",
        "commit",
        "-m",
        "second fixture",
    )
    assert not scan_workspace(tmp_path)["has_uncommitted_changes"]
    git("mv", "nested/日本語 file.py", "nested/renamed.py")
    assert (
        "nested/日本語 file.py -> nested/renamed.py" in scan_workspace(tmp_path)["modified_files"]
    )
    assert not (tmp_path / ".nova" / ".git_state_cache.json").exists()


def test_local_metrics_are_opt_in_and_never_log_content(monkeypatch, capsys):
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from novacode_cli.tracking.local_metrics import LocalMetricsCallback

    handler = LocalMetricsCallback()
    monkeypatch.delenv("NOVA_LOCAL_METRICS", raising=False)
    handler.on_llm_start({}, ["secret prompt"], run_id="off")
    assert not handler._runs
    monkeypatch.setenv("NOVA_LOCAL_METRICS", "1")
    handler.on_chat_model_start({"name": "fixture"}, [], run_id="model")
    handler.on_llm_new_token("secret response", run_id="model")
    handler.on_tool_start({"name": "fixture_tool"}, "secret arguments", run_id="tool")
    handler.on_tool_end("secret tool output", run_id="tool")
    response = LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content="secret",
                        usage_metadata={
                            "input_tokens": 10,
                            "output_tokens": 2,
                            "total_tokens": 12,
                            "input_token_details": {"cache_read": 5},
                        },
                    )
                )
            ]
        ]
    )
    handler.on_llm_end(response, run_id="model")
    handler.on_llm_end(response, run_id="model")  # duplicate attachment counts once
    handler.on_llm_start({}, [], run_id="failed")
    handler.on_llm_error(RuntimeError("secret exception"), run_id="failed")
    out, err = capsys.readouterr()
    assert not out
    assert "secret" not in err
    import json

    records = [json.loads(line)["nova_local_metrics"] for line in err.splitlines()]
    assert len(records) == 3
    assert records[1]["first_token_ms"] is not None
    assert records[1]["usage"]["cache_read_tokens"] == 5
    assert records[2]["outcome"] == "error"
    assert not handler._runs


def test_usage_metadata_does_not_accumulate_without_a_tree():
    from novacode_cli.tracking.usage_tree import UsageCallbackHandler

    handler = UsageCallbackHandler()
    handler.on_llm_start({}, [], run_id="no-tree")
    handler.on_llm_end(SimpleNamespace(generations=[]), run_id="no-tree")
    handler.on_llm_start({}, [], run_id="failure")
    handler.on_llm_error(RuntimeError(), run_id="failure")
    assert not handler._model_names


@pytest.mark.asyncio
async def test_real_callback_dispatch_deduplicates_model_observers(monkeypatch, capsys):
    import json

    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.tools import tool

    from novacode_cli.tracking.local_metrics import LocalMetricsCallback

    monkeypatch.setenv("NOVA_LOCAL_METRICS", "1")
    handler = LocalMetricsCallback()
    model = FakeListChatModel(responses=["private answer"], callbacks=[handler])
    await model.ainvoke("private prompt", config={"callbacks": [handler]})

    @tool
    def fixture_tool() -> str:
        """Return deterministic fixture data."""
        return "private tool result"

    await fixture_tool.ainvoke({}, config={"callbacks": [handler]})
    out, err = capsys.readouterr()
    records = [json.loads(line)["nova_local_metrics"] for line in err.splitlines()]
    assert not out
    assert "private" not in err
    assert [record["kind"] for record in records] == ["model", "tool"]
    assert not handler._runs


def test_grep_virtual_paths_keep_unicode_and_drop_outside_matches(tmp_path, monkeypatch):
    import json

    from novacode_cli.backends import filesystem as module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "日本語.py").write_text("needle\nneedle\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text("needle\n", encoding="utf-8")
    records = [
        json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": path},
                    "lines": {"text": "needle\n"},
                    "line_number": number,
                },
            }
        )
        for path, number in [("日本語.py", 1), ("日本語.py", 2), ("../outside.py", 1)]
    ]
    monkeypatch.setattr(module, "_resolve_ripgrep_path", lambda: "rg")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="\n".join(records)),
    )
    backend = module.OptimizedFilesystemBackend(root_dir=str(workspace), virtual_mode=True)
    result = backend.grep("needle", "/")
    assert not result.error
    assert [(match["path"], match["line"]) for match in result.matches] == [
        ("/日本語.py", 1),
        ("/日本語.py", 2),
    ]
