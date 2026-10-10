"""A wrapper for Nova Code CLI to run in Harbor evaluation environments.

This wrapper integrates the full Nova Code agent with all middleware
(FileTracker, SharedMemory, etc.) for comprehensive evaluation.
"""

import warnings

# Modal's SDK (via Harbor's Modal environment) emits PendingDeprecation warnings
# for its legacy Sandbox.open() / FileIO.{read,write,open} file APIs. They still
# work and are out of our control — silence them. Match loosely: the messages are
# date-prefixed and wrap the API names in backticks (e.g. "`Sandbox.open()` is
# deprecated"), so an exact "Sandbox.open() is deprecated" pattern never matched.
warnings.filterwarnings("ignore", message=r".*Sandbox\.open.*deprecated.*")
warnings.filterwarnings("ignore", message=r".*FileIO\.(read|write|open).*deprecated.*")
# Belt-and-suspenders: silence by Modal's own warning category when available.
try:
    from modal.exception import PendingDeprecationError as _ModalPendingDeprecation

    if isinstance(_ModalPendingDeprecation, type) and issubclass(
        _ModalPendingDeprecation, Warning
    ):
        warnings.filterwarnings("ignore", category=_ModalPendingDeprecation)
except Exception:  # noqa: BLE001 — modal may not expose this symbol
    pass

import json
import os
import threading
import tomllib
import uuid
from collections import defaultdict
from typing import Any
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext, ModelUsage
from harbor.models.trajectories import (
    Agent,
    FinalMetrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)
from langchain.messages import UsageMetadata
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langsmith import trace

# Load .env file if present
load_dotenv()

from deepagents_harbor.backend import HarborSandbox
from deepagents_harbor.time_budget import TimeBudgetMiddleware
from deepagents_harbor.tracing import create_example_id_from_instruction

# Import Nova Code components
from novacode_cli.agents.core_agent import create_agent_with_config, get_system_prompt
from novacode_cli.tracking.file_tracker import reset_session_tracker
from novacode_cli.config.model_manager import ModelManager, MODEL_PRESETS, ProviderType
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.memory.store import DualModeStore, get_async_durable_store
from langgraph.store.memory import InMemoryStore

def _agent_time_limit_min(environment: Any) -> int | None:  # noqa: ANN401
    """The task's agent timeout in whole minutes, from its ``task.toml``.

    Harbor enforces the limit but does not hand it to the agent; the task
    directory is the parent of the environment directory. ``None`` if it cannot
    be read — the prompt then simply omits the time line.
    """
    try:
        task_toml = Path(environment.environment_dir).parent / "task.toml"
        seconds = tomllib.loads(task_toml.read_text(encoding="utf-8"))["agent"]["timeout_sec"]
        return int(seconds // 60) or None
    except Exception:  # noqa: BLE001 — a missing limit must never fail a trial
        return None


class _UsageCollector(BaseCallbackHandler):
    """Total token usage over EVERY model call, broken down by model.

    Summing the returned message list counts the main agent only: a subagent
    runs its own graph and its messages never reach the top-level list, so any
    task that delegated under-reported its tokens — and harbor documents
    ``model_usage`` as including subagents. A callback sees every call.

    ``on_llm_end`` can fire from a worker thread while subagents run in
    parallel, hence the lock.
    """

    def __init__(self, default_model: str) -> None:
        self._default_model = default_model
        self._lock = threading.Lock()
        self.by_model: dict[str, dict[str, int]] = defaultdict(
            lambda: {"input": 0, "cache": 0, "output": 0}
        )

    def on_llm_end(self, response: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        for generations in getattr(response, "generations", None) or []:
            for generation in generations or []:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None) if message else None
                if not usage:
                    continue
                meta = getattr(message, "response_metadata", None) or {}
                name = meta.get("model_name") or meta.get("model") or self._default_model
                with self._lock:
                    row = self.by_model[name]
                    row["input"] += usage.get("input_tokens", 0) or 0
                    row["output"] += usage.get("output_tokens", 0) or 0
                    row["cache"] += (usage.get("input_token_details") or {}).get(
                        "cache_read", 0
                    ) or 0

    def totals(self) -> dict[str, int]:
        with self._lock:
            return {
                key: sum(row[key] for row in self.by_model.values())
                for key in ("input", "cache", "output")
            }

    def model_usage(self) -> dict[str, ModelUsage]:
        with self._lock:
            return {
                name: ModelUsage(
                    n_input_tokens=row["input"],
                    n_cache_tokens=row["cache"],
                    n_output_tokens=row["output"],
                    # Left unset: deriving a price needs a per-model rate card,
                    # and a wrong cost is worse than a missing one.
                    cost_usd=None,
                )
                for name, row in self.by_model.items()
            }


class NovaCodeWrapper(BaseAgent):
    """Harbor agent implementation using the full Nova Code CLI agent.

    This wrapper uses create_agent_with_config from novacode_cli to create
    a fully-featured agent with all middleware (FileTracker, SharedMemory, etc.).
    """

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        temperature: float = 0.0,
        verbose: bool = True,
        provider: ProviderType | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        learning: str = "off",
        context_budget: int | None = None,
        *args,
        **kwargs,
    ) -> None:
        """Initialize NovaCodeWrapper.

        Args:
            logs_dir: Directory for storing logs
            model_name: Name of the LLM model to use (optional, uses configured model)
            temperature: Temperature setting for the model
            verbose: Enable verbose output
            provider: Model provider (openai, anthropic, ollama, google).
                      If None, uses the configured provider from Nova.config.json or env.
            reasoning_effort: NovaCode's reasoning setting ("off", "low", "medium",
                      "high"); None leaves the model's own default.
            max_tokens: Output cap per model response; None uses NovaCode's default.
            learning: NovaCode's learning loop across trials.
                      "off" (default): every trial starts blank — the benchmark setting.
                      "on": trials share one memory, store and skills directory, and
                      the review loop writes lessons and skills as it goes.
                      "frozen": trials read what an earlier "on" run learned but
                      write nothing — for scoring tasks that were NOT learned from.
            context_budget: Tokens of context NovaCode should work within, when
                      that is less than the model's window. Its reducers (clearing
                      old tool results, summarizing) are sized from this instead.
        """
        super().__init__(logs_dir, model_name, *args, **kwargs)

        if learning not in ("off", "on", "frozen"):
            raise ValueError(f"learning must be off, on or frozen, not {learning!r}")
        # Learned state is written into the eval home. Letting that be the
        # default home would quietly turn every later "blank" baseline into a
        # run with learned skills, so a learning run must name its own home.
        if learning != "off" and not os.environ.get("NOVA_EVAL_HOME"):
            raise ValueError(
                "learning=on/frozen needs its own home: set NOVA_EVAL_HOME to a "
                "separate directory so baseline runs stay blank."
            )
        self._learning = learning
        self._context_budget = int(context_budget) if context_budget else None

        # Model knobs go through NovaCode's own config file, so they reach the
        # model the way a user's settings would. The file lives in the isolated
        # eval home and is rewritten from this run's arguments alone: nothing
        # carries over from an earlier run.
        knobs = {
            "reasoning_effort": reasoning_effort,
            "max_tokens": max_tokens,
            # Only "on" runs the review loop; "frozen" reads without writing.
            "learning_enabled": True if learning == "on" else None,
            "context_budget_tokens": int(context_budget) if context_budget else None,
        }
        knobs = {k: v for k, v in knobs.items() if v is not None}
        if "max_tokens" in knobs:
            knobs["max_tokens"] = int(knobs["max_tokens"])
        config_path = NovaConfig().config_path
        wanted = json.dumps(knobs, indent=2)
        if (
            not config_path.exists()
            or config_path.read_text(encoding="utf-8") != wanted
        ):
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(wanted, encoding="utf-8")
        self._reasoning_effort = reasoning_effort

        # Initialize model manager to get configured provider/model
        model_manager = ModelManager()

        # Determine provider and model
        if provider is None and model_name is None:
            # Use configured provider/model from Nova.config.json or env
            current = model_manager.get_current_provider()
            if current:
                provider_name, configured_model = current
                # Map display name back to provider ID
                provider = self._get_provider_id(provider_name)
                model_name = configured_model
            else:
                # Fallback to Ollama if nothing configured
                provider = "ollama"
                model_name = MODEL_PRESETS["ollama"]["default_model"]
        elif provider is not None and model_name is None:
            # Provider specified but no model - use default for that provider
            model_name = MODEL_PRESETS[provider]["default_model"]
        elif provider is None and model_name is not None:
            # Harbor-style "provider/model" (or "provider:model") names the
            # provider outright; otherwise infer it from the model name.
            provider, model_name = self._split_provider(model_name)
            provider = provider or self._infer_provider(model_name)

        self._provider: ProviderType = provider or "ollama"
        self._model_name = model_name or MODEL_PRESETS[self._provider]["default_model"]
        self._temperature = temperature
        self._verbose = verbose

        # Create model using ModelManager for consistent configuration
        self._model: BaseChatModel = model_manager.create_model_for_provider(
            self._provider, self._model_name
        )

        # LangSmith run tracking
        self._langsmith_run_id: str | None = None
        self._task_name: str | None = None

    @staticmethod
    def _get_provider_id(provider_name: str) -> ProviderType:
        """Convert display name to provider ID."""
        name_map = {
            "OpenAI": "openai",
            "Anthropic": "anthropic",
            "Ollama": "ollama",
            "Google": "google",
        }
        return name_map.get(provider_name, "ollama")  # type: ignore

    @staticmethod
    def _split_provider(model_name: str) -> tuple[ProviderType | None, str]:
        """Split "opencode/deepseek-v4.1-flash" into ("opencode", "deepseek-v4.1-flash").

        Only a known provider id counts as a prefix, so slashes inside a model
        name ("deepseek-ai/deepseek-v4-pro") are left alone.
        """
        for sep in ("/", ":"):
            head, found, rest = model_name.partition(sep)
            if found and head.lower() in MODEL_PRESETS:
                return head.lower(), rest  # type: ignore
        return None, model_name

    @staticmethod
    def _infer_provider(model_name: str) -> ProviderType:
        """Infer provider from model name."""
        model_lower = model_name.lower()
        if "gpt" in model_lower or "o1" in model_lower:
            return "openai"
        elif "claude" in model_lower:
            return "anthropic"
        elif "gemini" in model_lower:
            return "google"
        else:
            # Default to Ollama for unknown models (likely local)
            return "ollama"

    @staticmethod
    def name() -> str:
        return "NovaCode-harbor"

    async def setup(self, environment: BaseEnvironment) -> None:
        """Setup the agent with the given environment."""
        # Reset session tracker for clean evaluation
        reset_session_tracker()

    async def _build_system_prompt(
        self, backend: HarborSandbox, assistant_id: str
    ) -> str:
        """Build system prompt with actual container CWD and file listing injected.

        Queries the Docker container at runtime so the model knows exactly where it is,
        rather than relying on a static /app placeholder.
        """
        # Query actual container state
        try:
            cwd_result = await backend.aexecute("pwd")
            current_dir = cwd_result.output.strip() if cwd_result.output else "/app"
        except Exception:
            current_dir = "/app"

        # Relative paths in file tools resolve against the measured cwd, not a
        # per-provider guess (not every task works in /app).
        backend._workdir = current_dir

        try:
            ls_info = await backend.als_info(".")
            total = len(ls_info)
            shown = ls_info[:15]
            if total == 0:
                file_section = "Current directory is empty."
            elif total <= 15:
                file_section = "\n".join(
                    f"- {f['path']}{'/' if f['is_dir'] else ''}" for f in shown
                )
            else:
                file_section = (
                    "\n".join(
                        f"- {f['path']}{'/' if f['is_dir'] else ''}" for f in shown
                    )
                    + f"\n... ({total - 15} more)"
                )
        except Exception:
            file_section = "(unable to list directory)"

        # The conditions the agent actually works under. Without them it behaved
        # as if someone were on the other end: it ended trials with "please run
        # the tests in your environment", and it spent a whole budget on scratch
        # files (check.c … check8.c) without once writing the file it was asked
        # for. These are facts about the run, not hints about any task.
        minutes = _agent_time_limit_min(backend.environment)
        self._time_limit_min = minutes  # recorded in the trajectory
        time_line = (
            f"You have about {minutes} minutes in total and are stopped without warning "
            f"when they run out.\n"
            if minutes
            else ""
        )
        context_block = f"""<env>
Working directory: {current_dir}
</env>

You are operating in a **Docker sandbox**. Your current working directory is `{current_dir}`.
All file operations and shell commands execute inside this container — NOT on the host machine.

You are running **unattended**. Nobody will answer a question, approve a step, or run anything for you, and nobody reads your final message: the work is judged only by the state you leave in this container when you stop.
{time_line}Put a working version of what the task asks for at its required path early, then improve it. Work that exists only in scratch files when time runs out counts for nothing.

Files in `{current_dir}`:
{file_section}

"""
        base_prompt = get_system_prompt(
            assistant_id=assistant_id, sandbox_type="harbor"
        )
        return context_block + base_prompt

    def version(self) -> str | None:
        """The version of the agent."""
        return "0.1.0"

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Execute the Nova Code agent on the given instruction.

        Args:
            instruction: The task to complete
            environment: Harbor environment (Docker, Modal, etc.)
            context: Context to populate with metrics
        """
        configuration = json.loads(environment.trial_paths.config_path.read_text())
        if not isinstance(configuration, dict):
            raise AssertionError(
                f"Unexpected configuration format. Expected dict, got {type(configuration)}."
            )

        # Create Harbor sandbox backend
        backend = HarborSandbox(environment)

        # Build system prompt with actual container CWD and file listing
        # Memory files are kept per assistant id: one per trial when blank, one
        # shared id when trials are meant to build on each other.
        learning = self._learning != "off"
        assistant_id = "Nova-eval-learner" if learning else f"Nova-eval-{environment.session_id}"
        system_prompt = await self._build_system_prompt(backend, assistant_id)

        # Create tools list for the agent
        # Using empty list — deepagents provides built-in tools
        # (read_file, write_file, edit_file, execute, glob, grep, etc.)
        # which are sufficient for Terminal-Bench tasks.
        # Avoid passing tools with dict[str, Any] type hints + from __future__ import annotations
        # which triggers a pydantic/typing evaluation error in subagent creation.
        tools = []

        # Create Nova Code agent with full middleware stack.
        # auto_approve=True: evaluation runs unattended — no human-in-the-loop interrupts.
        # Fresh in-memory store per trial: nothing one task remembers can leak
        # into another, and concurrent trials don't share a sqlite file.
        # A learning run instead shares NovaCode's durable store (in its own home).
        store = await get_async_durable_store() if learning else DualModeStore(InMemoryStore())
        # Durations of slow calls, budget checkpoints, and where the time went.
        self._clock = TimeBudgetMiddleware(getattr(self, "_time_limit_min", None))
        Nova_agent, _ = create_agent_with_config(
            model=self._model,
            assistant_id=assistant_id,
            tools=tools,
            sandbox=backend,
            sandbox_type="harbor",
            system_prompt=system_prompt,
            auto_approve=True,
            store=store,
            extra_middleware=[self._clock],
        )

        # Build metadata
        metadata = {
            "task_instruction": instruction,
            "model": self._model_name,
            "harbor_session_id": environment.session_id,
            "agent_mode": "NovaCode-full",
            "wrapper": "NovaCodeWrapper",
        }
        metadata.update(configuration)

        # Compute example_id for LangSmith linking
        example_id = create_example_id_from_instruction(instruction)

        # Counts every model call, subagents included (see _UsageCollector).
        usage = _UsageCollector(self._model_name)

        config: RunnableConfig = {
            "run_name": f"Nova-{environment.session_id}",
            # LangGraph defaults to 25 super-steps; a real Terminal-Bench task
            # (read → edit → run tests → fix → re-run) easily exceeds that and
            # would die with GraphRecursionError mid-task. Raise it well above
            # the per-task step budget so completion is bounded by the agent
            # timeout, not the recursion limit. (200 was still too low:
            # build-pov-ray burned through it in 207s.)
            # 10_000 was effectively no bound: a looping task reached 254 turns
            # and 32M input tokens. The longest *productive* run observed was
            # ~254 turns, so 2_000 graph steps leaves ample headroom while still
            # ending a runaway. Completion is still normally bounded by the
            # harbor agent timeout.
            "recursion_limit": 2_000,
            "tags": [
                self._model_name,
                environment.session_id,
                "NovaCode",
                "evaluation",
            ],
            "configurable": {
                "thread_id": str(uuid.uuid4()),
            },
            "callbacks": [usage],
        }

        # Run with LangSmith tracing if configured
        langsmith_experiment_name = (
            os.environ.get("LANGSMITH_EXPERIMENT", "").strip() or None
        )

        # Stream state snapshots rather than ainvoke: harbor cancels run() at the
        # agent timeout, and the last snapshot is all that is left to record.
        # A turn cut off at the output limit is continued by NovaCode itself
        # (TaskDisciplineMiddleware's truncation gate), not from here.
        result: dict = {"messages": []}

        async def _run_agent() -> None:
            nonlocal result
            inputs = {"messages": [{"role": "user", "content": instruction}]}
            async for result in Nova_agent.astream(
                inputs, config=config, stream_mode="values"
            ):
                pass

        try:
            if langsmith_experiment_name:
                with trace(
                    name=f"Nova-{environment.session_id}",
                    reference_example_id=example_id,
                    inputs={"instruction": instruction},
                    project_name=langsmith_experiment_name,
                    metadata=metadata,
                ) as run_tree:
                    await _run_agent()
                    # Extract last AI message for output
                    last_message = result["messages"][-1]
                    if isinstance(last_message, AIMessage):
                        run_tree.end(outputs={"last_message": last_message.text})
            else:
                config["metadata"] = metadata
                await _run_agent()
        finally:
            # Save trajectory + token counts for Harbor, timed out or not.
            self._save_trajectory(environment, instruction, result, context, usage)

    def _save_trajectory(
        self,
        environment: BaseEnvironment,
        instruction: str,
        result: dict,
        context: AgentContext,
        # Not `usage`: the message loop below binds that name per message.
        collector: _UsageCollector | None = None,
    ) -> None:
        """Save current trajectory to logs directory in ATIF format."""
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cache_tokens = 0

        # Create initial step from user instruction
        steps = [
            Step(
                step_id=1,
                timestamp=datetime.now(timezone.utc).isoformat(),
                source="user",
                message=instruction,
            ),
        ]

        observations = []
        pending_step: Step | None = None

        for msg in result["messages"]:
            if isinstance(msg, AIMessage):
                # Extract token usage
                usage: UsageMetadata = msg.usage_metadata
                if usage:
                    total_prompt_tokens += usage.get("input_tokens", 0)
                    total_completion_tokens += usage.get("output_tokens", 0)
                    total_cache_tokens += (usage.get("input_token_details") or {}).get(
                        "cache_read", 0
                    ) or 0

                # Process pending step with observations
                if pending_step is not None:
                    if pending_step.tool_calls and observations:
                        pending_step.observation = Observation(results=observations)
                        observations = []
                    steps.append(pending_step)
                    pending_step = None

                # Extract content and tool calls.
                # Prefer msg.tool_calls (authoritative) for tool call extraction;
                # content_blocks is used for text/reasoning content only.
                atf_tool_calls = []
                message = ""

                content_blocks = getattr(msg, "content_blocks", None)
                if content_blocks:
                    for cb in content_blocks:
                        if cb.get("type") == "text":
                            message += cb.get("text", "")
                        elif cb.get("type") == "reasoning":
                            message += cb.get("reasoning", "")
                        # Skip tool_call blocks here — captured below via msg.tool_calls
                elif isinstance(msg.content, str):
                    message = msg.content
                elif isinstance(msg.content, list):
                    for item in msg.content:
                        if isinstance(item, str):
                            message += item
                        elif isinstance(item, dict) and item.get("type") == "text":
                            message += item.get("text", "")

                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    for tc in msg.tool_calls:
                        atf_tool_calls.append(
                            ToolCall(
                                tool_call_id=tc.get("id", ""),
                                function_name=tc.get("name", ""),
                                arguments=tc.get("args", {}),
                            )
                        )

                new_step = Step(
                    step_id=steps[-1].step_id + 1 if steps else 1,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    source="agent",
                    message=message,
                    # Thinking and the stop reason, so an empty turn can be told
                    # apart from a finished one ("length" = cut off at the cap).
                    reasoning_content=msg.additional_kwargs.get("reasoning_content")
                    or None,
                    extra={
                        "finish_reason": msg.response_metadata.get("done_reason")
                        or msg.response_metadata.get("finish_reason")
                    },
                    tool_calls=atf_tool_calls if atf_tool_calls else None,
                )

                if atf_tool_calls:
                    pending_step = new_step
                else:
                    steps.append(new_step)

            elif isinstance(msg, ToolMessage):
                observations.append(
                    ObservationResult(
                        source_call_id=msg.tool_call_id,
                        content=str(msg.content),
                    )
                )

            elif isinstance(msg, HumanMessage):
                # Skip - already handled initial instruction
                pass

        # Add remaining pending step
        if pending_step is not None:
            if pending_step.tool_calls and observations:
                pending_step.observation = Observation(results=observations)
            steps.append(pending_step)

        # Token counts into harbor's result.json (main agent only — subagent
        # model calls don't appear in the top-level message list).
        # Prefer the callback totals: they include subagent calls, which never
        # appear in the message list scanned above. Fall back to that scan if no
        # callback fired (e.g. a provider that reports no usage metadata).
        collected = collector.totals() if collector else {"input": 0, "cache": 0, "output": 0}
        if collected["input"] or collected["output"]:
            context.n_input_tokens = collected["input"] or None
            context.n_cache_tokens = collected["cache"] or None
            context.n_output_tokens = collected["output"] or None
            context.model_usage = collector.model_usage() if collector else None
        else:
            context.n_input_tokens = total_prompt_tokens or None
            context.n_output_tokens = total_completion_tokens or None
            context.n_cache_tokens = total_cache_tokens or None

        # Build trajectory
        metrics = FinalMetrics(
            total_prompt_tokens=total_prompt_tokens or None,
            total_completion_tokens=total_completion_tokens or None,
            total_steps=len(steps),
        )

        trajectory = Trajectory(
            schema_version="ATIF-v1.2",
            session_id=environment.session_id,
            agent=Agent(
                name=self.name(),
                version=self.version() or "unknown",
                model_name=self._model_name,
                extra={
                    "framework": "NovaCode-cli",
                    "reasoning_effort": self._reasoning_effort,
                    "learning": self._learning,
                    "context_budget": self._context_budget,
                    "timing": self._clock.summary() if getattr(self, "_clock", None) else None,
                    "time_limit_min_told_to_agent": getattr(self, "_time_limit_min", None),
                    "middleware": [
                        "FileTrackerMiddleware",
                        "AgentMemoryMiddleware",
                        "SharedMemoryMiddleware",
                        "ShellMiddleware",
                    ],
                },
            ),
            steps=steps,
            final_metrics=metrics,
        )

        trajectory_path = self.logs_dir / "trajectory.json"
        trajectory_path.write_text(json.dumps(trajectory.to_json_dict(), indent=2))
