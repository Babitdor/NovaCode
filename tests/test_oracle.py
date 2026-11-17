"""Oracle completion requires independent, current grades or explicit owner acceptance."""

import asyncio
import subprocess
import sys
import tracemalloc
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from novacode_cli import ui_events as ev
from novacode_cli.core import oracle


@pytest.fixture
def workspace(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init")
    (tmp_path / "code.py").write_text("value = 1\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "base")
    return tmp_path


class Model:
    def __init__(self, scores):
        self.scores = iter(scores)
        self.messages = []
        self.on_review = lambda: None

    def with_structured_output(self, schema):
        assert schema is oracle.Review
        return self

    async def ainvoke(self, messages):
        self.messages.append(messages)
        self.on_review()
        await asyncio.sleep(0)
        score = next(self.scores)
        if isinstance(score, Exception):
            raise score
        return {"score": score, "summary": "Reviewed", "findings": ["Check code.py"]}


def state(workspace, scores, **settings):
    return SimpleNamespace(
        thread_id="thread",
        session_id="session",
        workspace_root=str(workspace),
        oracle_settings=oracle.OracleSettings(**settings),
        oracle_enabled=True,
        oracle_run=None,
        _model=Model(scores),
        plan_mode_enabled=False,
    )


def lead(monkeypatch, workspace):
    calls = []

    async def stream(prompt, *args, **kwargs):
        calls.append(prompt)
        (workspace / "code.py").write_text(f"value = {len(calls) + 1}\n")
        yield ev.AssistantMessage(
            text="Implemented and checked", agent_name="Nova", agent_color=None
        )
        yield ev.Done(had_response=True)

    monkeypatch.setattr(oracle, "run_with_goal", stream)
    return calls


async def collect(session, decisions=()):
    events = []
    choices = iter(decisions)
    async for event in oracle.run_with_oracle("Improve code.py", object(), "nova", session):
        events.append(event)
        if isinstance(event, ev.InterruptRequest):
            event.future.set_result(next(choices, {"action": "pause"}))
    return events


async def test_independent_rounds_fix_lowest_score_and_hold_done(workspace, monkeypatch):
    calls = lead(monkeypatch, workspace)
    session = state(workspace, [5.0, 9.0, 9.0, 8.0])
    events = await collect(session)
    run = session.oracle_run
    assert len(calls) == 2
    assert "ORACLE REVIEW FEEDBACK" in calls[1]
    assert run.round == 2 and run.lowest == 8 and run.status == "passed"
    assert sum(isinstance(e, ev.Done) for e in events) == 1
    assert len({r.identity for r in run.reviewers}) == 4
    assert len(session._model.messages) == 4
    assert all(
        len(m) == 2 and isinstance(m[0], SystemMessage) and isinstance(m[1], HumanMessage)
        for m in session._model.messages
    )
    assert "value = 2" in session._model.messages[0][1].content
    assert "value = 3" in session._model.messages[2][1].content


async def test_changes_during_review_force_a_fresh_round_without_extra_lead(workspace, monkeypatch):
    calls = lead(monkeypatch, workspace)
    session = state(workspace, [10.0, 10.0, 9.0, 9.0])
    edits = [True]

    def edit():
        if edits:
            edits.pop()
            (workspace / "external.py").write_text("external = True\n")

    session._model.on_review = edit
    events = await collect(session)
    assert len(calls) == 1
    assert session.oracle_run.round == 2
    assert session.oracle_run.status == "passed"
    assert len(session.oracle_run.reviewers) == 4
    assert sum(isinstance(e, ev.Done) for e in events) == 1


@pytest.mark.parametrize(
    "action,expected,calls_count",
    [
        ("pause", "paused", 1),
        ("accept", "accepted", 1),
        ("fix", "passed", 2),
        ("extend", "passed", 1),
    ],
)
async def test_round_limit_owner_decisions(workspace, monkeypatch, action, expected, calls_count):
    calls = lead(monkeypatch, workspace)
    session = state(workspace, [4.0, 9.0], reviewers=1, max_rounds=1)
    await collect(session, [{"action": action}])
    assert session.oracle_run.status == expected
    assert len(calls) == calls_count


async def test_lower_target_cannot_approve_changed_code(workspace, monkeypatch):
    lead(monkeypatch, workspace)
    session = state(workspace, [7.0, 6.0], reviewers=1, max_rounds=1)
    async for event in oracle.run_with_oracle("task", object(), "nova", session):
        if isinstance(event, ev.InterruptRequest):
            if session.oracle_run.round == 1:
                (workspace / "code.py").write_text("broken = True\n")
                event.future.set_result({"action": "lower", "target": 7.0})
            else:
                event.future.set_result({"action": "pause"})
    assert session.oracle_run.round == 2
    assert session.oracle_run.status == "paused"
    assert session.oracle_run.lowest == 6


async def test_lower_target_accepts_only_current_complete_reviews(workspace, monkeypatch):
    lead(monkeypatch, workspace)
    session = state(workspace, [7.0], reviewers=1, max_rounds=1)
    await collect(session, [{"action": "lower", "target": 7.0}])
    assert session.oracle_run.status == "passed"
    assert session.oracle_run.round == 1


@pytest.mark.parametrize("score", [float("nan"), 11.0, "9", RuntimeError("offline")])
async def test_invalid_or_failed_reviewer_never_passes(workspace, monkeypatch, score):
    lead(monkeypatch, workspace)
    session = state(workspace, [score], reviewers=1)
    await collect(session)
    assert session.oracle_run.status == "paused"
    assert session.oracle_run.lowest is None
    assert session.oracle_run.reviewers[0].error
    assert "Reviewer 1 failed" in session.oracle_run.error


async def test_active_team_prevents_passing(workspace, monkeypatch):
    from unittest.mock import AsyncMock

    lead(monkeypatch, workspace)
    session = state(workspace, [10.0])
    monkeypatch.setattr(oracle, "team_busy", AsyncMock(return_value=True))
    await collect(session)
    assert session.oracle_run.status == "paused"
    assert not session._model.messages


async def test_cancelled_reviews_cancel_all_children(workspace, monkeypatch):
    lead(monkeypatch, workspace)
    session = state(workspace, [])
    started = asyncio.Event()
    cancelled = []

    async def slow(messages):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    session._model.ainvoke = slow
    task = asyncio.create_task(collect(session))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(cancelled) == 2
    assert session.oracle_run.status == "cancelled"
    assert session.oracle_run.decision is None
    assert all(row.status == "cancelled" for row in session.oracle_run.latest)
    assert all(row.finished_at >= row.started_at for row in session.oracle_run.latest)


@pytest.mark.parametrize(
    "error,expected",
    [(None, "complete"), (TimeoutError(), "timed out"), (RuntimeError(), "failed")],
)
async def test_reviewer_lifecycle_records_elapsed_and_terminal_status(error, expected):
    run = oracle.OracleRun(oracle.OracleSettings(reviewers=1), ".", "thread", round=1)
    model = Model([error if error is not None else 9.0])
    await oracle.review_round(run, model, "evidence")
    row = run.latest[0]
    assert row.status == expected
    assert row.finished_at >= row.started_at
    assert row.elapsed == row.finished_at - row.started_at
    assert (run.lowest is not None) == (error is None)


async def test_disabled_oracle_adds_no_review_calls(workspace, monkeypatch):
    lead(monkeypatch, workspace)
    session = state(workspace, [])
    session.oracle_enabled = False
    await collect(session)
    assert session.oracle_run is None
    assert not session._model.messages


def test_snapshot_detects_staged_unstaged_and_untracked_changes(workspace):
    versions = [oracle.snapshot(str(workspace)).revision]
    path = workspace / "code.py"
    path.write_text("value = 2\n")
    versions.append(oracle.snapshot(str(workspace)).revision)
    subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
    assert oracle.snapshot(str(workspace)).revision == versions[-1]
    path.write_text("value = 3\n")
    versions.append(oracle.snapshot(str(workspace)).revision)
    (workspace / "new.py").write_text("new = True\n")
    versions.append(oracle.snapshot(str(workspace)).revision)
    path.unlink()
    versions.append(oracle.snapshot(str(workspace)).revision)
    assert len(set(versions)) == 5


def test_incomplete_evidence_fails_closed_and_keeps_assignments(workspace):
    before = oracle.snapshot(str(workspace))
    path = workspace / "code.py"
    path.write_text("value = 2\n")
    after = oracle.snapshot(str(workspace))
    assert "value = 2" in oracle.review_input(before, after, "task", "answer")
    path.write_text("x" * (oracle.MAX_REVIEW_CHARS + 1))
    with pytest.raises(ValueError, match="too large"):
        oracle.review_input(before, oracle.snapshot(str(workspace)), "task", "answer")


def test_unchanged_preexisting_binary_edits_do_not_block_code_review(workspace):
    image = workspace / "image.png"
    image.write_bytes(b"\x89PNG\0original")
    subprocess.run(["git", "-C", str(workspace), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(workspace), "-c", "user.name=Test", "-c",
         "user.email=test@example.test", "commit", "-m", "asset"],
        check=True, capture_output=True,
    )
    image.write_bytes(b"\x89PNG\0preexisting edit")
    before = oracle.snapshot(str(workspace))
    (workspace / "code.py").write_text("value = 2\n")
    evidence = oracle.review_input(before, oracle.snapshot(str(workspace)), "task", "answer")
    assert "value = 2" in evidence
    assert "Binary files" not in evidence
    assert "not reviewed):\nimage.png" in evidence


@pytest.mark.parametrize("action", ["modify", "delete", "committed_delete", "untracked", "non_utf8"])
def test_task_binary_changes_name_the_file_and_block_review(workspace, action):
    image = workspace / "image.png"
    if action in {"modify", "delete", "committed_delete"}:
        image.write_bytes(b"\x89PNG\0original")
        subprocess.run(["git", "-C", str(workspace), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(workspace), "-c", "user.name=Test", "-c",
             "user.email=test@example.test", "commit", "-m", "asset"],
            check=True, capture_output=True,
        )
    before = oracle.snapshot(str(workspace))
    if action in {"delete", "committed_delete"}:
        image.unlink()
        if action == "committed_delete":
            subprocess.run(
                ["git", "-C", str(workspace), "-c", "user.name=Test", "-c",
                 "user.email=test@example.test", "commit", "-am", "remove asset"],
                check=True, capture_output=True,
            )
    else:
        image.write_bytes(b"\xff\xfe" if action == "non_utf8" else b"\x89PNG\0changed")
    with pytest.raises(ValueError, match=r"binary.*image\.png"):
        oracle.review_input(before, oracle.snapshot(str(workspace)), "task", "answer")


def test_review_of_existing_text_changes_keeps_the_diff(workspace):
    (workspace / "code.py").write_text("value = 2\n")
    before = oracle.snapshot(str(workspace))
    evidence = oracle.review_input(before, before, "Review my existing changes", "Reviewed")
    assert "+value = 2" in evidence


def test_large_git_path_list_never_exceeds_windows_command_limit(monkeypatch, tmp_path):
    paths = [f"src/{index:04d}-" + "x" * 110 + ".py" for index in range(1000)]
    calls = []

    def git(root, *args, **kwargs):
        command = ["git", "-C", str(root), *args]
        units = len(subprocess.list2cmdline(command).encode("utf-16-le")) // 2
        if units >= 32767:
            raise OSError(206, "The filename or extension is too long")
        if "--numstat" in args:
            return b"".join(f"1\t1\t{name}\0".encode() for name in paths)
        selected = list(args[args.index("--") + 1:])
        calls.append(selected)
        return b"".join(f"diff --git a/{name} b/{name}\n+changed\n".encode() for name in selected)

    monkeypatch.setattr(oracle, "_git", git)
    unchanged = oracle.Snapshot(tmp_path, "revision", {}, "a" * 40)
    # Complete evidence remains too large; the platform must not reject the process first.
    with pytest.raises(ValueError, match="64,000"):
        oracle.review_input(unchanged, unchanged, "review existing changes", "done")
    seen = [name for batch in calls for name in batch]
    assert seen == paths[:len(seen)]
    assert 0 < len(seen) < len(paths)


def test_batched_git_diff_preserves_literal_unicode_paths(workspace, monkeypatch):
    before = oracle.snapshot(str(workspace))
    contents = {"a [literal].txt": "alpha", "b ünicode😀.txt": "beta", "c space.txt": "gamma"}
    for name, body in contents.items():
        (workspace / name).write_text(body + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "--all"], check=True, capture_output=True)
    calls = []
    real_git = oracle._git

    def git(root, *args, **kwargs):
        if args[0] == "--literal-pathspecs":
            calls.append(list(args[args.index("--") + 1:]))
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(oracle, "_git", git)
    # Leave room for one quoted filename, including supplementary Unicode characters.
    prefix = ["git", "-C", str(workspace), "--literal-pathspecs", "diff", "--no-ext-diff",
              "--no-textconv", "--no-renames", before.head, "--"]
    units = lambda args: len(subprocess.list2cmdline(args).encode("utf-16-le")) // 2
    monkeypatch.setattr(oracle, "MAX_GIT_COMMAND_UNITS", max(units([*prefix, name]) for name in contents))
    evidence = oracle.review_input(before, oracle.snapshot(str(workspace)), "task", "done")
    assert len(calls) == len(contents)
    assert sorted(name for batch in calls for name in batch) == sorted(contents)
    for body in contents.values():
        assert f"+{body}" in evidence


@pytest.mark.parametrize("oversized", [False, True])
def test_empty_or_individually_oversized_paths_never_launch_git(tmp_path, monkeypatch, oversized):
    monkeypatch.setattr(oracle, "_git", lambda *args: pytest.fail("Git must not be launched"))
    if oversized:
        with pytest.raises(ValueError, match="Git path is too long"):
            oracle._text_diff(tmp_path, "a" * 40, ["x" * oracle.MAX_GIT_COMMAND_UNITS])
    else:
        assert oracle._text_diff(tmp_path, "a" * 40, []) == ""


def test_committing_during_task_does_not_hide_code_from_review(workspace):
    before = oracle.snapshot(str(workspace))
    (workspace / "code.py").write_text("value = 2\n")
    subprocess.run(
        ["git", "-C", str(workspace), "-c", "user.name=Test", "-c",
         "user.email=test@example.test", "commit", "-am", "change code"],
        check=True, capture_output=True,
    )
    evidence = oracle.review_input(before, oracle.snapshot(str(workspace)), "task", "answer")
    assert "+value = 2" in evidence


async def test_task_binary_change_cannot_grant_a_passing_review(workspace, monkeypatch):
    async def stream(*args, **kwargs):
        (workspace / "image.png").write_bytes(b"\x89PNG\0new")
        yield ev.Done(had_response=True)

    monkeypatch.setattr(oracle, "run_with_goal", stream)
    session = state(workspace, [10.0], reviewers=1)
    await collect(session)
    assert session.oracle_run.status == "paused"
    assert "image.png" in session.oracle_run.error
    assert session.oracle_run.lowest is None
    assert not session._model.messages


@pytest.mark.parametrize("change", ["delete", "replace_with_text"])
async def test_untracked_binary_history_blocks_review(workspace, monkeypatch, change):
    asset = workspace / "draft.png"
    asset.write_bytes(b"\x89PNG\0original")

    async def stream(*args, **kwargs):
        if change == "delete":
            asset.unlink()
        else:
            asset.write_text("now plain text\n")
        yield ev.Done(had_response=True)

    monkeypatch.setattr(oracle, "run_with_goal", stream)
    session = state(workspace, [9.0], reviewers=1)
    await collect(session)
    assert session.oracle_run.status == "paused"
    assert "draft.png" in session.oracle_run.error
    assert session.oracle_run.lowest is None
    assert not session._model.messages


def test_snapshot_classifies_binary_without_splitting_utf8_characters(workspace):
    (workspace / "unicode.txt").write_bytes(b"a" * (1024 * 1024 - 1) + "😀".encode())
    (workspace / "incomplete.txt").write_bytes(b"a" * (1024 * 1024 - 1) + b"\xf0\x9f")
    (workspace / "nul.txt").write_bytes(b"abc\0def")
    snapshot = oracle.snapshot(str(workspace))
    assert snapshot.binary_files == {"incomplete.txt", "nul.txt"}


def test_large_existing_patch_is_rejected_with_bounded_memory(workspace):
    path = workspace / "code.py"
    path.write_text("old line\n" * 200_000)
    subprocess.run(["git", "-C", str(workspace), "add", "--all"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(workspace), "-c", "user.name=Test", "-c",
         "user.email=test@example.test", "commit", "-m", "large fixture"],
        check=True, capture_output=True,
    )
    path.write_text("new line\n" * 200_000)
    before = oracle.snapshot(str(workspace))
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="64,000"):
            oracle.review_input(before, before, "Review existing changes", "done")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1_000_000, f"Git output accumulated {peak:,} bytes before rejection"


def test_review_budget_is_shared_across_git_batches(workspace, monkeypatch):
    prefix = ["git", "-C", str(workspace), "--literal-pathspecs", "diff", "--no-ext-diff",
              "--no-textconv", "--no-renames", "a" * 40, "--", "a.txt"]
    monkeypatch.setattr(oracle, "MAX_GIT_COMMAND_UNITS", len(subprocess.list2cmdline(prefix)))
    budgets = []

    def git(root, *args, max_output_chars=None):
        budgets.append(max_output_chars)
        return b"x" * (60_000 if len(budgets) == 1 else 10_000)

    monkeypatch.setattr(oracle, "_git", git)
    with pytest.raises(ValueError, match="64,000"):
        oracle._text_diff(workspace, "a" * 40, ["a.txt", "b.txt", "c.txt"])
    assert budgets == [64_000, 4_000]


@pytest.mark.parametrize("kind", ["timeout", "stdout", "stderr"])
def test_bounded_git_reaps_stalled_or_noisy_processes(workspace, monkeypatch, kind):
    original_popen = subprocess.Popen
    processes = []
    output = (
        f"sys.{kind}.buffer.write(b'x' * 1_000_000);sys.{kind}.flush();"
        if kind != "timeout" else ""
    )

    def popen(command, **kwargs):
        if command[0] == "git":
            command = [sys.executable, "-c", f"import sys,time;{output}time.sleep(30)"]
            process = original_popen(command, **kwargs)
            processes.append(process)
            return process
        return original_popen(command, **kwargs)

    monkeypatch.setattr(oracle.subprocess, "Popen", popen)
    if kind == "timeout":
        monkeypatch.setattr(oracle, "GIT_TIMEOUT", 0.1)
    try:
        error = subprocess.TimeoutExpired if kind == "timeout" else ValueError
        with pytest.raises(error):
            oracle._git(workspace, "diff", max_output_chars=1_000)
        assert len(processes) == 1
        assert processes[0].poll() is not None
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()


async def test_oversized_existing_diff_never_requests_a_grade(workspace, monkeypatch):
    (workspace / "code.py").write_text("x" * 100_000)

    async def stream(*args, **kwargs):
        yield ev.Done(had_response=True)

    monkeypatch.setattr(oracle, "run_with_goal", stream)
    session = state(workspace, [9.0], reviewers=1)
    await collect(session)
    assert session.oracle_run.status == "paused"
    assert "64,000" in session.oracle_run.error
    assert session.oracle_run.lowest is None
    assert not session._model.messages


def test_file_body_budget_stops_before_reading_remaining_files(workspace, monkeypatch):
    paths = [workspace / name for name in ("a.py", "b.py", "c.py")]
    for path in paths:
        path.write_text("unchanged\n" * 5000)
    subprocess.run(["git", "-C", str(workspace), "add", "--all"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(workspace), "-c", "user.name=Test", "-c",
         "user.email=test@example.test", "commit", "-m", "body fixtures"],
        check=True, capture_output=True,
    )
    before = oracle.snapshot(str(workspace))
    for path in paths:
        path.write_text("changed\n" + "unchanged\n" * 4999)
    after = oracle.snapshot(str(workspace))
    original_open = type(workspace).open
    reads = []

    def open_file(path, *args, **kwargs):
        if path.parent == workspace and args and args[0] == "rb":
            reads.append(path.name)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(type(workspace), "open", open_file)
    with pytest.raises(ValueError, match="64,000"):
        oracle.review_input(before, after, "review", "done")
    assert reads == ["a.py", "b.py"]


@pytest.mark.parametrize(
    "settings",
    [
        {"target": 0.0},
        {"target": float("nan")},
        {"max_rounds": 21},
        {"reviewers": 0},
        {"reviewers": True},
        {"default_enabled": "false"},
    ],
)
def test_settings_validate_at_boundary(settings):
    with pytest.raises(ValueError):
        oracle.OracleSettings(**settings)


async def test_change_after_passing_progress_cannot_reach_done(workspace, monkeypatch):
    lead(monkeypatch, workspace)
    session = state(workspace, [9.0, 5.0], reviewers=1, max_rounds=1)
    edited = False
    async for event in oracle.run_with_oracle("task", object(), "nova", session):
        if isinstance(event, ev.ContextMessage) and event.message == "Oracle passed" and not edited:
            (workspace / "code.py").write_text("new = True\n")
            edited = True
        if isinstance(event, ev.InterruptRequest):
            event.future.set_result({"action": "pause"})
    assert session.oracle_run.status == "paused"


async def test_tui_entry_point_really_runs_oracle(workspace, monkeypatch):
    from novacode_cli import agent_stream

    lead(monkeypatch, workspace)
    monkeypatch.setattr(agent_stream, "_maybe_verifier", lambda: None)
    session = state(workspace, [9.0], reviewers=1)
    events = [e async for e in agent_stream.run_agent_stream("task", object(), "nova", session)]
    assert session.oracle_run.status == "passed"
    assert len(session._model.messages) == 1
    assert sum(isinstance(e, ev.Done) for e in events) == 1


async def test_sandbox_cannot_receive_passing_local_scores(workspace, monkeypatch):
    lead(monkeypatch, workspace)
    session = state(workspace, [10.0], reviewers=1)
    session._sandbox_type = "docker"
    await collect(session)
    assert session.oracle_run.status == "paused"
    assert not session._model.messages


async def test_absolute_round_limit_cannot_be_extended(workspace, monkeypatch):
    calls = lead(monkeypatch, workspace)
    session = state(workspace, [1.0] * oracle.MAX_ROUNDS, reviewers=1, max_rounds=oracle.MAX_ROUNDS)
    await collect(session, [{"action": "extend"}])
    assert session.oracle_run.round == oracle.MAX_ROUNDS
    assert session.oracle_run.status == "paused"
    assert len(calls) == oracle.MAX_ROUNDS


async def test_review_usage_scope_exists_and_does_not_leak(workspace, monkeypatch):
    from novacode_cli.tracking.usage_tree import get_current_tree

    lead(monkeypatch, workspace)
    session = state(workspace, [9.0, 9.0])
    seen = []
    session._model.on_review = lambda: seen.append(get_current_tree())
    assert get_current_tree() is None
    await collect(session)
    assert len(seen) == 2 and seen[0] is not None and seen[0] is seen[1]
    assert get_current_tree() is None


@pytest.mark.parametrize("enabled", [True, False, None])
def test_saved_thread_round_trips_only_toggle_not_scores(tmp_path, enabled):
    from novacode_cli.session.session_persistence import SessionManager

    manager = SessionManager(sessions_dir=tmp_path)
    manager.save_session(
        session_id="session",
        thread_id="thread",
        messages=[HumanMessage("task")],
        assistant_id="nova",
        oracle_enabled=enabled,
    )
    loaded = manager.load_session("session")
    assert loaded.meta.oracle_enabled is enabled
    assert "oracle_run" not in loaded.meta.to_dict()
