"""Collaborative planning council — the place Nova thinks before it acts.

The original council (:mod:`novacode_cli.council`) is a *debate*: five personas
answer a question and vote for a winner. This module is a different thing built
from the same parts — a **planning stage** that runs before any code changes:

    brainstorm -> critique -> anonymous ranked vote -> judge -> user approval

and only then hands a structured, approved plan to the coding agent.

Three properties are the whole point, and each is enforced here rather than left
to prompt wording:

**Council agents never touch the repository.** They are called with
``model.ainvoke`` and nothing else — no tools are bound, so there is no file
write or shell call available to them by construction, not by instruction.

**Evaluation is anonymous.** Proposals are handed out under shuffled ids with
the authoring agent stripped, so critics and voters cannot see (or infer from
ordering) who wrote what. :func:`anonymize` is the only thing that builds the
text critics and voters see.

**Nothing executes without explicit approval.** ``run_planning_council`` ends at
``awaiting_approval``. Turning a plan into an implementation handoff requires a
separate :func:`approve` call, which refuses to act on a run in any other state.

Everything is a plain ``dict``-yielding async generator over injectable
``model``s, so the whole flow is unit-testable without HTTP — the same shape as
:func:`novacode_cli.council.run_council`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from novacode_cli.council import PERSONAS, Persona, _content_text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)


# ── Status ──────────────────────────────────────────────────────────────────

# Plain string constants rather than an Enum: these round-trip through JSON in
# council-state.json, and a str is what both the artifact and the TUI compare.
INITIALIZING = "initializing"
BRAINSTORMING = "brainstorming"
CRITIQUING = "critiquing"
VOTING = "voting"
JUDGING = "judging"
AWAITING_APPROVAL = "awaiting_approval"
APPROVED = "approved"
EXECUTING = "executing"
COMPLETED = "completed"
CANCELLED = "cancelled"
REJECTED = "rejected"
FAILED = "failed"

#: A run in one of these states is finished; it cannot be approved or resumed.
TERMINAL = frozenset({COMPLETED, CANCELLED, REJECTED, FAILED})


# ── Tunables ────────────────────────────────────────────────────────────────

#: Per-agent wall clock for a single model call. A stuck provider must not hang
#: the council: the agent is dropped and the round continues without it.
#:
#: Generous by default because a council turn is not a chat turn — it asks for a
#: whole structured plan, and a hosted reasoning model can spend minutes on one.
#: Set too low, every agent "fails" and the council looks broken when it is only
#: impatient; override with ``NOVA_COUNCIL_TIMEOUT`` (seconds).
AGENT_TIMEOUT = 600.0


def agent_timeout() -> float:
    """Per-call timeout, overridable per environment."""
    raw = os.environ.get("NOVA_COUNCIL_TIMEOUT", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            logger.warning("ignoring non-numeric NOVA_COUNCIL_TIMEOUT=%r", raw)
        else:
            if value > 0:
                return value
    return AGENT_TIMEOUT

#: A comparison needs at least two things to compare. Below this the council
#: has nothing useful to say and says so, rather than rubber-stamping one plan.
MIN_PROPOSALS = 2

#: How many plans the judge surfaces to the user.
MAX_PLANS = 3

#: Judge rubric. Weights are relative — they are normalized before scoring, so
#: callers can pass any scale. Mirrors the dimensions the judge is asked to
#: score, and a judge that invents a dimension has it ignored.
RUBRIC: dict[str, float] = {
    "correctness": 0.25,
    "feasibility": 0.20,
    "simplicity": 0.15,
    "maintainability": 0.15,
    "risk": 0.10,
    "testability": 0.10,
    "ux": 0.05,
}


#: The planning council. Reuses the debate personas — they already encode the
#: diversity of viewpoint this phase depends on — plus a UX voice, which matters
#: for a CLI/TUI product and has no equivalent among the debate five.
PLANNING_PERSONAS: list[Persona] = [
    *PERSONAS,
    Persona(
        id="ux",
        name="The Craftsperson",
        avatar="🎛️",
        color="#5fb3c4",
        system=(
            "You are The Craftsperson. You judge a change by what it is like to "
            "live with: the command surface, the feedback the user gets while "
            "waiting, what happens on the unhappy path, and whether the feature "
            "is discoverable at all. A technically correct plan with a confusing "
            "interface is not done."
        ),
    ),
]


# ── Schemas ─────────────────────────────────────────────────────────────────


@dataclass
class Proposal:
    """One agent's independent plan. ``agent_id`` is never shown to evaluators."""

    id: str
    agent_id: str
    title: str
    summary: str
    problem_understanding: list[str] = field(default_factory=list)
    approach: list[str] = field(default_factory=list)
    implementation_steps: list[str] = field(default_factory=list)
    files_likely_affected: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    # Nested to match the proposal schema agents are asked to emit, so a parsed
    # payload maps onto this dataclass without a translation layer.
    tradeoffs: dict[str, list[str]] = field(
        default_factory=lambda: {"pros": [], "cons": []}
    )
    risks: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    testing_strategy: list[str] = field(default_factory=list)
    confidence: float = 0.5


@dataclass
class Evaluation:
    """One critic's read of one proposal."""

    proposal_id: str
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    technical_risks: list[str] = field(default_factory=list)
    recommended_changes: list[str] = field(default_factory=list)
    score: float = 0.0


@dataclass
class Critique:
    """Everything one critic said, across all proposals it reviewed."""

    critic_id: str
    evaluations: list[Evaluation] = field(default_factory=list)


@dataclass
class Ballot:
    """One voter's ranking of proposal ids, best first.

    ``voter_id`` exists to deduplicate ballots and to debug a run afterwards. It
    is deliberately absent from everything an evaluating agent is shown.
    """

    voter_id: str
    ranking: list[str] = field(default_factory=list)


@dataclass
class SelectedPlan:
    """A judge's pick, with the rubric justification that earned the place."""

    proposal_id: str
    rank: int
    score: float
    reasoning: str = ""
    required_changes: list[str] = field(default_factory=list)


@dataclass
class Judgment:
    """The judge's verdict over the whole candidate set."""

    selected_plans: list[SelectedPlan] = field(default_factory=list)
    rejected_proposals: list[dict[str, str]] = field(default_factory=list)
    confidence: float = 0.5
    #: True when the judge failed twice and the ranking is the voting order.
    fell_back_to_vote: bool = False


@dataclass
class CouncilRun:
    """The full state of one council session — the unit that gets persisted."""

    id: str
    prompt: str
    status: str = INITIALIZING
    context: str = ""
    proposals: list[Proposal] = field(default_factory=list)
    critiques: list[Critique] = field(default_factory=list)
    votes: list[Ballot] = field(default_factory=list)
    tally: dict[str, Any] = field(default_factory=dict)
    judgment: Judgment | None = None
    approved_proposal_id: str | None = None
    approved_plan: str = ""
    revision_notes: list[str] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CouncilRun:
        """Rebuild a run from ``council-state.json``.

        Tolerant by design: an artifact written by an older Nova is still worth
        showing, so unknown keys are ignored and missing ones take defaults.
        """
        run = cls(id=data.get("id", ""), prompt=data.get("prompt", ""))
        run.status = data.get("status", INITIALIZING)
        run.context = data.get("context", "")
        run.proposals = [_load(Proposal, p) for p in data.get("proposals", [])]
        run.critiques = [
            Critique(
                critic_id=c.get("critic_id", ""),
                evaluations=[
                    _load(Evaluation, e) for e in c.get("evaluations", [])
                ],
            )
            for c in data.get("critiques", [])
        ]
        run.votes = [_load(Ballot, v) for v in data.get("votes", [])]
        run.tally = data.get("tally", {}) or {}
        judgment = data.get("judgment")
        if judgment:
            run.judgment = Judgment(
                selected_plans=[
                    _load(SelectedPlan, s)
                    for s in judgment.get("selected_plans", [])
                ],
                rejected_proposals=judgment.get("rejected_proposals", []) or [],
                confidence=float(judgment.get("confidence", 0.5) or 0.5),
                fell_back_to_vote=bool(judgment.get("fell_back_to_vote", False)),
            )
        run.approved_proposal_id = data.get("approved_proposal_id")
        run.approved_plan = data.get("approved_plan", "")
        run.revision_notes = data.get("revision_notes", []) or []
        run.failures = data.get("failures", []) or []
        run.created_at = data.get("created_at", "")
        run.updated_at = data.get("updated_at", "")
        return run

    # -- lookup --------------------------------------------------------------

    def proposal(self, proposal_id: str) -> Proposal | None:
        return next((p for p in self.proposals if p.id == proposal_id), None)

    def ranked_plans(self) -> list[SelectedPlan]:
        """The judge's picks, best first. Empty until judging completes."""
        if not self.judgment:
            return []
        return sorted(self.judgment.selected_plans, key=lambda s: s.rank)


def _load(cls: type, data: dict[str, Any]) -> Any:
    """Build a dataclass from a dict, dropping keys the class doesn't declare."""
    fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in fields})


# ── Small helpers ───────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).isoformat()


def new_run_id() -> str:
    """A sortable, collision-resistant run id: ``council-20260830-141207-a3f1``."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"council-{stamp}-{uuid.uuid4().hex[:4]}"


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply.

    Models wrap JSON in prose or code fences even when told not to, so this
    takes the outermost ``{...}`` span. Returns None when there is nothing
    parseable — callers treat that as a failed turn and retry once.
    """
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _strs(value: Any, limit: int = 12) -> list[str]:
    """Coerce a model-supplied field to a bounded list of non-empty strings."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = [str(v).strip() for v in value if str(v).strip()]
    return out[:limit]


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


async def _ask_json(
    model: Any, system: str, user: str, *, timeout: float | None = None
) -> tuple[dict[str, Any] | None, str]:
    """One model call expected to return JSON, retried once. Never raises.

    Returns ``(parsed, reason)`` — ``reason`` is empty on success and otherwise
    says why the turn produced nothing. The reason is not decoration: a council
    that silently drops agents reports "3 members dropped out" and leaves the
    user with no idea whether their model is slow, unreachable, or simply bad at
    emitting JSON, which are three different fixes.

    A single retry is the whole recovery policy (a malformed reply is usually a
    formatting slip, and a second failure means this agent is not going to
    produce usable structure this round). Cancellation propagates — a cancelled
    council must actually stop.
    """
    limit = timeout if timeout is not None else agent_timeout()
    reason = "no reply"
    for attempt in (1, 2):
        try:
            reply = await asyncio.wait_for(
                model.ainvoke([("system", system), ("human", user)]), limit
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            reason = (
                f"timed out after {limit:.0f}s — a slow reasoning model may need "
                "NOVA_COUNCIL_TIMEOUT raised"
            )
            logger.debug("council model call timed out (attempt %d)", attempt)
            continue
        except Exception as exc:  # noqa: BLE001 — one agent must not end the council
            reason = f"{type(exc).__name__}: {exc}"[:200]
            logger.debug("council model call failed (attempt %d)", attempt, exc_info=True)
            continue
        text = _content_text(reply)
        parsed = _extract_json(text)
        if parsed is not None:
            return parsed, ""
        reason = (
            "replied without JSON"
            if text.strip()
            else "replied with empty content"
        )
        logger.debug("council reply was not JSON (attempt %d)", attempt)
    return None, reason


# ── Anonymization ───────────────────────────────────────────────────────────

_IDENTITY_FIELDS = ("agent_id",)


def anonymize(proposals: Iterable[Proposal]) -> str:
    """Render proposals for critics and voters with every identity cue removed.

    This is the ONLY function that builds the text an evaluating agent sees, so
    the anonymity guarantee lives in one testable place. It strips the authoring
    agent and presents proposals in shuffled order — generation order is itself
    an identity cue once a reader knows the council's fixed persona sequence.
    """
    shuffled = list(proposals)
    random.shuffle(shuffled)
    blocks: list[str] = []
    for proposal in shuffled:
        body = {
            k: v
            for k, v in asdict(proposal).items()
            if k not in _IDENTITY_FIELDS
        }
        blocks.append(json.dumps(body, indent=2, ensure_ascii=False))
    return "\n\n".join(blocks)


# ── Voting ──────────────────────────────────────────────────────────────────


def tally_ranked(
    ballots: list[Ballot], candidates: list[str]
) -> dict[str, Any]:
    """Score ranked ballots with a Borda count, plus disagreement metrics.

    Borda rather than instant-runoff: the judge wants a ranked *candidate set*,
    not a single winner, and IRV discards everything below the final two. A
    ballot's ``i``-th choice earns ``len(candidates) - i`` points, so a proposal
    that is everyone's solid second beats one that is half the council's first
    and half's last — which is the outcome worth surfacing here.

    Returns first-choice counts, preference scores, the full rank distribution,
    and a normalized entropy where 0.0 is unanimous and 1.0 is a dead split.
    """
    n = len(candidates)
    scores = dict.fromkeys(candidates, 0)
    firsts = dict.fromkeys(candidates, 0)
    distribution: dict[str, list[int]] = {c: [0] * n for c in candidates}

    for ballot in ballots:
        seen: set[str] = set()
        position = 0
        for choice in ballot.ranking:
            # Ignore unknown ids and repeats: a malformed ballot should lose the
            # bad entries, not the whole vote.
            if choice not in scores or choice in seen:
                continue
            seen.add(choice)
            scores[choice] += n - position
            if position == 0:
                firsts[choice] += 1
            if position < n:
                distribution[choice][position] += 1
            position += 1

    order = sorted(candidates, key=lambda c: (-scores[c], -firsts[c], c))
    total_firsts = sum(firsts.values())
    share = {
        c: (firsts[c] / total_firsts if total_firsts else 0.0) for c in candidates
    }
    return {
        "order": order,
        "scores": scores,
        "first_choice": firsts,
        "first_choice_share": share,
        "distribution": distribution,
        "ballots": len(ballots),
        "entropy": _entropy(list(firsts.values())),
    }


def _entropy(counts: list[int]) -> float:
    """Normalized Shannon entropy of a first-choice split (0 = unanimous)."""
    total = sum(counts)
    if total <= 0 or len(counts) < 2:
        return 0.0
    probabilities = [c / total for c in counts if c > 0]
    if len(probabilities) < 2:
        return 0.0
    raw = -sum(p * math.log(p) for p in probabilities)
    return raw / math.log(len(counts))


# ── Prompts ─────────────────────────────────────────────────────────────────

_PROPOSAL_SCHEMA = """{
  "title": "<short name for the approach>",
  "summary": "<2-3 sentences a reviewer could act on>",
  "problem_understanding": ["<what the task actually requires>"],
  "approach": ["<the shape of the solution>"],
  "implementation_steps": ["<ordered, concrete steps>"],
  "files_likely_affected": ["<path or module>"],
  "dependencies": ["<library, service, or existing component relied on>"],
  "tradeoffs": {"pros": ["..."], "cons": ["..."]},
  "risks": ["<what could break, and where>"],
  "assumptions": ["<what you assumed because it wasn't stated>"],
  "testing_strategy": ["<how this gets verified>"],
  "confidence": 0.0
}"""

_BRAINSTORM_GUIDE = (
    "\n\nYou are one member of a planning council. Every member is planning the "
    "same task independently — you cannot see anyone else's plan, so give a "
    "COMPLETE plan rather than one angle, and never refer to another member.\n"
    "\n"
    "You are planning, NOT implementing. You cannot edit files or run commands; "
    "another agent will execute whichever plan the user approves. So the plan "
    "has to be precise enough for someone else to follow without you.\n"
    "\n"
    "Be specific and falsifiable: name the actual modules, the actual failure "
    "mode, the actual tradeoff. State assumptions explicitly instead of "
    "smoothing over what the request left unsaid, and say what would break. "
    "`confidence` is your own calibrated 0-1 estimate that this plan survives "
    "contact with the codebase; it is recorded, not used to pick a winner, so "
    "an honest low number costs you nothing.\n"
    "\n"
    "Reply with ONLY this JSON object and nothing else:\n" + _PROPOSAL_SCHEMA
)

_CRITIQUE_GUIDE = (
    "\n\nSeveral plans for the same task are below, anonymized. You do not know "
    "who wrote any of them, and one may be your own — judge them all the same "
    "way.\n"
    "\n"
    "Evaluate on: correctness, feasibility, complexity, maintainability, fit "
    "with the existing code, testability, security, UX, implementation risk, "
    "migration cost, and reversibility. Do NOT comment on writing quality, "
    "length, or tone.\n"
    "\n"
    "The useful output is what a plan MISSES — an unhandled case, an unstated "
    "assumption, a step that will not work as written. `score` is 0-10 on "
    "overall merit.\n"
    "\n"
    "Reply with ONLY this JSON and nothing else:\n"
    '{"evaluations":[{"proposal_id":"<id>","strengths":["..."],'
    '"weaknesses":["..."],"missing_information":["..."],'
    '"technical_risks":["..."],"recommended_changes":["..."],"score":0.0}]}'
)

_VOTE_GUIDE = (
    "\n\nRank the plans below from best to worst. They are anonymous; you do "
    "not know who wrote them, and one may be your own.\n"
    "\n"
    "Rank by which plan you would want actually built if you had to live with "
    "the result — not which most resembles your own instincts, and not which "
    "sounds most thorough. A plan that admits a real limitation beats one that "
    "hides it.\n"
    "\n"
    "Include every proposal id exactly once, best first. Reply with ONLY this "
    'JSON and nothing else:\n{"ranking":["<proposal_id>","<proposal_id>"]}'
)


def _judge_guide(rubric: dict[str, float], max_plans: int) -> str:
    weights = "\n".join(
        f"  - {dim}: {int(round(w * 100))}%" for dim, w in rubric.items()
    )
    return (
        "You are the Judge of a planning council. You are a technical review "
        "board, not a fan: you decide which plans are strongest against a fixed "
        "rubric, and you must justify each placement against that rubric.\n"
        "\n"
        "Rubric (weights):\n" + weights + "\n"
        "\n"
        "You receive the plans, the council's critiques of them, and the result "
        "of an anonymous ranked vote. The vote is evidence about the plans, not "
        "an instruction — you may place a plan the council under-ranked, but "
        "then say what the rubric sees that the vote missed.\n"
        "\n"
        f"Select exactly {max_plans} plans, ranked best first — the user is "
        "choosing BETWEEN them, so one pick is not an answer. `score` is out "
        "of 10 (e.g. 8.4), not a fraction of 1. `reasoning` must name the "
        "rubric dimensions that decided the placement and what the plan does "
        "about them — 'it is cleaner' or 'I prefer it' is not a reason. Use "
        "`required_changes` for what must be fixed before this plan is safe to "
        "build. Every plan you do not select goes in `rejected_proposals` with "
        "a reason.\n"
        "\n"
        "Reply with ONLY this JSON and nothing else:\n"
        '{"selected_plans":[{"proposal_id":"<id>","rank":1,"score":0.0,'
        '"reasoning":"...","required_changes":["..."]}],'
        '"rejected_proposals":[{"proposal_id":"<id>","reason":"..."}],'
        '"confidence":0.0}'
    )


# ── Phases ──────────────────────────────────────────────────────────────────


def _task_block(prompt: str, context: str, revisions: list[str]) -> str:
    block = f"The task before the council:\n\n{prompt}\n"
    if context:
        block += f"\nRepository context:\n\n{context}\n"
    if revisions:
        joined = "\n".join(f"- {note}" for note in revisions)
        block += (
            "\nThe user reviewed an earlier round of plans and asked for these "
            f"changes — they take priority over your own preferences:\n{joined}\n"
        )
    return block


async def _brainstorm_one(
    persona: Persona, run: CouncilRun, model: Any, index: int
) -> tuple[Proposal | None, str]:
    """One agent's plan, plus why it produced none when it failed."""
    user = _task_block(run.prompt, run.context, run.revision_notes) + (
        "\nProduce your own independent plan."
    )
    data, reason = await _ask_json(
        model, persona.system + _BRAINSTORM_GUIDE, user
    )
    if data is None:
        return None, reason
    tradeoffs = data.get("tradeoffs")
    if not isinstance(tradeoffs, dict):
        tradeoffs = {}
    title = str(data.get("title", "")).strip()
    summary = str(data.get("summary", "")).strip()
    if not title and not summary:
        return None, "returned JSON with no plan in it"
    return Proposal(
        # Ids are assigned by position in a shuffled list by the caller; this
        # placeholder is replaced there.
        id=f"proposal-{index:02d}",
        agent_id=persona.id,
        title=title or "Untitled plan",
        summary=summary,
        problem_understanding=_strs(data.get("problem_understanding")),
        approach=_strs(data.get("approach")),
        implementation_steps=_strs(data.get("implementation_steps"), limit=30),
        files_likely_affected=_strs(data.get("files_likely_affected"), limit=30),
        dependencies=_strs(data.get("dependencies")),
        tradeoffs={
            "pros": _strs(tradeoffs.get("pros")),
            "cons": _strs(tradeoffs.get("cons")),
        },
        risks=_strs(data.get("risks")),
        assumptions=_strs(data.get("assumptions")),
        testing_strategy=_strs(data.get("testing_strategy")),
        confidence=_clamp(data.get("confidence"), 0.0, 1.0, 0.5),
    ), ""


async def _critique_one(
    persona: Persona, run: CouncilRun, anonymized: str, model: Any, valid: set[str]
) -> tuple[Critique | None, str]:
    user = (
        _task_block(run.prompt, run.context, run.revision_notes)
        + "\nThe plans to evaluate:\n\n"
        + anonymized
    )
    data, reason = await _ask_json(
        model, persona.system + _CRITIQUE_GUIDE, user
    )
    if data is None:
        return None, reason
    evaluations: list[Evaluation] = []
    seen: set[str] = set()
    for raw in data.get("evaluations", []) or []:
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("proposal_id", "")).strip()
        if pid not in valid or pid in seen:
            continue
        seen.add(pid)
        evaluations.append(
            Evaluation(
                proposal_id=pid,
                strengths=_strs(raw.get("strengths")),
                weaknesses=_strs(raw.get("weaknesses")),
                missing_information=_strs(raw.get("missing_information")),
                technical_risks=_strs(raw.get("technical_risks")),
                recommended_changes=_strs(raw.get("recommended_changes")),
                score=_clamp(raw.get("score"), 0.0, 10.0, 0.0),
            )
        )
    if not evaluations:
        return None, "scored no known proposal"
    return Critique(critic_id=persona.id, evaluations=evaluations), ""


async def _ballot_one(
    persona: Persona, run: CouncilRun, anonymized: str, model: Any, valid: set[str]
) -> tuple[Ballot | None, str]:
    user = (
        _task_block(run.prompt, run.context, run.revision_notes)
        + "\nThe plans to rank:\n\n"
        + anonymized
    )
    data, reason = await _ask_json(
        model, persona.system + _VOTE_GUIDE, user
    )
    if data is None:
        return None, reason
    ranking: list[str] = []
    for choice in data.get("ranking", []) or []:
        pid = str(choice).strip()
        if pid in valid and pid not in ranking:
            ranking.append(pid)
    if not ranking:
        return None, "ranked nothing recognisable"
    return Ballot(voter_id=persona.id, ranking=ranking), ""


def _critique_digest(run: CouncilRun) -> str:
    """Critiques folded per proposal, so the judge reads by plan, not by critic."""
    by_proposal: dict[str, list[Evaluation]] = {}
    for critique in run.critiques:
        for evaluation in critique.evaluations:
            by_proposal.setdefault(evaluation.proposal_id, []).append(evaluation)

    lines: list[str] = []
    for pid, evaluations in by_proposal.items():
        scores = [e.score for e in evaluations]
        mean = sum(scores) / len(scores) if scores else 0.0
        lines.append(f"\n=== {pid} — mean critic score {mean:.1f}/10 ===")
        for evaluation in evaluations:
            # Critic identity is withheld here too: the judge should weigh the
            # substance of an objection, not which persona raised it.
            for label, items in (
                ("weakness", evaluation.weaknesses),
                ("missing", evaluation.missing_information),
                ("risk", evaluation.technical_risks),
                ("change", evaluation.recommended_changes),
            ):
                for item in items:
                    lines.append(f"  [{label}] {item}")
    return "\n".join(lines)


def _vote_digest(tally: dict[str, Any]) -> str:
    lines = [
        f"Ballots cast: {tally.get('ballots', 0)}",
        f"Disagreement (0 unanimous, 1 split): {tally.get('entropy', 0.0):.2f}",
        "",
    ]
    for pid in tally.get("order", []):
        lines.append(
            f"  {pid}: preference score {tally['scores'][pid]}, "
            f"first choices {tally['first_choice'][pid]}"
        )
    return "\n".join(lines)


async def _judge(
    run: CouncilRun, model: Any, rubric: dict[str, float], max_plans: int,
    perspective: str = "",
) -> Judgment:
    """Score the candidate set. Falls back to the vote order if the judge fails.

    Spec'd behaviour on judge failure is to hand the user the ranked vote and
    let them choose, rather than to fail the whole run — the council's work is
    still worth reading without a judge's summary of it.
    """
    valid = {p.id for p in run.proposals}
    user = (
        _task_block(run.prompt, run.context, run.revision_notes)
        + "\nThe plans:\n\n"
        + anonymize(run.proposals)
        + "\n\nCouncil critiques:\n"
        + _critique_digest(run)
        + "\n\nAnonymous ranked vote:\n"
        + _vote_digest(run.tally)
    )
    wanted = min(max_plans, len(run.proposals))
    guide = _judge_guide(rubric, wanted)
    if perspective:
        guide += (
            "\n\nAdditional judge perspective (retain the required rubric and JSON format):\n"
            + perspective
        )
    data, _reason = await _ask_json(model, guide, user)

    selected: list[SelectedPlan] = []
    if data is not None:
        seen: set[str] = set()
        for raw in data.get("selected_plans", []) or []:
            if not isinstance(raw, dict):
                continue
            pid = str(raw.get("proposal_id", "")).strip()
            if pid not in valid or pid in seen:
                continue
            seen.add(pid)
            selected.append(
                SelectedPlan(
                    proposal_id=pid,
                    rank=len(selected) + 1,  # renumbered: trust order, not label
                    score=_clamp(raw.get("score"), 0.0, 10.0, 0.0),
                    reasoning=str(raw.get("reasoning", "")).strip(),
                    required_changes=_strs(raw.get("required_changes")),
                )
            )
            if len(selected) >= max_plans:
                break

    if selected:
        # A judge that returns one pick defeats the point — the user is choosing
        # BETWEEN plans. Observed live: a real judge returned a single plan
        # despite the prompt. Backfill from the council's vote order so there is
        # always a full slate to compare, however the judge behaves.
        chosen = {s.proposal_id for s in selected}
        for pid in run.tally.get("order", [p.id for p in run.proposals]):
            if len(selected) >= wanted:
                break
            if pid in chosen or pid not in valid:
                continue
            selected.append(
                SelectedPlan(
                    proposal_id=pid,
                    rank=len(selected) + 1,
                    score=0.0,
                    reasoning=(
                        "The judge did not rank this plan; it is shown because "
                        "the council's vote placed it here and you should see "
                        "the alternatives you are choosing between."
                    ),
                )
            )
            chosen.add(pid)

        _rescale_scores(selected)
        rejected = [
            {
                "proposal_id": str(r.get("proposal_id", "")),
                "reason": str(r.get("reason", "")),
            }
            for r in (data or {}).get("rejected_proposals", []) or []
            if isinstance(r, dict) and str(r.get("proposal_id", "")) in valid
            and str(r.get("proposal_id", "")) not in chosen
        ]
        return Judgment(
            selected_plans=selected,
            rejected_proposals=rejected,
            confidence=_clamp((data or {}).get("confidence"), 0.0, 1.0, 0.5),
        )

    # Fallback: the council's own ranking, presented as exactly that.
    order = run.tally.get("order", [p.id for p in run.proposals])
    return Judgment(
        selected_plans=[
            SelectedPlan(
                proposal_id=pid,
                rank=i,
                score=0.0,
                reasoning=(
                    "The judge could not be reached, so this ordering is the "
                    "council's anonymous ranked vote, not a rubric assessment."
                ),
            )
            for i, pid in enumerate(order[:max_plans], 1)
        ],
        confidence=0.0,
        fell_back_to_vote=True,
    )


# ── Orchestration ───────────────────────────────────────────────────────────


def _rescale_scores(selected: list[SelectedPlan]) -> None:
    """Normalize a judge that scored 0-1 onto the 0-10 scale the UI renders.

    Observed live: a judge returned ``0.88`` meaning "excellent", which the UI
    would print as "0.9/10" — the opposite reading. Every real score sitting at
    or below 1.0 is the tell; a genuine slate where the best plan scores under
    1/10 is not a slate anyone would be asked to choose from.
    """
    scored = [s.score for s in selected if s.score > 0]
    if scored and max(scored) <= 1.0:
        for plan in selected:
            plan.score = round(plan.score * 10, 1)


def _failure_summary(run: CouncilRun) -> str:
    """Turn recorded agent failures into one actionable sentence.

    "The model may be unavailable or refusing JSON" is a guess that helps
    nobody. The agents already know which of those happened, so say it — a
    timeout and a model that cannot emit JSON need different fixes.
    """
    reasons = [f.get("reason", "") for f in run.failures if f.get("reason")]
    if not reasons:
        return "No agent reported why."
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    top, hits = max(counts.items(), key=lambda kv: kv[1])
    return f"{hits} of {len(reasons)} failures: {top}."


async def _as_done(
    council: list[Persona], work: Callable[[Persona], Awaitable[Any]]
) -> AsyncIterator[tuple[Persona, Any]]:
    """Run ``work`` for every persona at once; yield each ``(persona, result)`` as it lands.

    Concurrent like ``gather`` (a round costs one agent's latency), but the TUI
    can tick an agent off when it finishes instead of all of them at the end.
    """
    tasks = {asyncio.ensure_future(work(p)): p for p in council}
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                yield tasks[task], task.result()
    finally:
        for task in pending:  # the caller stopped early (cancelled run)
            task.cancel()


def _event(name: str, run: CouncilRun, /, **payload: Any) -> dict[str, Any]:
    """One event on the council stream.

    Every event carries the ``run_id`` so a TUI, a log, and a future web client
    can all reconstruct a single run from an interleaved stream.

    ``name``/``run`` are positional-only so a payload key may be called ``name``
    (agents have one) without colliding with the parameter.
    """
    return {
        "event": name,
        "run_id": run.id,
        "timestamp": _now(),
        "status": run.status,
        "payload": payload,
    }


async def run_planning_council(
    prompt: str,
    model: Any,
    *,
    context: str = "",
    members: list[Persona] | None = None,
    store: CouncilArtifactStore | None = None,
    rubric: dict[str, float] | None = None,
    max_plans: int = MAX_PLANS,
    run: CouncilRun | None = None,
    member_models: dict[str, Any] | None = None,
    judge_model: Any = None,  # noqa: ANN401 - Provider model protocol, like the main model.
    judge_perspective: str = "",
) -> AsyncIterator[dict[str, Any]]:
    """Run the planning council and yield events.

    Ends at ``awaiting_approval`` — deliberately. Producing an implementation
    handoff is :func:`approve`, a separate call the user has to trigger, so
    there is no path through this function that starts changing code.

    Pass an existing *run* (with ``revision_notes`` appended) to re-plan after
    the user asks for a revision.
    """
    council = members or PLANNING_PERSONAS
    models = dict(member_models or {})
    rubric = rubric or RUBRIC
    if run is None:
        run = CouncilRun(id=new_run_id(), prompt=prompt, context=context)
        run.created_at = _now()
    else:
        # A revision round replans from scratch; last round's artifacts stay on
        # disk but must not be mistaken for this round's.
        run.proposals, run.critiques, run.votes = [], [], []
        run.tally, run.judgment = {}, None
        run.failures = []
        # A run handed in by a caller (a revision) may never have been stamped.
        run.created_at = run.created_at or _now()

    def _save() -> None:
        run.updated_at = _now()
        if store is not None:
            store.save(run)

    run.status = INITIALIZING
    _save()
    yield _event(
        "council.started",
        run,
        prompt=prompt,
        agents=[
            {"id": p.id, "name": p.name, "avatar": p.avatar, "color": p.color}
            for p in council
        ],
    )

    # --- Phase 1: independent brainstorming ---------------------------------
    run.status = BRAINSTORMING
    yield _event("council.phase.started", run, phase=BRAINSTORMING)
    for persona in council:
        yield _event("agent.started", run, agent_id=persona.id, name=persona.name)

    # Concurrent: the plans are independent by design, so there is nothing to
    # serialize and a 6-agent round costs one agent's latency, not six.
    position = {persona.id: i for i, persona in enumerate(council, 1)}
    results: dict[str, Any] = {}
    async for persona, (result, reason) in _as_done(
        council, lambda p: _brainstorm_one(p, run, models.get(p.id, model), position[p.id])
    ):
        results[persona.id] = result
        if result is None:
            run.failures.append(
                {"agent_id": persona.id, "phase": BRAINSTORMING, "reason": reason}
            )
            yield _event(
                "agent.failed", run, agent_id=persona.id, name=persona.name,
                phase=BRAINSTORMING, reason=reason,
            )
        else:
            yield _event("agent.completed", run, agent_id=persona.id, phase=BRAINSTORMING)

    produced = [(p, results[p.id]) for p in council if results[p.id] is not None]

    # Ids are assigned over a shuffled list so a proposal's number carries no
    # information about which persona produced it.
    shuffled = list(produced)
    random.shuffle(shuffled)
    for index, (_persona, proposal) in enumerate(shuffled, 1):
        proposal.id = f"proposal-{index:02d}"
        run.proposals.append(proposal)
    run.proposals.sort(key=lambda p: p.id)
    _save()

    for proposal in run.proposals:
        yield _event(
            "proposal.created", run, proposal_id=proposal.id, title=proposal.title
        )

    if len(run.proposals) < MIN_PROPOSALS:
        run.status = FAILED
        _save()
        yield _event(
            "council.failed",
            run,
            reason=(
                f"Only {len(run.proposals)} plan(s) came back — the council "
                f"needs at least {MIN_PROPOSALS} to compare. "
                + _failure_summary(run)
            ),
        )
        return

    valid_ids = {p.id for p in run.proposals}
    # Built once and reused for critique and voting: both phases must see the
    # same anonymized text, and building it twice would reshuffle it.
    anonymized = anonymize(run.proposals)

    # --- Phase 2: critique --------------------------------------------------
    run.status = CRITIQUING
    yield _event("council.phase.started", run, phase=CRITIQUING)
    async for persona, (critique, reason) in _as_done(
        council, lambda p: _critique_one(p, run, anonymized, models.get(p.id, model), valid_ids)
    ):
        if critique is None:
            run.failures.append(
                {"agent_id": persona.id, "phase": CRITIQUING, "reason": reason}
            )
            yield _event(
                "agent.failed", run, agent_id=persona.id, name=persona.name,
                phase=CRITIQUING, reason=reason,
            )
            continue
        run.critiques.append(critique)
        yield _event(
            "proposal.critiqued",
            run,
            critic_id=persona.id,
            reviewed=len(critique.evaluations),
        )
    _save()
    yield _event("critique.completed", run, critics=len(run.critiques))

    # --- Phase 3: anonymous ranked voting -----------------------------------
    run.status = VOTING
    yield _event("council.phase.started", run, phase=VOTING)
    async for persona, (ballot, reason) in _as_done(
        council, lambda p: _ballot_one(p, run, anonymized, models.get(p.id, model), valid_ids)
    ):
        if ballot is None:
            run.failures.append(
                {"agent_id": persona.id, "phase": VOTING, "reason": reason}
            )
            continue
        run.votes.append(ballot)
        # No voter identity and no ranking in the event: the TUI renders progress
        # during voting, and leaking a ballot as it lands would let a viewer
        # reconstruct who voted for what.
        yield _event("vote.submitted", run, cast=len(run.votes))

    run.tally = tally_ranked(run.votes, [p.id for p in run.proposals])
    _save()
    yield _event("vote.tallied", run, tally=run.tally)

    # --- Phase 4: judging ---------------------------------------------------
    run.status = JUDGING
    yield _event("council.phase.started", run, phase=JUDGING)
    yield _event("judge.started", run)
    run.judgment = await _judge(
        run, judge_model if judge_model is not None else model, rubric, max_plans, judge_perspective
    )
    _save()
    yield _event(
        "judge.completed",
        run,
        confidence=run.judgment.confidence,
        fell_back_to_vote=run.judgment.fell_back_to_vote,
    )

    # --- Phase 5: present, then stop and wait -------------------------------
    run.status = AWAITING_APPROVAL
    _save()
    if store is not None:
        store.write_selected_plans(run)
    yield _event(
        "plans.selected",
        run,
        plans=[asdict(s) for s in run.ranked_plans()],
        entropy=run.tally.get("entropy", 0.0),
    )
    yield _event("approval.requested", run, count=len(run.ranked_plans()))


# ── Approval and handoff ────────────────────────────────────────────────────


class CouncilApprovalError(RuntimeError):
    """Raised when an approval is attempted that the run's state forbids."""


def approve(
    run: CouncilRun, choice: int, *, store: CouncilArtifactStore | None = None
) -> str:
    """Approve the *choice*-th presented plan (1-based) and build the handoff.

    This is the authorization boundary. It refuses any run that is not sitting
    at ``awaiting_approval``, which is what stops a cancelled, failed, or
    already-approved run from being replayed into an execution.
    """
    if run.status != AWAITING_APPROVAL:
        raise CouncilApprovalError(
            f"Council run {run.id} is '{run.status}', not awaiting approval."
        )
    plans = run.ranked_plans()
    if not 1 <= choice <= len(plans):
        raise CouncilApprovalError(
            f"Choose a plan between 1 and {len(plans)}; got {choice}."
        )
    selected = plans[choice - 1]
    proposal = run.proposal(selected.proposal_id)
    if proposal is None:  # pragma: no cover — judge output is id-validated
        raise CouncilApprovalError(f"Plan {selected.proposal_id} is missing.")

    run.approved_proposal_id = proposal.id
    run.approved_plan = to_implementation_plan(run, proposal, selected)
    run.status = APPROVED
    run.updated_at = _now()
    if store is not None:
        store.save(run)
        store.write_approved_plan(run)
    return run.approved_plan


def reject(run: CouncilRun, *, store: CouncilArtifactStore | None = None) -> None:
    """End the session with no implementation."""
    run.status = REJECTED
    run.updated_at = _now()
    if store is not None:
        store.save(run)


def cancel(run: CouncilRun, *, store: CouncilArtifactStore | None = None) -> None:
    run.status = CANCELLED
    run.updated_at = _now()
    if store is not None:
        store.save(run)


def mark_executing(
    run: CouncilRun, *, store: CouncilArtifactStore | None = None
) -> None:
    """Record that the approved plan was handed to the coding agent.

    Written before the handoff rather than after, so a run interrupted during
    implementation is distinguishable afterwards from one that was approved and
    never started.
    """
    run.status = EXECUTING
    run.updated_at = _now()
    if store is not None:
        store.save(run)


def mark_completed(
    run: CouncilRun, *, store: CouncilArtifactStore | None = None
) -> None:
    """Close the run once the coding agent's turn on the plan has finished."""
    run.status = COMPLETED
    run.updated_at = _now()
    if store is not None:
        store.save(run)


def request_revision(
    run: CouncilRun, notes: str, *, store: CouncilArtifactStore | None = None
) -> CouncilRun:
    """Record what the user wants changed and reopen the run for another round."""
    note = notes.strip()
    if note:
        run.revision_notes.append(note)
    run.status = INITIALIZING
    run.updated_at = _now()
    if store is not None:
        store.save(run)
    return run


def to_implementation_plan(
    run: CouncilRun, proposal: Proposal, selected: SelectedPlan | None = None
) -> str:
    """Render the approved proposal as the coding agent's brief.

    The coding agent gets this, not the transcript: the deliberation was how the
    plan was chosen, and replaying five rejected alternatives into the executor's
    context invites it to relitigate a decision the user already made. What does
    carry over is the judge's ``required_changes`` and the plan's own risks and
    assumptions — those are conditions on the work, not debate.
    """
    def _section(title: str, items: list[str], bullet: str = "-") -> str:
        if not items:
            return ""
        body = "\n".join(f"{bullet} {item}" for item in items)
        return f"\n## {title}\n\n{body}\n"

    out = [
        "# Implementation Plan",
        "",
        f"> Approved by the user from council run `{run.id}`.",
        "> This plan was selected over "
        f"{max(len(run.proposals) - 1, 0)} alternative(s) after critique and an "
        "anonymous ranked vote.",
        "",
        "## Objective",
        "",
        run.prompt.strip(),
        "",
        f"## Approved Approach — {proposal.title}",
        "",
        proposal.summary,
    ]
    text = "\n".join(out) + "\n"
    text += _section("Problem Understanding", proposal.problem_understanding)
    text += _section("Approach", proposal.approach)

    if proposal.implementation_steps:
        steps = "\n".join(f"- [ ] {s}" for s in proposal.implementation_steps)
        text += f"\n## Implementation Tasks\n\n{steps}\n"

    text += _section("Files Likely Affected", proposal.files_likely_affected)
    text += _section("Dependencies", proposal.dependencies)

    if selected and selected.required_changes:
        text += _section(
            "Required Changes (from the judge — do these)",
            selected.required_changes,
        )
    if run.revision_notes:
        text += _section("User Revisions", run.revision_notes)

    text += _section("Risks", proposal.risks)
    text += _section("Assumptions", proposal.assumptions)
    text += _section("Testing Strategy", proposal.testing_strategy)

    if selected and selected.reasoning:
        text += f"\n## Why This Plan\n\n{selected.reasoning}\n"

    known = proposal.tradeoffs.get("cons") or []
    if known:
        text += _section("Accepted Tradeoffs", known)
    return text


# ── Artifacts ───────────────────────────────────────────────────────────────


class CouncilArtifactStore:
    """Persists a council run under ``<root>/.nova/council/<run-id>/``.

    Written after every phase rather than once at the end, so a council
    interrupted mid-run (a crash, a closed terminal) can still be inspected and
    approved afterwards instead of being lost.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        base = Path(root) if root is not None else Path.cwd()
        self.base = base / ".nova" / "council"

    def run_dir(self, run_id: str) -> Path:
        return self.base / run_id

    def save(self, run: CouncilRun) -> Path:
        """Write the full run state plus the per-phase JSON artifacts."""
        directory = self.run_dir(run.id)
        directory.mkdir(parents=True, exist_ok=True)
        _write(directory / "prompt.md", run.prompt)
        _write_json(directory / "council-state.json", run.to_dict())
        _write_json(
            directory / "proposals.json", [asdict(p) for p in run.proposals]
        )
        _write_json(
            directory / "critiques.json", [asdict(c) for c in run.critiques]
        )
        _write_json(
            directory / "votes.json",
            {"ballots": [asdict(b) for b in run.votes], "tally": run.tally},
        )
        if run.judgment is not None:
            _write_json(directory / "judgment.json", asdict(run.judgment))
        return directory

    def write_selected_plans(self, run: CouncilRun) -> None:
        _write(self.run_dir(run.id) / "selected-plans.md", render_plans(run))

    def write_approved_plan(self, run: CouncilRun) -> None:
        if run.approved_plan:
            _write(self.run_dir(run.id) / "approved-plan.md", run.approved_plan)

    def load(self, run_id: str) -> CouncilRun | None:
        path = self.run_dir(run_id) / "council-state.json"
        if not path.is_file():
            return None
        try:
            return CouncilRun.from_dict(json.loads(path.read_text("utf-8")))
        except (OSError, json.JSONDecodeError, ValueError):
            logger.debug("unreadable council artifact: %s", path, exc_info=True)
            return None

    def latest(self) -> CouncilRun | None:
        """The most recent run, by id — ids are timestamp-prefixed and sort."""
        for run_id in self.history():
            run = self.load(run_id)
            if run is not None:
                return run
        return None

    def history(self, limit: int = 20) -> list[str]:
        """Run ids, newest first."""
        if not self.base.is_dir():
            return []
        dirs = [d.name for d in self.base.iterdir() if d.is_dir()]
        return sorted(dirs, reverse=True)[:limit]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, data: Any) -> None:
    _write(path, json.dumps(data, indent=2, ensure_ascii=False))


def render_plans(run: CouncilRun) -> str:
    """The user-facing summary of what the council selected."""
    lines = [f"# Council Results — {run.id}", "", f"**Task:** {run.prompt.strip()}", ""]
    entropy = run.tally.get("entropy", 0.0)
    if run.judgment and run.judgment.fell_back_to_vote:
        lines.append(
            "> The judge was unreachable. The ordering below is the council's "
            "anonymous ranked vote — read it as a preference, not a verdict.\n"
        )
    elif entropy >= 0.9:
        # Surfaced rather than smoothed over: a near-even split is real
        # information about the decision, and hiding it behind a confident "#1
        # Recommended" is the failure this council exists to avoid.
        lines.append(
            f"> The council did not converge (disagreement {entropy:.2f}). The "
            "ranking below is a judgement call, not a consensus.\n"
        )

    for plan in run.ranked_plans():
        proposal = run.proposal(plan.proposal_id)
        if proposal is None:  # pragma: no cover
            continue
        label = "Recommended" if plan.rank == 1 else "Alternative"
        lines.append(f"## #{plan.rank} {label} — {proposal.title}")
        lines.append("")
        lines.append(proposal.summary)
        lines.append("")
        lines.append(
            f"*Score {plan.score:.1f}/10 · author confidence "
            f"{int(proposal.confidence * 100)}% · "
            f"{len(proposal.implementation_steps)} steps*"
        )
        lines.append("")
        for pro in proposal.tradeoffs.get("pros", [])[:3]:
            lines.append(f"- ✅ {pro}")
        for con in proposal.tradeoffs.get("cons", [])[:3]:
            lines.append(f"- ⚠️ {con}")
        if plan.required_changes:
            lines.append("")
            lines.append("**Must fix before building:**")
            for change in plan.required_changes:
                lines.append(f"- {change}")
        if plan.reasoning:
            lines.append("")
            lines.append(f"> {plan.reasoning}")
        lines.append("")
    return "\n".join(lines)
