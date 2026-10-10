"""Single source of truth for Hermes numeric thresholds and store namespaces.

Every tunable constant the Hermes learning subsystem depends on lives here so
that the **threshold auto-tuner** (``hermes/tuner.py``, Loop-Engineering
Enhancement 4) has exactly one place to read defaults from and exactly one set
of safety bounds to clamp against.

Runtime override model
----------------------
These constants are *defaults*. At runtime, :class:`~novacode_cli.hermes.review.ReviewRunner`
lazily loads tuned values from the durable store namespace
:data:`HARNESS_CONFIG_NS` (key :data:`HARNESS_CONFIG_KEY`). When that key is
absent — a fresh install, or before the tuner has run — the defaults below are
used unchanged, so behaviour is identical to the pre-tuner system.

The tuner never writes a value outside the ``*_FLOOR`` / ``*_CEILING`` bounds,
which exist to cap LLM spend (too-low review threshold) and prevent reviews
from never firing (too-high threshold).

Store namespaces introduced by the Loop-Engineering work are declared here too,
as named constants, so call sites reference a symbol instead of re-typing the
``("nova", "...")`` tuple (a typo there silently reads/writes the wrong place).
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Review-trigger thresholds (read by ReviewRunner; tuned by ThresholdTuner)
# ---------------------------------------------------------------------------

#: Tool calls between automatic reviews when no tuned value is stored.
REVIEW_THRESHOLD_DEFAULT: Final[int] = 10
# When a review runs.
#   "task_end" — once when the agent finishes a task, plus mid-task only when it
#                has just worked through an error.
#   "periodic" — the original behaviour: every REVIEW_THRESHOLD tool calls.
# Periodic reviews cannot know how the work turns out, and a long task gets one
# every ten calls: a 200-step Terminal-Bench task was reviewed ~20 times, wrote
# 98 bullets, and recorded approaches that went on to fail. Each one is also an
# extra model call carrying the whole conversation.
REVIEW_MODE_DEFAULT: Final[str] = "task_end"
#: A review fires early once this many failed tool calls occur in one window.
FAILURE_BURST_DEFAULT: Final[int] = 3

#: Hard bounds the tuner may never cross. The floor caps LLM spend (reviews
#: can't fire arbitrarily often); the ceiling stops reviews deferring forever.
REVIEW_THRESHOLD_FLOOR: Final[int] = 5
REVIEW_THRESHOLD_CEILING: Final[int] = 50
FAILURE_BURST_FLOOR: Final[int] = 2
FAILURE_BURST_CEILING: Final[int] = 10

# ---------------------------------------------------------------------------
# Threshold auto-tuner (Enhancement 4)
# ---------------------------------------------------------------------------

#: Minimum number of recorded reviews before the tuner will adjust anything —
#: tuning on sparse data overfits to one unusual session.
TUNER_MIN_BASIS_REVIEWS: Final[int] = 10
#: Weight applied to the freshly suggested value when blending with the current
#: one: ``new = (1 - TUNER_DAMPING) * old + TUNER_DAMPING * suggested``. Small,
#: so the tuner converges slowly instead of oscillating.
TUNER_DAMPING: Final[float] = 0.2

# ---------------------------------------------------------------------------
# Inline verification loop (Enhancement 1)
# ---------------------------------------------------------------------------

#: Maximum verify→retry cycles before an output is returned to the user as-is.
#: The hard guard against an infinite self-correction loop.
INLINE_VERIFIER_MAX_RETRIES: Final[int] = 3

# ---------------------------------------------------------------------------
# Prompt-template hill climbing (Enhancement 2)
# ---------------------------------------------------------------------------

#: Runs required *per variant* before an A/B prompt comparison is decided.
PROMPT_AB_MIN_RUNS: Final[int] = 20

#: How often (in reviews) the proactive auto-evolve trigger fires. Every N
#: reviews it proposes a candidate for a template even when no review flagged
#: one, so the loop explores instead of only reacting to complaints.
PROMPT_EVOLVE_INTERVAL: Final[int] = 10
#: Templates the proactive auto-evolve trigger may target. Only these are
#: considered so the loop never mutates arbitrary files.
PROMPT_EVOLVE_TEMPLATES: Final[tuple[str, ...]] = (
    "core_agent_system.jinja",
    "nova_review.jinja",
)

# ---------------------------------------------------------------------------
# Cron / heartbeat scheduler (Enhancement 3)
# ---------------------------------------------------------------------------

#: Most cron-triggered tasks allowed to be in flight at once, so a dense
#: schedule can't flood the shared remote-message queue.
CRON_MAX_CONCURRENT: Final[int] = 3

# ---------------------------------------------------------------------------
# Durable-store namespaces (LangGraph ``BaseStore``, all under the "nova" root)
# ---------------------------------------------------------------------------
# Existing namespaces (tool_counter / tool_history / tool_stats / skill_usage /
# reviews / meta / created_skills / curation_log) are owned by tracker.py and
# review.py. The names below are introduced by the Loop-Engineering work.

#: Tuned threshold values written by the auto-tuner; read by ReviewRunner.
HARNESS_CONFIG_NS: Final[tuple[str, str]] = ("nova", "harness_config")
#: Key within :data:`HARNESS_CONFIG_NS` holding the threshold dict.
HARNESS_CONFIG_KEY: Final[str] = "thresholds"

#: Per-run quality + variant records backing the prompt A/B comparison.
PROMPT_AB_LOG_NS: Final[tuple[str, str]] = ("nova", "prompt_ab_log")
#: Active/candidate version pointers for each evolving prompt template.
PROMPT_VERSIONS_NS: Final[tuple[str, str]] = ("nova", "prompt_versions")

#: Serialized cron job definitions (survive restarts).
CRON_SCHEDULES_NS: Final[tuple[str, str]] = ("nova", "cron_schedules")

#: Per-source webhook secrets and event-type allowlists.
WEBHOOK_CONFIG_NS: Final[tuple[str, str]] = ("nova", "webhook_config")

#: Inline-verifier retry outcomes — also the quality signal for prompt A/B.
VERIFICATION_LOG_NS: Final[tuple[str, str]] = ("nova", "verification_log")

#: Per-repo verification memory — which checks fail, which files are high-risk,
#: and which failure patterns recur. Keyed by repo so the verifier gets smarter
#: per-project over time ("Verification that Learns").
VERIFICATION_MEMORY_NS: Final[tuple[str, str]] = ("nova", "verification_memory")
