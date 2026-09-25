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
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.markup import escape

from novacode_cli.commands import ralph_events as rev
from novacode_cli.config.config import COLORS, console
from novacode_cli.prompts import render_template
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


def _read_ralph_progress() -> str:
    """Return the current progress.md contents (bounded), or '' if absent."""
    p = Path(".nova") / "ralph" / "progress.md"
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


def _read_checklist() -> _ChecklistState:
    """Parse ``.nova/ralph/checklist.md`` (empty state if absent/unreadable)."""
    p = Path(".nova") / "ralph" / "checklist.md"
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
    """Fingerprint the working tree, to detect a no-progress iteration.

    Combines ``git status --porcelain`` (new/untracked files) with ``git diff
    HEAD`` (edits to already-dirty tracked files, which the status line alone
    would miss). Returns ``""`` on any git failure — the caller treats an empty
    signature as "changed" so a git hiccup never false-positives as stuck.
    """
    chunks: list[str] = []
    for args in (["status", "--porcelain"], ["diff", "HEAD"]):
        try:
            r = subprocess.run(  # noqa: S603, S607 — fixed git command
                ["git", *args],
                cwd=working_directory,
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""
        if r.returncode != 0:
            return ""
        chunks.append(r.stdout)
    return hashlib.sha256("\x00".join(chunks).encode("utf-8", "replace")).hexdigest()


def _read_verify_command(working_directory: str) -> str:
    """The opt-in verification command from ``.nova/ralph/verify.txt`` ('' if none)."""
    cfg = Path(working_directory) / ".nova" / "ralph" / "verify.txt"
    try:
        if cfg.exists():
            return cfg.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pass
    return ""


@dataclass
class _VerifyResult:
    """Outcome of the opt-in per-iteration verification command."""

    passed: bool
    summary: str


def _run_verification(working_directory: str) -> _VerifyResult | None:
    """Run the opt-in verification command, or ``None`` if not configured.

    The command lives in ``.nova/ralph/verify.txt`` (one shell command). Opt-in
    by design: auto-running a whole test suite every iteration would be slow and
    surprising. Best-effort — a missing/blank file, an unparseable command, or a
    launch failure returns ``None`` (verification simply doesn't gate the loop).
    """
    command = _read_verify_command(working_directory)
    if not command:
        return None
    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        argv = []
    if not argv:
        return None
    try:
        r = subprocess.run(  # noqa: S603 — user's own opt-in verify command, by design
            argv,
            cwd=working_directory,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return _VerifyResult(passed=False, summary=f"verification could not run: {e}")
    tail = (r.stdout + r.stderr).strip()
    if len(tail) > _VERIFY_TAIL_CHARS:
        tail = "…\n" + tail[-_VERIFY_TAIL_CHARS:]
    return _VerifyResult(passed=r.returncode == 0, summary=tail or f"exit code {r.returncode}")


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
    if no_change_streak >= max_no_change and not checklist.all_done:
        return "stuck", f"no file changes for {no_change_streak} consecutive iterations"
    return "continue", ""


def _read_checklist_md() -> str:
    """Raw checklist markdown for display in the prompt ('' if absent)."""
    p = Path(".nova") / "ralph" / "checklist.md"
    try:
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pass
    return ""


def _ralph_render_extras(
    no_change_streak: int, last_verify: _VerifyResult | None
) -> dict[str, Any]:
    """Template kwargs that carry the smart-loop state into the next iteration."""
    checklist = _read_checklist()
    return {
        "checklist_path": RALPH_CHECKLIST_PATH,
        "verify_path": RALPH_VERIFY_PATH,
        "checklist_md": _read_checklist_md(),
        "checklist_summary": checklist.summary,
        "verify_summary": last_verify.summary if last_verify else "",
        "verify_passed": last_verify.passed if last_verify else False,
        "no_change_streak": no_change_streak,
    }


def _ralph_evaluate(
    working_directory: str, sig_before: str, no_change_streak: int
) -> tuple[str, str, int, _VerifyResult | None]:
    """Post-iteration evaluation shared by both loops.

    Compares the working-tree fingerprint, runs verification, reads the
    checklist, and returns ``(decision, reason, new_no_change_streak, verify)``.
    An empty before/after signature (git unavailable) counts as "changed" so a
    git failure never false-positives the stuck check.
    """
    sig_after = _git_change_signature(working_directory)
    changed = (not sig_before) or (not sig_after) or (sig_after != sig_before)
    streak = 0 if changed else no_change_streak + 1
    verify = _run_verification(working_directory)
    checklist = _read_checklist()
    decision, reason = _ralph_iteration_decision(checklist, streak, verify)
    return decision, reason, streak, verify


# =============================================================================
# Ralph Checkpoint Management
# =============================================================================


def _get_checkpoint_path() -> Path:
    """Get the path to the ralph checkpoint file."""
    home = Path.home()
    return home / ".nova" / "ralph-checkpoint.json"


def _save_ralph_checkpoint(
    task: str,
    max_iterations: int,
    completed_iterations: int,
    working_directory: str,
    notes: str = "",
) -> None:
    """Save ralph checkpoint to disk."""
    checkpoint_path = _get_checkpoint_path()
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    checkpoint: dict[str, Any] = {
        "task": task,
        "max_iterations": max_iterations,
        "completed_iterations": completed_iterations,
        "timestamp": datetime.now(UTC).isoformat(),
        "working_directory": working_directory,
        "notes": notes,
    }

    # Track modified files via git
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=working_directory,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            checkpoint["files_modified"] = [
                line[3:] for line in result.stdout.strip().split("\n") if line
            ]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        checkpoint["files_modified"] = []

    with checkpoint_path.open("w") as f:
        json.dump(checkpoint, f, indent=2)


def _load_ralph_checkpoint() -> dict[str, Any] | None:
    """Load ralph checkpoint from disk."""
    checkpoint_path = _get_checkpoint_path()
    if not checkpoint_path.exists():
        return None

    try:
        with checkpoint_path.open("r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _clear_ralph_checkpoint() -> None:
    """Remove ralph checkpoint file."""
    checkpoint_path = _get_checkpoint_path()
    if checkpoint_path.exists():
        checkpoint_path.unlink()


def _stop_and_save_all_ralph_tasks(session_state, emit: EmitFn = _console_emit) -> bool:
    """Stop all running Ralph background tasks and save their state.

    Returns True if tasks were stopped and saved, False if none were running.
    """
    if not session_state.background_ralph_tasks:
        return False

    running_tasks = [
        task
        for task in session_state.background_ralph_tasks.values()
        if task.status == RalphTaskStatus.RUNNING
    ]

    if not running_tasks:
        return False

    for task in running_tasks:
        try:
            # Completed iterations = current - 1 (we stop before this one finishes).
            completed = max(0, task.iteration - 1)
            _save_ralph_checkpoint(
                task=task.task_description,
                max_iterations=task.max_iterations,
                completed_iterations=completed,
                working_directory=task.working_directory,
                notes=f"Stopped at iteration {task.iteration}. Resume with /ralph --resume",
            )
            task.status = RalphTaskStatus.CANCELLED
            task.completed_at = datetime.now(UTC)

            emit(f"[yellow]✓[/yellow] Saved Ralph checkpoint at iteration {task.iteration}")
            emit("[dim]  Resume later with: /ralph --resume[/dim]")
        except Exception as e:
            emit(f"[red]✗ Failed to save Ralph checkpoint: {escape(str(e))}[/red]")

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


def _run_background_ralph_in_thread(
    task: str,
    start_iteration: int,
    max_iterations: int,
    agent,
    assistant_id: str,
    session_state,
    token_tracker: TokenTracker,
    working_directory: str,
    backend,
    emit: EmitFn = _console_emit,
    on_event: EventFn | None = None,
) -> None:
    """Run background Ralph iterations in a separate thread with its own loop."""
    loop = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            _execute_all_ralph_iterations_background(
                task=task,
                start_iteration=start_iteration,
                max_iterations=max_iterations,
                agent=agent,
                assistant_id=assistant_id,
                session_state=session_state,
                token_tracker=token_tracker,
                working_directory=working_directory,
                backend=backend,
                emit=emit,
                on_event=on_event,
            )
        )
    except BaseException as e:
        logger.exception("Background Ralph execution failed")
        emit(f"[red]✗ Background Ralph execution failed: {escape(str(e))}[/red]")
    finally:
        if loop is not None:
            try:
                loop.close()
            except Exception:
                logger.exception("Failed to close background Ralph event loop")


async def _execute_all_ralph_iterations_background(
    task: str,
    start_iteration: int,
    max_iterations: int,
    agent,
    assistant_id: str,
    session_state,
    token_tracker: TokenTracker,
    working_directory: str,
    backend,
    emit: EmitFn = _console_emit,
    on_event: EventFn | None = None,
) -> None:
    """Execute all Ralph iterations sequentially in the background (non-blocking).

    Tool calls are auto-approved so the run never blocks on input. Progress is
    surfaced via ``emit`` / ``on_event`` and a final notification; check anytime
    with ``/ralph --status``.
    """
    from novacode_cli.ui.execution import execute_task

    def _hdr() -> None:
        emit("")
        emit("[bold]🚀 Ralph background execution started[/bold]")
        desc = task[:70] + "..." if len(task) > 70 else task
        emit(f"[dim]Task: {escape(desc)}[/dim]")

    _emit_event(
        on_event,
        rev.RalphStarted(task=task, max_iterations=max_iterations, background=True),
        _hdr,
    )

    original_auto_approve = session_state.auto_approve
    session_state.auto_approve = True

    # Ground the loop in the prior conversation (read once before iterating).
    conversation_context = ""
    try:
        from novacode_cli.context import ContextManager

        conversation_context = await ContextManager().digest(agent, session_state.thread_id)
    except Exception:
        conversation_context = ""

    _ensure_ralph_dir()
    iteration = start_iteration
    # Smart-loop state, carried across iterations.
    no_change_streak = 0
    last_verify: _VerifyResult | None = None
    sig_before = ""

    try:
        try:
            while max_iterations == 0 or iteration <= max_iterations:
                # Check for stop/checkpoint requests from TUI
                if getattr(session_state, "_ralph_stop_requested", False):
                    session_state._ralph_stop_requested = False
                    break

                if getattr(session_state, "_ralph_checkpoint_requested", False):
                    session_state._ralph_checkpoint_requested = False
                    _save_ralph_checkpoint(
                        task=task,
                        max_iterations=max_iterations,
                        completed_iterations=iteration - 1,
                        working_directory=working_directory,
                        notes="Checkpoint requested by user (background mode)",
                    )
                    emit(f"[green]✓ Saved Ralph checkpoint at iteration {iteration - 1}[/green]")
                    break

                task_id = str(uuid.uuid4())
                iters_str = f"{iteration}/{max_iterations}" if max_iterations > 0 else str(
                    iteration,
                )
                iter_display = iters_str

                bg_task = BackgroundRalphTask(
                    task_id=task_id,
                    iteration=iteration,
                    max_iterations=max_iterations,
                    task_description=task,
                    working_directory=working_directory,
                )
                session_state.background_ralph_tasks[task_id] = bg_task

                _emit_event(
                    on_event,
                    rev.IterationStarted(iteration=iteration, max_iterations=max_iterations),
                    lambda disp=iter_display, tid=task_id: emit(
                        f"[dim]\u2192 iteration {disp} started ({tid[:8]})[/dim]"
                    ),
                )

                sig_before = _git_change_signature(working_directory)
                prompt = render_template(
                    "ralph_iteration.jinja",
                    iteration_display=iter_display,
                    task=task,
                    conversation_context=conversation_context,
                    progress_notes=_read_ralph_progress(),
                    progress_path=RALPH_PROGRESS_PATH,
                    **_ralph_render_extras(no_change_streak, last_verify),
                )

                try:
                    await execute_task(
                        prompt,
                        agent,
                        "ralph",  # display the agent as "Ralph"
                        session_state,
                        token_tracker,
                        backend=backend,
                    )
                    bg_task.status = RalphTaskStatus.COMPLETED
                    bg_task.completed_at = datetime.now(UTC)
                    elapsed = (bg_task.completed_at - bg_task.created_at).total_seconds()
                    _emit_event(
                        on_event,
                        rev.IterationFinished(
                            iteration=iteration,
                            max_iterations=max_iterations,
                            ok=True,
                            elapsed=elapsed,
                        ),
                        lambda disp=iter_display, el=elapsed: emit(
                            f"[green]\u2713 iteration {disp} done[/green] [dim]({el:.1f}s)[/dim]"
                        ),
                    )
                except Exception as e:
                    bg_task.status = RalphTaskStatus.FAILED
                    bg_task.error_message = str(e)
                    bg_task.completed_at = datetime.now(UTC)
                    elapsed = (bg_task.completed_at - bg_task.created_at).total_seconds()
                    err = str(e)[:100] + "..." if len(str(e)) > 100 else str(e)
                    _emit_event(
                        on_event,
                        rev.IterationFinished(
                            iteration=iteration,
                            max_iterations=max_iterations,
                            ok=False,
                            elapsed=elapsed,
                            error=err,
                        ),
                        lambda disp=iter_display, msg=err: emit(
                            f"[red]\u2717 iteration {disp} failed: {escape(msg)}[/red]"
                        ),
                    )

                iteration += 1

                decision, decision_reason, no_change_streak, last_verify = _ralph_evaluate(
                    working_directory, sig_before, no_change_streak
                )
                if decision in ("complete", "stuck"):
                    style = "green" if decision == "complete" else "yellow"
                    label = "Task complete" if decision == "complete" else "Stopped — no progress"
                    emit(f"[{style}]{label}.[/{style}] [dim]{decision_reason}[/dim]")
                    _emit_event(
                        on_event,
                        rev.RalphFinished(
                            completed=iteration - 1, failed=0, total=iteration - 1, reason=decision
                        ),
                        lambda: None,
                    )
                    break
        except asyncio.CancelledError:
            _save_ralph_checkpoint(
                task=task,
                max_iterations=max_iterations,
                completed_iterations=iteration - 1,
                working_directory=working_directory,
                notes="Cancelled (background mode)",
            )
            iter_n = iteration - 1
            emit(f"[yellow]\u2713 Saved checkpoint at iteration {iter_n} before cancel[/yellow]")
            raise
    finally:
        total_tasks = len(session_state.background_ralph_tasks)
        completed = sum(
            1
            for t in session_state.background_ralph_tasks.values()
            if t.status == RalphTaskStatus.COMPLETED
        )
        failed = sum(
            1
            for t in session_state.background_ralph_tasks.values()
            if t.status == RalphTaskStatus.FAILED
        )

        def _summary() -> None:
            emit("")
            emit(
                f"[bold]📊 Ralph background summary[/bold] — "
                f"{completed} completed, {failed} failed of {total_tasks} task(s)"
            )

        _emit_event(
            on_event,
            rev.RalphFinished(
                completed=completed,
                failed=failed,
                total=total_tasks,
                reason="background",
            ),
            _summary,
        )

        try:
            session_state.add_notification(
                level="error" if failed and not completed else ("warning" if failed else "success"),
                title="Ralph background run finished",
                message=f"{completed} completed, {failed} failed of {total_tasks} task(s)",
                source="ralph",
            )
        except Exception:
            pass

        session_state.auto_approve = original_auto_approve


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
    emit("  --iterations N, -i N    Maximum number of iterations (default: 5)")
    emit("  --background            Run iterations in background (non-blocking)")
    emit("  --resume                Resume from last saved checkpoint")
    emit("  --status                Show status of background ralph tasks")
    emit("")
    emit("[dim]Ralph mode runs autonomously, making progress each iteration.[/dim]")
    emit("[dim]Press Ctrl+C to choose: Stop, Finish iteration, Continue, or Checkpoint.[/dim]")
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
    emit("[dim]Press Ctrl+C to stop at any time.[/dim]")
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
        /ralph <task>                 - Run autonomously (unlimited iterations)
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

    # Parse task, iterations, resume flag, and background mode.
    max_iterations = 5
    background_mode = False
    resume_mode = False
    task_parts: list[str] = []
    i = 0
    while i < len(cmd_args):
        arg = cmd_args[i]
        if arg == "--resume":
            resume_mode = True
            i += 1
            continue
        if arg == "--background":
            background_mode = True
            i += 1
            continue
        if arg in ("--iterations", "-i"):
            if i + 1 < len(cmd_args) and cmd_args[i + 1].isdigit():
                max_iterations = int(cmd_args[i + 1])
                i += 2
                continue
            emit("")
            emit("[red]Error: --iterations requires a number[/red]")
            emit("[dim]Example: /ralph task --iterations 5[/dim]")
            emit("")
            return True
        task_parts.append(arg)
        i += 1

    task = " ".join(task_parts)

    # Resume mode.
    if resume_mode:
        checkpoint = _load_ralph_checkpoint()
        if not checkpoint:
            emit("")
            emit("[red]No checkpoint found to resume.[/red]")
            emit("[dim]Start a new ralph session first: /ralph <task>[/dim]")
            emit("")
            return True

        task = checkpoint["task"]
        max_iterations = checkpoint["max_iterations"]
        start_iteration = checkpoint["completed_iterations"] + 1
        working_directory = checkpoint.get("working_directory", os.getcwd())

        _emit_event(
            on_event,
            rev.RalphStarted(
                task=task,
                max_iterations=max_iterations,
                background=background_mode,
                resumed_from=start_iteration,
            ),
            lambda: _emit_ralph_header(
                emit,
                task=task,
                max_iterations=max_iterations,
                background_mode=background_mode,
                resumed_from=start_iteration,
            ),
        )
        _clear_ralph_checkpoint()
    else:
        if not task:
            emit("")
            emit("[red]Error: No task specified[/red]")
            emit("[dim]Usage: /ralph <task> [--iterations N][/dim]")
            emit("[dim]       /ralph --resume[/dim]")
            emit("")
            return True

        start_iteration = 1
        working_directory = os.getcwd()
        _emit_event(
            on_event,
            rev.RalphStarted(
                task=task,
                max_iterations=max_iterations,
                background=background_mode,
            ),
            lambda: _emit_ralph_header(
                emit,
                task=task,
                max_iterations=max_iterations,
                background_mode=background_mode,
            ),
        )

    # Resolve backend from the agent.
    backend = None
    if hasattr(agent, "backend"):
        backend = agent.backend
    elif hasattr(agent, "graph") and hasattr(agent.graph, "backend"):
        backend = agent.graph.backend

    # Background mode: one thread runs all iterations sequentially.
    if background_mode:
        background_thread = threading.Thread(
            target=_run_background_ralph_in_thread,
            args=(
                task,
                start_iteration,
                max_iterations,
                agent,
                assistant_id,
                session_state,
                token_tracker,
                working_directory,
                backend,
                emit,
                on_event,
            ),
            daemon=True,
        )
        if not hasattr(session_state, "_background_threads"):
            session_state._background_threads = []
        session_state._background_threads.append(background_thread)
        background_thread.start()

        emit("")
        emit("[green]✓ Background execution started[/green]")
        emit("[dim]Iterations will progress asynchronously in the background.[/dim]")
        emit("[dim]Check progress with: /ralph --status[/dim]")
        emit("")
        return True

    # Foreground mode: run iterations synchronously.
    stop_after_iteration = False
    interrupted = False

    original_auto_approve = session_state.auto_approve
    session_state.auto_approve = True

    # Ground the loop in the prior conversation (read once before iterating).
    conversation_context = ""
    try:
        from novacode_cli.context import ContextManager

        conversation_context = await ContextManager().digest(agent, session_state.thread_id)
    except Exception:
        conversation_context = ""

    _ensure_ralph_dir()
    iteration = start_iteration
    # Smart-loop state, carried across iterations.
    no_change_streak = 0
    last_verify: _VerifyResult | None = None
    sig_before = ""

    try:
        try:
            while max_iterations == 0 or iteration <= max_iterations:
                if getattr(session_state, "_ralph_stop_requested", False):
                    session_state._ralph_stop_requested = False
                    done = iteration - 1

                    def _finished_stop(done: int = done) -> None:
                        emit("")
                        emit("[yellow]Ralph mode stopped by user.[/yellow]")
                        emit(f"[dim]Completed {done} iteration(s).[/dim]")
                        emit("")

                    _emit_event(
                        on_event,
                        rev.RalphFinished(completed=done, failed=0, total=done, reason="stopped"),
                        _finished_stop,
                    )
                    return True

                if getattr(session_state, "_ralph_checkpoint_requested", False):
                    session_state._ralph_checkpoint_requested = False
                    done = iteration - 1
                    _save_ralph_checkpoint(
                        task=task,
                        max_iterations=max_iterations,
                        completed_iterations=done,
                        working_directory=working_directory,
                        notes="Checkpoint requested by user",
                    )

                    def _finished_checkpoint(done: int = done) -> None:
                        emit("")
                        emit(f"[green]✓ Saved Ralph checkpoint at iteration {done}[/green]")
                        emit(f"[dim]Completed {done} iteration(s). Run stopped.[/dim]")
                        emit("")

                    _emit_event(
                        on_event,
                        rev.RalphFinished(
                            completed=done, failed=0, total=done, reason="checkpoint"
                        ),
                        _finished_checkpoint,
                    )
                    return True

                iters_str = f"{iteration}/{max_iterations}" if max_iterations > 0 else str(
                    iteration,
                )
                iter_display = iters_str

                def _banner(display: str = iter_display) -> None:
                    emit("")
                    emit(f"[bold cyan]{'─' * 50}[/bold cyan]")
                    emit(f"[bold cyan]Iteration {display}[/bold cyan]")
                    emit(f"[bold cyan]{'─' * 50}[/bold cyan]")
                    emit("")

                _emit_event(
                    on_event,
                    rev.IterationStarted(iteration=iteration, max_iterations=max_iterations),
                    _banner,
                )
                iter_start = datetime.now(UTC)

                sig_before = _git_change_signature(working_directory)
                prompt = render_template(
                    "ralph_iteration.jinja",
                    iteration_display=iter_display,
                    task=task,
                    conversation_context=conversation_context,
                    progress_notes=_read_ralph_progress(),
                    progress_path=RALPH_PROGRESS_PATH,
                    **_ralph_render_extras(no_change_streak, last_verify),
                )

                # The agent run streams natively through execute_fn.
                await execute_fn(
                    prompt,
                    agent,
                    "ralph",  # display the agent as "Ralph"
                    session_state,
                    token_tracker,
                    backend=backend,
                )

                # The iteration completed (a HITL interrupt would have broken the loop
                # before here). Report it; the trailing markup "Continuing…" line below
                # remains the CLI-only cue between iterations.
                elapsed = (datetime.now(UTC) - iter_start).total_seconds()
                _emit_event(
                    on_event,
                    rev.IterationFinished(
                        iteration=iteration,
                        max_iterations=max_iterations,
                        ok=True,
                        elapsed=elapsed,
                    ),
                    lambda: None,
                )

                iteration += 1

                decision, decision_reason, no_change_streak, last_verify = _ralph_evaluate(
                    working_directory, sig_before, no_change_streak
                )
                if decision in ("complete", "stuck"):
                    done = iteration - 1

                    def _finished_smart(
                        done: int = done,
                        decision: str = decision,
                        reason: str = decision_reason,
                    ) -> None:
                        style = "green" if decision == "complete" else "yellow"
                        label = (
                            "Task complete"
                            if decision == "complete"
                            else "Ralph stopped — no progress"
                        )
                        emit("")
                        emit(f"[{style}]{label}.[/{style}] [dim]{reason}[/dim]")
                        emit(f"[dim]Completed {done} iteration(s).[/dim]")
                        emit("")

                    _emit_event(
                        on_event,
                        rev.RalphFinished(
                            completed=done, failed=0, total=done, reason=decision
                        ),
                        _finished_smart,
                    )
                    return True

                if stop_after_iteration:
                    done = iteration - 1

                    def _finished_stop(done: int = done) -> None:
                        emit("")
                        emit("[green]Finished current iteration. Stopping as requested.[/green]")
                        emit(f"[dim]Completed {done} iteration(s).[/dim]")
                        emit("")

                    _emit_event(
                        on_event,
                        rev.RalphFinished(completed=done, failed=0, total=done, reason="finished"),
                        _finished_stop,
                    )
                    return True

                if (max_iterations == 0 or iteration <= max_iterations) and on_event is None:
                    # CLI-only inter-iteration cue; the TUI shows it via the cards.
                    emit("")
                    emit(f"[dim]Completed iteration {iteration - 1}. Continuing...[/dim]")

        except asyncio.CancelledError:
            done = iteration - 1

            def _cancelled_stop(done: int = done) -> None:
                emit("")
                emit("[yellow]Ralph mode stopped by user.[/yellow]")
                emit(f"[dim]Completed {done} iteration(s).[/dim]")
                emit("")

            _emit_event(
                on_event,
                rev.RalphFinished(completed=done, failed=0, total=done, reason="stopped"),
                _cancelled_stop,
            )
            return True
        except KeyboardInterrupt:
            if on_event is not None:
                done = iteration - 1

                def _tui_stop(done: int = done) -> None:
                    emit("")
                    emit("[yellow]Ralph mode stopped by user.[/yellow]")
                    emit(f"[dim]Completed {done} iteration(s).[/dim]")
                    emit("")

                _emit_event(
                    on_event,
                    rev.RalphFinished(completed=done, failed=0, total=done, reason="stopped"),
                    _tui_stop,
                )
                return True
            interrupted = True

        if interrupted:
            # The console REPL was removed and the TUI always passes ``on_event``
            # (handled above), so this path is unreachable in practice. Default to
            # a clean stop rather than prompting for an action.
            emit("")
            emit("[dim]Ralph mode stopped (no interactive UI available).[/dim]")
            emit(f"[dim]Completed {iteration - 1} iteration(s).[/dim]")
            emit("")
            return True

        done = iteration - 1

        def _finished_ok(done: int = done) -> None:
            emit("")
            emit(f"[green]Ralph mode completed after {done} iteration(s).[/green]")
            emit("")

        _emit_event(
            on_event,
            rev.RalphFinished(completed=done, failed=0, total=done, reason="completed"),
            _finished_ok,
        )

        try:
            session_state.add_notification(
                level="success",
                title="Ralph mode completed",
                message=f"{escape(task[:80])} — {iteration - 1} iteration(s)",
                source="ralph",
            )
        except Exception:
            pass

        return True
    finally:
        session_state.auto_approve = original_auto_approve
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
