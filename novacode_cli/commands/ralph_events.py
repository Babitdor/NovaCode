"""UI-agnostic progress events for the ``/ralph`` command.

The Ralph handler (:mod:`novacode_cli.commands.ralph_handler`) is UI-agnostic:
its user-facing chatter goes through a Rich-markup ``emit`` sink, while the
*structured* milestones of a run (start, each iteration, the final summary, and
``--status`` snapshots) are reported through an optional ``on_event`` callback
that hands a renderer one of these dataclasses.

This mirrors :mod:`novacode_cli.init.events`: the classic REPL leaves
``on_event`` unset and keeps rendering the markup lines, while the Textual TUI
passes an ``on_event`` that drives native widgets (a run header card, live
per-iteration cards, a summary card, and a ``--status`` table) instead of a flat
stream of log lines.

Events flow one-way (handler -> renderer). They are additive: when ``on_event``
is ``None`` the handler falls back to its markup ``emit`` output unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Union


@dataclass
class RalphStarted:
    """A Ralph run began. ``max_iterations`` of 0 means unlimited."""

    task: str
    max_iterations: int
    background: bool = False
    resumed_from: int | None = None


@dataclass
class IterationStarted:
    """One Ralph iteration began. ``max_iterations`` of 0 means unlimited."""

    iteration: int
    max_iterations: int


@dataclass
class IterationFinished:
    """One Ralph iteration finished — ``ok`` distinguishes success from failure."""

    iteration: int
    max_iterations: int
    ok: bool
    elapsed: float
    error: str | None = None


@dataclass
class RalphFinished:
    """A Ralph run ended. ``reason`` is a short outcome label.

    ``reason`` is a machine-readable outcome and ``detail`` explains it to the user.
    """

    completed: int
    failed: int
    total: int
    reason: str = "completed"
    detail: str = ""


@dataclass
class RalphTaskRow:
    """A single background task in a ``--status`` snapshot."""

    iteration: int
    max_iterations: int
    status: str  # "running" | "completed" | "failed"
    task: str
    task_id: str
    elapsed: float
    working_directory: str = ""
    error: str | None = None


@dataclass
class StatusSnapshot:
    """The result of ``/ralph --status`` as plain data for any renderer."""

    rows: list[RalphTaskRow] = field(default_factory=list)
    running: int = 0
    completed: int = 0
    failed: int = 0
    total: int = 0


# Anything the handler may hand to ``on_event``.
RalphEvent = Union[
    RalphStarted,
    IterationStarted,
    IterationFinished,
    RalphFinished,
    StatusSnapshot,
]
RalphEventFn = Callable[[RalphEvent], None]


def null_event(event: RalphEvent) -> None:
    """No-op ``on_event`` for callers that don't want structured progress."""
