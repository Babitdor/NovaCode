"""Handler for the /ralph command - autonomous looping mode.

UI is emitted through a small ``emit`` callback (a Rich-markup string sink) so
the same handler drives both the classic console REPL and the Textual TUI:

* CLI (default): :func:`_console_emit` renders the markup to the global console.
* TUI: ``tui/app.py`` passes a thread-safe emitter that logs each line as a
  native widget (so foreground headers, the background progress, and
  ``/ralph --status`` all render in the TUI instead of being swallowed or
  printed raw to stdout/stderr).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from rich.markup import escape

from novacode_cli.commands import ralph_events as rev
from novacode_cli.config.config import COLORS, console
from novacode_cli.prime_memory import session_directory
from novacode_cli.prompts import render_template
from novacode_cli.session.session_persistence import atomic_write
from novacode_cli.shell.processes import kill_process_tree, process_group_options
from novacode_cli.states.Session import BackgroundRalphTask, RalphTaskStatus
from novacode_cli.ui.ui_elements import TokenTracker

logger = logging.getLogger(__name__)

# A UI sink: called with one Rich-markup string per line. "" means a blank line.
EmitFn = Callable[[str], None]
# A structured-progress sink (optional). Renderers that want native widgets pass
# one of these; when it is None the handler renders the markup ``emit`` fallback.
EventFn = rev.RalphEventFn


def _console_emit(message: str = "") -> None:
    """Default emitter (CLI): render a Rich-markup string to the console."""
    console.print(message)


def _emit_event(
    on_event: EventFn | None,
    event: rev.RalphEvent,
    fallback: Callable[[], None],
) -> None:
    """Send a structured event to ``on_event`` if present, else run ``fallback``.

    Keeps the handler UI-agnostic: the TUI passes ``on_event`` and renders native
    widgets; the CLI leaves it ``None`` and the markup ``fallback`` runs unchanged.
    """
    if on_event is not None:
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 - a renderer hiccup must not break the run
            logger.debug("ralph on_event renderer failed", exc_info=True)
    else:
        fallback()


# Cross-iteration progress log. The agent reads it at the start of every
# iteration and appends what it implemented/learned at the end, so a Ralph run
# carries memory forward. POSIX path so the model never sees a backslashed form.
RALPH_PROGRESS_PATH = ".nova/ralph/progress.md"


def _ensure_ralph_dir() -> None:
    """Make sure .nova/ralph/ exists so progress.md has a home."""
    try:
        (Path(".nova") / "ralph").mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _read_ralph_progress(path: Path = Path(RALPH_PROGRESS_PATH)) -> str:
    """Return the current progress.md contents (bounded), or '' if absent."""
    p = path
    try:
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace").strip()
            if len(text) > 6000:
                text = "…(earlier progress trimmed)…\n" + text[-6000:]
            return text
    except Exception:
        pass
    return ""


# =============================================================================
# Smart-loop signals: checklist (completion), git fingerprint (stuck), verify
# =============================================================================

#: Structured completion signal. The agent maintains a markdown checklist here;
#: the loop stops early once every item is checked (instead of grinding through
#: all max_iterations). POSIX path so the model never sees a backslashed form.
RALPH_CHECKLIST_PATH = ".nova/ralph/checklist.md"
#: Opt-in verification command (one shell command). When present, the loop runs
#: it after each iteration and won't declare completion until it passes.
RALPH_VERIFY_PATH = ".nova/ralph/verify.txt"
#: Consecutive zero-change iterations tolerated before the loop calls it stuck.
_MAX_NO_CHANGE = 2
#: Max characters of verification output fed back into the next prompt.
_VERIFY_TAIL_CHARS = 1500

_CHECKLIST_RE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s+(.+?)\s*$")


@dataclass
class _ChecklistState:
    """Parsed Ralph checklist — the loop's structured completion signal."""

    items: list[tuple[bool, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def done(self) -> int:
        return sum(1 for checked, _ in self.items if checked)

    @property
    def all_done(self) -> bool:
        """True only when there is at least one item and all are checked."""
        return self.total > 0 and self.done == self.total

    @property
    def summary(self) -> str:
        return f"{self.done}/{self.total} items complete" if self.total else "no checklist yet"

    def remaining(self) -> list[str]:
        return [text for checked, text in self.items if not checked]


def _read_checklist(path: Path = Path(RALPH_CHECKLIST_PATH)) -> _ChecklistState:
    """Parse ``.nova/ralph/checklist.md`` (empty state if absent/unreadable)."""
    p = path
    items: list[tuple[bool, str]] = []
    try:
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                m = _CHECKLIST_RE.match(line)
                if m:
                    items.append((m.group(1).lower() == "x", m.group(2).strip()))
    except OSError:
        pass
    return _ChecklistState(items)


def _git_change_signature(working_directory: str) -> str:
    """Hash dirty file contents and HEAD, excluding Ralph's progress files."""
    workspace = Path(working_directory)
    try:
        status = subprocess.run(
            ["git", "--no-optional-locks", "status", "--porcelain=v1", "-z",
             "--untracked-files=all", "--no-renames", "--", ".", ":(exclude).nova/ralph"],
            cwd=workspace, capture_output=True, timeout=8, check=True,
        ).stdout
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
            cwd=workspace, capture_output=True, timeout=8, check=False,
        )
        if head.returncode not in (0, 1):
            return ""
        root = Path(os.fsdecode(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workspace, capture_output=True, timeout=8, check=True,
        ).stdout.rstrip(b"\r\n")))
        signature = hashlib.sha256(head.stdout + b"\0" + status)
        for entry in status.split(b"\0"):
            if not entry:
                continue
            path = root / os.fsdecode(entry[3:])
            if path.is_symlink():
                signature.update(os.fsencode(path.readlink()))
            elif path.is_file() and path.resolve().is_relative_to(workspace):
                with path.open("rb") as stream:
                    signature.update(hashlib.file_digest(stream, "sha256").digest())
        return signature.hexdigest()
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def _read_verify_command(working_directory: str) -> str:
    """The opt-in verification command from ``.nova/ralph/verify.txt`` ('' if none)."""
    cfg = Path(working_directory) / ".nova" / "ralph" / "verify.txt"
    try:
        if cfg.resolve().is_relative_to(working_directory) and cfg.is_file():
            return cfg.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pass
    return ""


@dataclass
class _VerifyResult:
    """Outcome of the opt-in per-iteration verification command."""

    passed: bool
    summary: str


def _run_verification(
    working_directory: str, command: str, cancelled: threading.Event,
) -> _VerifyResult | None:
    """Run the opt-in verification command, or ``None`` if not configured.

    The command is pinned before the agent starts so workspace edits cannot
    change the executable that the user opted into running.
    """
    if not command:
        return None
    try:
        argv = command if os.name == "nt" else shlex.split(command)
    except ValueError:
        argv = []
    if not argv:
        return _VerifyResult(passed=False, summary="Invalid verification command")
    if cancelled.is_set():
        return None
    try:
        with tempfile.TemporaryFile() as output, subprocess.Popen(  # noqa: S603
            argv, cwd=working_directory, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, **process_group_options(),
        ) as process:
            deadline = time.monotonic() + 600
            while process.poll() is None:
                if cancelled.wait(0.15) or time.monotonic() >= deadline:
                    kill_process_tree(process.pid)
                    if process.poll() is None:
                        process.kill()
                    return _VerifyResult(passed=False, summary="Verification stopped or timed out")
            size = output.seek(0, os.SEEK_END)
            output.seek(max(0, size - 4 * _VERIFY_TAIL_CHARS))
            tail = output.read().decode("utf-8", errors="replace").strip()
            returncode = process.returncode
    except OSError as e:
        return _VerifyResult(passed=False, summary=f"verification could not run: {e}")
    if len(tail) > _VERIFY_TAIL_CHARS:
        tail = "…\n" + tail[-_VERIFY_TAIL_CHARS:]
    return _VerifyResult(passed=returncode == 0, summary=tail or f"exit code {returncode}")


def _ralph_iteration_decision(
    checklist: _ChecklistState,
    no_change_streak: int,
    verify: _VerifyResult | None,
    max_no_change: int = _MAX_NO_CHANGE,
) -> tuple[str, str]:
    """Decide what to do after an iteration: ``continue`` / ``complete`` / ``stuck``.

    Pure (no I/O) so the loop policy is unit-testable. Completion requires every
    checklist item checked *and* verification passing (when configured). Stuck is
    only declared when there's been no file change for ``max_no_change``
    consecutive iterations and the work isn't actually finished.

    Returns ``(decision, human_reason)``.
    """
    verify_ok = verify is None or verify.passed
    if checklist.all_done and verify_ok:
        reason = f"all {checklist.total} checklist items complete"
        if verify is not None:
            reason += " and verification passed"
        return "complete", reason
    if no_change_streak >= max_no_change:
        return "stuck", f"no file changes for {no_change_streak} consecutive iterations"
    return "continue", ""


def _read_checklist_md(path: Path) -> str:
    """Raw checklist markdown for display in the prompt ('' if absent)."""
    p = path
    try:
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pass
    return ""


def _ralph_render_extras(
    no_change_streak: int, last_verify: _VerifyResult | None,
    *, workspace: Path = Path(), checklist_path: str = RALPH_CHECKLIST_PATH,
) -> dict[str, Any]:
    """Template kwargs that carry the smart-loop state into the next iteration."""
    path = workspace / checklist_path
    checklist = _read_checklist(path)
    return {
        "checklist_path": checklist_path,
        "verify_path": RALPH_VERIFY_PATH,
        "checklist_md": _read_checklist_md(path),
        "checklist_summary": checklist.summary,
        "verify_summary": last_verify.summary if last_verify else "",
        "verify_passed": last_verify.passed if last_verify else False,
        "no_change_streak": no_change_streak,
    }


def _ralph_evaluate(
    working_directory: str, sig_before: str, no_change_streak: int,
    checklist_path: Path, verify_command: str, cancelled: threading.Event,
) -> tuple[str, str, int, _VerifyResult | None]:
    """Post-iteration evaluation shared by both loops.

    Compares the working-tree fingerprint, runs verification, reads the
    checklist, and returns ``(decision, reason, new_no_change_streak, verify)``.
    An empty before/after signature (git unavailable) counts as "changed" so a
    git failure never false-positives the stuck check.
    """
    verify = _run_verification(working_directory, verify_command, cancelled)
    if cancelled.is_set():
        return "continue", "", no_change_streak, verify
    sig_after = _git_change_signature(working_directory)
    changed = (not sig_before) or (not sig_after) or (sig_after != sig_before)
    streak = 0 if changed else no_change_streak + 1
    checklist = _read_checklist(checklist_path)
    decision, reason = _ralph_iteration_decision(checklist, streak, verify)
    return decision, reason, streak, verify


# =============================================================================
# Ralph Checkpoint Management
# =============================================================================


def _get_checkpoint_path(working_directory: str) -> Path:
    key = hashlib.sha256(os.path.normcase(working_directory).encode("utf-8")).hexdigest()
    return Path.home() / ".nova" / "ralph" / "checkpoints" / f"{key}.json"


def _reserve_workspace(working_directory: str) -> int:
    """Hold the workspace's checkpoint lock until the run or process exits."""
    lock = _get_checkpoint_path(working_directory).with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise
    return fd


def _load_ralph_checkpoint(working_directory: str) -> dict[str, Any] | None:
    path = _get_checkpoint_path(working_directory)
    legacy = Path.home() / ".nova" / "ralph-checkpoint.json"
    try:
        checkpoint = json.loads((path if path.exists() else legacy).read_text(encoding="utf-8"))
        if not isinstance(checkpoint, dict):
            return None
        directory = checkpoint.get("working_directory")
        run_id = checkpoint.get("run_id", "")
        if (
            not isinstance(directory, str)
            or Path(directory).resolve() != Path(working_directory)
            or not isinstance(checkpoint.get("task"), str)
            or not checkpoint["task"].strip()
            or any(
                type(checkpoint.get(key)) is not int or checkpoint[key] < 0
                for key in ("max_iterations", "completed_iterations")
            )
            or type(checkpoint.get("budget", 5)) is not int
            or checkpoint.get("budget", 5) < 0
            or not isinstance(run_id, str)
            or (run_id and not re.fullmatch(r"[0-9a-f]{32}", run_id))
        ):
            return None
        return checkpoint
    except (OSError, UnicodeError, ValueError):
        return None


def _save_ralph_checkpoint(working_directory: str, record: dict[str, Any]) -> None:
    path = _get_checkpoint_path(working_directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(record, indent=2))


def _stop_and_save_all_ralph_tasks(session_state, emit: EmitFn = _console_emit) -> bool:
    """Request a stop. The current iteration already has a durable checkpoint."""
    if not any(
        task.status == RalphTaskStatus.RUNNING
        for task in session_state.background_ralph_tasks.values()
    ):
        return False
    session_state._ralph_stop_requested = True
    emit("[dim]Resume the saved Ralph run with /ralph --resume.[/dim]")
    return True


def _build_status_snapshot(session_state) -> rev.StatusSnapshot:
    """Collect background-task state into a plain, renderer-agnostic snapshot."""
    rows: list[rev.RalphTaskRow] = []
    running = completed = failed = 0
    now = datetime.now(UTC)
    for task_id, task in session_state.background_ralph_tasks.items():
        if task.status == RalphTaskStatus.RUNNING:
            status = "running"
            running += 1
            elapsed = (now - task.created_at).total_seconds()
        elif task.status == RalphTaskStatus.COMPLETED:
            status = "completed"
            completed += 1
            elapsed = (
                (task.completed_at - task.created_at).total_seconds() if task.completed_at else 0
            )
        elif task.status == RalphTaskStatus.FAILED:
            status = "failed"
            failed += 1
            elapsed = (
                (task.completed_at - task.created_at).total_seconds() if task.completed_at else 0
            )
        else:
            continue
        rows.append(
            rev.RalphTaskRow(
                iteration=task.iteration,
                max_iterations=task.max_iterations,
                status=status,
                task=task.task_description,
                task_id=task_id,
                elapsed=elapsed,
                working_directory=task.working_directory,
                error=task.error_message,
            )
        )
    return rev.StatusSnapshot(
        rows=rows,
        running=running,
        completed=completed,
        failed=failed,
        total=len(session_state.background_ralph_tasks),
    )


async def handle_ralph_status(
    session_state,
    emit: EmitFn = _console_emit,
    on_event: EventFn | None = None,
) -> bool:
    """Handle /ralph --status — show background task status (UI-agnostic)."""
    if on_event is not None:
        on_event(_build_status_snapshot(session_state))
        return True

    emit("")
    emit("[bold]Ralph Background Tasks[/bold]")

    if not session_state.background_ralph_tasks:
        emit("[dim]No background Ralph tasks running.[/dim]")
        emit("[dim]Note: tasks may take a moment to appear after being started.[/dim]")
        emit("")
        return True

    running_tasks = []
    completed_tasks = []
    failed_tasks = []
    for task_id, task in session_state.background_ralph_tasks.items():
        if task.status == RalphTaskStatus.RUNNING:
            running_tasks.append((task_id, task))
        elif task.status == RalphTaskStatus.COMPLETED:
            completed_tasks.append((task_id, task))
        elif task.status == RalphTaskStatus.FAILED:
            failed_tasks.append((task_id, task))

    if running_tasks:
        emit("")
        emit("[bold cyan]Running:[/bold cyan]")
        for task_id, task in running_tasks:
            elapsed = (datetime.now(UTC) - task.created_at).total_seconds()
            desc = task.task_description
            desc = desc[:60] + "..." if len(desc) > 60 else desc
            emit(f"  ⏳ Iteration {task.iteration}/{task.max_iterations}")
            emit(f"      Task: {escape(desc)}")
            emit(f"      ID: {task_id}")
            emit(f"      Duration: {elapsed:.0f}s")
            emit(f"      Dir: {escape(task.working_directory)}")

    if completed_tasks:
        emit("")
        emit("[bold green]Completed:[/bold green]")
        for _task_id, task in completed_tasks:
            elapsed = (
                (task.completed_at - task.created_at).total_seconds() if task.completed_at else 0
            )
            emit(f"  ✓ Iteration {task.iteration}/{task.max_iterations}")
            emit(f"      Duration: {elapsed:.0f}s")

    if failed_tasks:
        emit("")
        emit("[bold red]Failed:[/bold red]")
        for _task_id, task in failed_tasks:
            emit(f"  ✗ Iteration {task.iteration}/{task.max_iterations}")
            if task.error_message:
                msg = task.error_message
                msg = msg[:80] + "..." if len(msg) > 80 else msg
                emit(f"      Error: {escape(msg)}")

    emit("")
    emit("[bold]Summary:[/bold]")
    emit(f"  Total: {len(session_state.background_ralph_tasks)} tasks")
    emit(f"  Running: {len(running_tasks)}")
    emit(f"  Completed: {len(completed_tasks)}")
    emit(f"  Failed: {len(failed_tasks)}")
    emit("")
    return True


async def _run_ralph_loop(
    *, record: dict[str, Any], workspace: Path, start_iteration: int,
    agent, session_state, token_tracker: TokenTracker, backend, execute_fn,
    verify_command: str, background: bool, emit: EmitFn, on_event: EventFn | None,
) -> None:
    task = record["task"]
    limit = record["max_iterations"]
    completed = start_iteration - 1
    directory = Path(".nova/ralph/runs") / record["run_id"]
    progress_path = (directory / "progress.md").as_posix()
    checklist_path = (directory / "checklist.md").as_posix()
    active: BackgroundRalphTask | None = None
    running_iteration = False
    failed = 0
    reason, detail = "limit", "Iteration limit reached"
    streak = 0
    last_verify = None

    async def checkpoint() -> None:
        record["completed_iterations"] = completed
        save = asyncio.create_task(asyncio.to_thread(_save_ralph_checkpoint, str(workspace), record.copy()))
        try:
            await asyncio.shield(save)
        except asyncio.CancelledError:
            await save
            raise

    try:
        def validate_progress_paths() -> None:
            for path in (directory, Path(progress_path), Path(checklist_path)):
                if not (workspace / path).resolve().is_relative_to(workspace):
                    raise ValueError("Ralph progress files must stay inside the workspace")

        validate_progress_paths()
        await asyncio.to_thread((workspace / directory).mkdir, parents=True, exist_ok=True)
        await checkpoint()
        conversation = ""
        try:
            from novacode_cli.context import ContextManager

            conversation = await ContextManager().digest(agent, session_state.thread_id)
        except Exception:  # The worktree and checkpoint are sufficient to continue.
            pass

        while limit == 0 or completed < limit:
            if getattr(session_state, "_ralph_stop_requested", False):
                reason, detail = "stopped", "Stopped by user"
                break
            if getattr(session_state, "_ralph_checkpoint_requested", False):
                reason, detail = "checkpoint", "Paused after saving a checkpoint"
                break
            if background and not session_state.auto_approve:
                reason, detail = "stopped", "Background run paused because auto-approval was turned off"
                break

            validate_progress_paths()
            iteration = completed + 1
            display = f"{iteration}/{limit}" if limit else str(iteration)
            if background:
                key = uuid.uuid4().hex
                active = BackgroundRalphTask(
                    key, iteration, limit, task, working_directory=str(workspace)
                )
                session_state.background_ralph_tasks[key] = active
            started = datetime.now(UTC)
            running_iteration = True
            _emit_event(
                on_event, rev.IterationStarted(iteration, limit),
                lambda: emit(f"[cyan]Iteration {display}[/cyan]"),
            )
            sig_before, progress, extras = await asyncio.gather(
                asyncio.to_thread(_git_change_signature, str(workspace)),
                asyncio.to_thread(_read_ralph_progress, workspace / progress_path),
                asyncio.to_thread(
                    _ralph_render_extras, streak, last_verify,
                    workspace=workspace, checklist_path=checklist_path,
                ),
            )
            prompt = render_template(
                "ralph_iteration.jinja", iteration_display=display, task=task,
                conversation_context=conversation, progress_notes=progress,
                progress_path=progress_path, **extras,
            )
            outcome = await execute_fn(
                prompt, agent, "ralph", session_state, token_tracker, backend=backend,
            )
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            if outcome is False:
                raise RuntimeError("The agent turn ended before completing")
            completed = iteration
            await checkpoint()
            running_iteration = False
            elapsed = (datetime.now(UTC) - started).total_seconds()
            if active is not None:
                active.status = RalphTaskStatus.COMPLETED
                active.completed_at = datetime.now(UTC)
                active = None
            _emit_event(
                on_event, rev.IterationFinished(iteration, limit, ok=True, elapsed=elapsed),
                lambda: emit(f"[dim]Iteration {display} ended ({elapsed:.1f}s)[/dim]"),
            )
            if (
                getattr(session_state, "_ralph_stop_requested", False)
                or getattr(session_state, "_ralph_checkpoint_requested", False)
            ):
                reason, detail = ("stopped", "Stopped by user") if getattr(
                    session_state, "_ralph_stop_requested", False
                ) else ("checkpoint", "Paused after saving a checkpoint")
                break
            validate_progress_paths()
            cancelled = threading.Event()
            evaluation = asyncio.create_task(asyncio.to_thread(
                _ralph_evaluate, str(workspace), sig_before, streak,
                workspace / checklist_path, verify_command, cancelled,
            ))
            try:
                decision, decision_reason, streak, last_verify = await asyncio.shield(evaluation)
            except asyncio.CancelledError:
                cancelled.set()
                await evaluation
                raise
            if decision != "continue":
                reason, detail = decision, decision_reason
                break
        if reason in {"limit", "stuck"} and last_verify is not None and not last_verify.passed:
            detail += " with verification still failing"
        if reason == "complete":
            record["complete"] = True
            await checkpoint()
    except (asyncio.CancelledError, KeyboardInterrupt):
        reason, detail = "stopped", "Stopped by user"
    except Exception as exc:
        logger.debug("Ralph run failed", exc_info=True)
        reason, detail = "failed", str(exc)
        failed = int(running_iteration)
        if running_iteration:
            _emit_event(
                on_event, rev.IterationFinished(
                    iteration, limit, ok=False, elapsed=(datetime.now(UTC) - started).total_seconds(),
                    error=detail[:300],
                ),
                lambda: None,
            )
    finally:
        if active is not None:
            active.status = RalphTaskStatus.CANCELLED if reason == "stopped" else RalphTaskStatus.FAILED
            active.error_message = detail
            active.completed_at = datetime.now(UTC)
        session_state._ralph_stop_requested = False
        session_state._ralph_checkpoint_requested = False
        if reason != "complete" and not record.get("complete"):
            if _get_checkpoint_path(str(workspace)).is_file():
                detail += " · Resume with /ralph --resume"
        _emit_event(
            on_event, rev.RalphFinished(completed, failed, completed + failed, reason, detail),
            lambda: emit(f"{escape(detail)} · {completed} completed iteration(s)"),
        )


def _run_background_ralph_in_thread(lock_fd: int, **kwargs: Any) -> None:
    try:
        asyncio.run(_run_ralph_loop(**kwargs))
    finally:
        os.close(lock_fd)


def _print_ralph_usage(emit: EmitFn) -> None:
    """Emit the /ralph usage help."""
    emit("")
    emit("[yellow]Usage: /ralph <task> [--iterations N][/yellow]")
    emit("[yellow]       /ralph --resume[/yellow]")
    emit("[yellow]       /ralph --status[/yellow]")
    emit("")
    emit("[dim]Example: /ralph Fix the bug in auth.py --iterations 5[/dim]")
    emit("[dim]        /ralph Implement the feature described in issue #42[/dim]")
    emit("[dim]        /ralph --resume  (continue from last checkpoint)[/dim]")
    emit("[dim]        /ralph --status (check running background tasks)[/dim]")
    emit("")
    emit("[bold]Options:[/bold]")
    emit("  --iterations N, -i N    Maximum iterations (default: 5, 0 runs without a limit)")
    emit("  --background            Run while NovaCode is open; requires existing auto-approval")
    emit("  --resume                Resume unfinished work in this workspace")
    emit("  --verify                Run the command in .nova/ralph/verify.txt each iteration")
    emit("  --status                Show status of background ralph tasks")
    emit("")
    emit("[dim]Ralph saves progress after each iteration. Esc stops the current run.[/dim]")
    emit("")


def _emit_ralph_header(
    emit: EmitFn,
    *,
    task: str,
    max_iterations: int,
    background_mode: bool,
    resumed_from: int | None = None,
) -> None:
    """Emit the Ralph run header (replaces the old Rich Panel, works in both UIs)."""
    title = "🔄 Ralph Mode (Resumed)" if resumed_from else "🔄 Ralph Mode"
    iters = "Unlimited" if max_iterations == 0 else max_iterations
    emit("")
    emit(f"[bold {COLORS['primary']}]{title}[/bold {COLORS['primary']}]")
    emit(f"[bold]Task:[/bold] {escape(task)}")
    emit(f"[bold]Max Iterations:[/bold] {iters}")
    if resumed_from:
        emit(f"[bold]Resuming from:[/bold] Iteration {resumed_from}")
    if background_mode:
        emit("[bold]Mode:[/bold] Background (non-blocking)")
    emit("")
    if background_mode:
        emit("[dim]Background mode enabled. Iterations will run asynchronously.[/dim]")
    else:
        emit("[dim]Press Ctrl+C to stop and keep the checkpoint.[/dim]")
    emit("")


async def handle_ralph_command(
    agent,
    session_state,
    assistant_id: str,
    token_tracker: TokenTracker,
    cmd_args: str | list[str] | None,
    execute_fn=None,
    emit: EmitFn | None = None,
    on_event: EventFn | None = None,
) -> bool:
    """Handle /ralph command - autonomous looping mode.

    Usage:
        /ralph <task>                 - Run for up to five iterations
        /ralph <task> --iterations N  - Run with max N iterations
        /ralph <task> -i N            - Shorthand for --iterations
        /ralph <task> --background    - Run iterations in background (non-blocking)
        /ralph --resume               - Resume from last checkpoint
        /ralph --status               - Show status of running background tasks

    ``emit`` is the markup UI sink (defaults to the console). ``on_event`` is an
    optional structured-progress sink: when provided (the TUI passes one), the
    run's milestones render as native widgets instead of the markup ``emit``
    fallback. The CLI leaves it ``None`` and keeps its existing output.
    """
    if emit is None:
        emit = _console_emit
    if execute_fn is None:
        from novacode_cli.ui.execution import execute_task

        execute_fn = execute_task

    if not cmd_args:
        _print_ralph_usage(emit)
        return True

    # Normalize args to a list.
    if isinstance(cmd_args, str):
        cmd_args = cmd_args.split()
    elif not cmd_args:
        cmd_args = []

    # --status short-circuit.
    if cmd_args and len(cmd_args) == 1 and cmd_args[0] == "--status":
        return await handle_ralph_status(session_state, emit, on_event)

    budget = None
    background = resume = verify = False
    task_parts = []
    i = 0
    while i < len(cmd_args):
        arg = cmd_args[i]
        i += 1
        if arg == "--resume":
            resume = True
        elif arg == "--background":
            background = True
        elif arg == "--verify":
            verify = True
        elif arg in ("--iterations", "-i"):
            value = cmd_args[i] if i < len(cmd_args) else ""
            if not re.fullmatch(r"[0-9]{1,9}", value):
                emit("[red]Error: --iterations requires a number[/red]")
                return True
            budget = int(value)
            i += 1
        else:
            task_parts.append(arg)

    if resume and task_parts:
        emit("[red]Start a new task without --resume, or resume without task text.[/red]")
        return True
    if not resume and not task_parts:
        _print_ralph_usage(emit)
        return True
    if getattr(session_state, "_sandbox_type", None) not in {None, "none", "os"}:
        emit("[red]Ralph requires a local workspace for progress and checkpoints.[/red]")
        return True
    if getattr(session_state, "plan_mode_enabled", False):
        emit("[yellow]Exit Plan Mode before starting Ralph.[/yellow]")
        return True
    if background and not session_state.auto_approve:
        emit("[yellow]Background Ralph requires a session with auto-approval already enabled.[/yellow]")
        return True

    workspace = Path(session_directory(session_state)).resolve()
    if not workspace.is_dir():
        emit("[red]Ralph needs an existing workspace directory.[/red]")
        return True
    directory = str(workspace)
    try:
        lock_fd = _reserve_workspace(directory)
    except OSError as exc:
        emit(f"[yellow]Could not reserve the Ralph workspace. Check for another active run. {escape(str(exc))}[/yellow]")
        return True

    handed_off = False
    try:
        if resume:
            saved = await asyncio.to_thread(_load_ralph_checkpoint, directory)
            if not saved or saved.get("complete"):
                emit("[yellow]No unfinished Ralph checkpoint in this workspace.[/yellow]")
                return True
            if saved.get("verify") and not verify:
                emit("[yellow]Resume this verified run with /ralph --resume --verify.[/yellow]")
                return True
            completed = saved["completed_iterations"]
            limit = saved["max_iterations"]
            if budget is not None:
                limit = completed + budget if budget else 0
            elif limit and completed >= limit:
                limit = completed + saved.get("budget", 5)
            record = {
                **saved, "run_id": saved.get("run_id") or uuid.uuid4().hex, "max_iterations": limit,
                "budget": budget if budget is not None else saved.get("budget", 5), "verify": verify,
            }
        else:
            limit = 5 if budget is None else budget
            completed = 0
            record = {
                "task": " ".join(task_parts), "working_directory": directory,
                "run_id": uuid.uuid4().hex, "max_iterations": limit, "budget": limit,
                "verify": verify,
            }
        command = await asyncio.to_thread(_read_verify_command, directory) if verify else ""
        if verify and not command:
            emit("[red]--verify needs a command in .nova/ralph/verify.txt.[/red]")
            return True
        start_iteration = completed + 1
        _emit_event(
            on_event, rev.RalphStarted(record["task"], limit, background, start_iteration if resume else None),
            lambda: _emit_ralph_header(
                emit, task=record["task"], max_iterations=limit, background_mode=background,
                resumed_from=start_iteration if resume else None,
            ),
        )
        backend = getattr(agent, "backend", None) or getattr(getattr(agent, "graph", None), "backend", None)
        kwargs = dict(
            record=record, workspace=workspace, start_iteration=start_iteration,
            agent=agent, session_state=session_state, token_tracker=token_tracker, backend=backend,
            execute_fn=execute_fn, verify_command=command, background=background,
            emit=emit, on_event=on_event,
        )
        if background:
            from novacode_cli.ui.execution import execute_task

            kwargs["execute_fn"] = partial(execute_task, is_subagent=True)
            thread = threading.Thread(
                target=_run_background_ralph_in_thread, args=(lock_fd,), kwargs=kwargs, daemon=True,
            )
            thread.start()
            handed_off = True
            emit("[dim]Ralph is running in the background. Check /ralph --status.[/dim]")
        else:
            await _run_ralph_loop(**kwargs)
        return True
    finally:
        if not handed_off:
            os.close(lock_fd)


# Registry
# ---------------------------------------------------------------------------


def register_commands(registry) -> None:
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        return await handle_ralph_command(
            ctx.agent,
            ctx.session_state,
            ctx.assistant_id,
            ctx.token_tracker,
            ctx.cmd_args,
        )

    registry.register("ralph", _handle)
