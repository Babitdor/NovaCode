"""Inline output verifier — Loop-Engineering Enhancement 1 (Verification Loop).

``ReviewRunner`` grades *out of band* and only benefits future sessions. This
module closes the gap with an **in-turn** grader: after the agent finishes a
task, :meth:`InlineVerifier.grade` runs a single out-of-band model call that
scores the output against a rubric (was the response on-topic, did the promised
file changes actually happen, are there unrecovered errors?). The
:mod:`novacode_cli.core.verification_loop` wrapper consumes that verdict and, on
a failure, re-drives the agent with the verifier's feedback — up to a hard retry
cap so a miscalibrated grader can never loop forever.

The grading call is tagged ``nova_oob=True`` so the agent event loop drops its
streamed output (it must never surface as a "Nova" assistant message — see the
``nova_oob`` filter in ``core/agent_loop.py``).

Fail-open: any error grading, or an unparseable verdict, yields a *passing*
verdict. Verification is a safety net, never a gate that can wedge a turn shut.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage

from novacode_cli.hermes import config
from novacode_cli.prompts import render_template

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langgraph.store.base import BaseStore

    from novacode_cli.memory.store import DualModeStore

logger = logging.getLogger("nova.hermes.verifier")

_PASSED_RE = re.compile(r"<passed>\s*(true|false)\s*</passed>", re.IGNORECASE)
_SCORE_RE = re.compile(r"<score>\s*([0-9]*\.?[0-9]+)\s*</score>", re.IGNORECASE)
_FEEDBACK_RE = re.compile(r"<feedback>(.*?)</feedback>", re.IGNORECASE | re.DOTALL)
_CHECK_RE = re.compile(r'<check\s+name="([^"]+)"\s+result="(pass|fail)"\s*/?>', re.IGNORECASE)
_FILE_OP_ERROR_CHARS = 160


@dataclass
class VerifierVerdict:
    """The grader's decision on one agent attempt."""

    passed: bool
    score: float
    feedback: str
    checks: list[str] = field(default_factory=list)


class InlineVerifier:
    """Grades an agent's task output against a rubric via an out-of-band call.

    Args:
        store: Durable store for logging outcomes (the quality signal Phase 4's
            prompt A/B testing reads). ``None`` disables logging only.
        model: Pre-built chat model (tests inject a fake); otherwise
            ``create_model()`` is used per grade call.
        max_retries: Hard cap on verify→retry cycles, surfaced to the loop.
        enabled: When ``False`` the loop skips grading entirely.
    """

    def __init__(
        self,
        store: BaseStore | DualModeStore | None = None,
        *,
        model: BaseChatModel | None = None,
        max_retries: int = config.INLINE_VERIFIER_MAX_RETRIES,
        enabled: bool = True,
    ) -> None:
        """Store the deps; see the class docstring for argument meanings."""
        self._store = store
        self._model = model
        self.max_retries = max_retries
        self.enabled = enabled

    async def grade(
        self,
        task: str,
        agent_output: str,
        file_ops: list[Any],
        *,
        test_evidence: list[str] | None = None,
        diffs: list[str] | None = None,
    ) -> VerifierVerdict:
        """Score one attempt; returns a passing verdict on any failure (fail-open).

        Args:
            task: The user's original request.
            agent_output: The agent's final textual response.
            file_ops: File-operation records from the turn.
            test_evidence: Raw outputs of any test runs the agent performed, so
                the rubric can grade ``tests_pass`` against real outcomes rather
                than the agent's claims.
            diffs: Unified diffs of the files the agent changed, so the rubric
                can detect reward hacking (edits that weaken tests).
        """
        try:
            from novacode_cli.hermes.test_gaming import detect_test_gaming

            gaming_summary = detect_test_gaming(diffs or []).summarize()
        except Exception:
            logger.exception("Test-gaming detection failed; continuing without it")
            gaming_summary = ""

        # Load the repo's recent failure history so the rubric pays extra
        # attention to recurring patterns ("Verification that Learns").
        repo_root = _derive_repo_root(file_ops)
        memory_summary = ""
        if self._store is not None:
            try:
                from novacode_cli.hermes.verification_memory import load_verification_memory

                memory_summary = await load_verification_memory(self._store, repo_root=repo_root)
            except Exception:
                logger.exception("Failed to load verification memory")

        try:
            prompt = render_template(
                "nova_verify.jinja",
                task=task,
                agent_output=agent_output,
                file_ops=_summarize_file_ops(file_ops),
                test_evidence=test_evidence or [],
                diffs=diffs or [],
                test_gaming=gaming_summary,
                verification_memory=memory_summary,
            )
            model = self._model
            if model is None:
                from novacode_cli.config.model_create import create_model

                model = create_model()
            resp = await model.ainvoke(
                [HumanMessage(content=prompt)],
                config={
                    "run_name": "nova_inline_verify",
                    "tags": ["nova", "hermes", "verify"],
                    # nova_oob: drop this call's streamed output in the agent loop.
                    "metadata": {"nova_oob": True},
                },
            )
            raw = getattr(resp, "content", "")
            content = raw if isinstance(raw, str) else str(raw)
            verdict = _parse_verdict(content)
        except Exception:
            logger.exception("Inline verification grading failed; passing through")
            return VerifierVerdict(
                passed=True, score=1.0, feedback="", checks=["verification-unavailable"]
            )

        # Record a failing verdict into the repo's verification memory so future
        # grades learn from it. Best-effort; never breaks the turn.
        if not verdict.passed and self._store is not None:
            try:
                from novacode_cli.hermes.verification_memory import (
                    record_verification_failure,
                )

                await record_verification_failure(
                    self._store,
                    repo_root=repo_root,
                    failed_checks=verdict.checks,
                    files=_file_paths(file_ops),
                    feedback=verdict.feedback,
                )
            except Exception:
                logger.exception("Failed to record verification failure")

        return verdict

    async def log_outcome(self, thread_id: str, verdict: VerifierVerdict, attempt: int) -> None:
        """Persist a verdict to the verification log (best-effort, never raises)."""
        if self._store is None:
            return
        try:
            key = f"{thread_id}_{int(time.time() * 1000)}"
            await self._store.aput(
                config.VERIFICATION_LOG_NS,
                key,
                {
                    "thread_id": thread_id,
                    "passed": verdict.passed,
                    "score": verdict.score,
                    "attempt": attempt,
                    "timestamp": time.time(),
                },
            )
        except Exception:
            logger.exception("Failed to log verification outcome")


def _summarize_file_ops(file_ops: list[Any]) -> list[str]:
    """Render file-op records into short human-readable lines for the rubric."""
    out: list[str] = []
    for rec in file_ops or []:
        tool = getattr(rec, "tool_name", "?")
        path = getattr(rec, "display_path", "?")
        status = getattr(rec, "status", "?")
        err = getattr(rec, "error", None)
        line = f"{tool} {path} ({status})"
        if err:
            line += f" — error: {str(err)[:_FILE_OP_ERROR_CHARS]}"
        out.append(line)
    return out


def _file_paths(file_ops: list[Any]) -> list[str]:
    """Collect the display paths of the turn's file operations."""
    paths: list[str] = []
    for rec in file_ops or []:
        path = getattr(rec, "display_path", None)
        if path:
            paths.append(str(path))
    return paths


def _derive_repo_root(file_ops: list[Any]) -> str | None:
    """Best-effort derive the repo root from the turn's file-op physical paths.

    Walks up from the first usable absolute file path looking for a ``.git``
    directory (the repo root); falls back to the current working directory.
    Used only to key per-repo verification memory.
    """
    for rec in file_ops or []:
        phys = getattr(rec, "physical_path", None)
        if phys is None:
            continue
        path = _resolve_safe(phys)
        if path is None or not path.is_absolute():
            continue
        # Walk up to find the git root.
        for parent in (path if path.is_dir() else path.parent).parents:
            if (parent / ".git").exists():
                return str(parent)
        return str(path.anchor)  # drive root as a last resort
    try:
        return str(Path.cwd())
    except Exception:  # noqa: BLE001 — best-effort; never break grading
        return None


def _resolve_safe(path: Path) -> Path | None:
    """Resolve a path without raising; returns ``None`` on failure."""
    try:
        return path.resolve()
    except Exception:  # noqa: BLE001 — best-effort; a bad path is skipped
        return None


def _parse_verdict(content: str) -> VerifierVerdict:
    """Parse the ``<verdict>`` block; fail-open if ``<passed>`` is missing."""
    passed_match = _PASSED_RE.search(content)
    # Fail-open: only an explicit <passed>false</passed> fails the attempt.
    passed = passed_match is None or passed_match.group(1).lower() == "true"

    score = 1.0
    score_match = _SCORE_RE.search(content)
    if score_match is not None:
        try:
            score = max(0.0, min(1.0, float(score_match.group(1))))
        except ValueError:
            score = 1.0 if passed else 0.0

    feedback_match = _FEEDBACK_RE.search(content)
    feedback = feedback_match.group(1).strip() if feedback_match else ""

    checks = [f"{name}:{result.lower()}" for name, result in _CHECK_RE.findall(content)]
    return VerifierVerdict(passed=passed, score=score, feedback=feedback, checks=checks)
