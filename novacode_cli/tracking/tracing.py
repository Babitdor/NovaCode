"""LangSmith tracing integration for Nova-Code CLI.

This module provides comprehensive tracing capabilities using LangSmith,
enabling visibility into LLM calls, tool executions, and agent workflows.

Features:
- Automatic tracing of all LLM calls via wrapped clients
- Custom traceable decorators for functions
- Tracing configuration via environment variables
- Project and dataset management
- Trace filtering and analysis helpers

Environment Variables:
- LANGSMITH_TRACING: Set to "true" to enable tracing
- LANGSMITH_API_KEY: Your LangSmith API key
- LANGSMITH_PROJECT: Optional project name (defaults to "Nova-Code")
- LANGSMITH_WORKSPACE_ID: Optional workspace ID for multi-tenant setups
- LANGSMITH_ENDPOINT: Optional LangSmith server endpoint

Usage:
    # Enable tracing at startup
    from novacode_cli.tracing import configure_tracing
    configure_tracing()

    # Or with custom project name
    configure_tracing(project_name="my-agent")

    # Use @traceable decorator for custom functions
    from novacode_cli.tracing import traceable

    @traceable
    def my_function(arg1, arg2):
        ...

    # Wrap OpenAI client for automatic LLM tracing
    from langchain_openai import ChatOpenAI
    from novacode_cli.tracing import wrap_openai_client

    model = wrap_openai_client(ChatOpenAI(model="gpt-4o"))
"""

import contextlib
import os
from collections.abc import Callable, Mapping
from functools import wraps
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only: importing langchain_openai eagerly costs ~1.5s at startup.
    from langchain_openai import ChatOpenAI

# LangSmith imports with graceful fallback
_TRACING_AVAILABLE = False
_TRACING_ERROR: str | None = None

try:
    from langsmith import Client, traceable
    from langsmith.wrappers import wrap_openai as _wrap_openai

    _TRACING_AVAILABLE = True
except ImportError as e:
    _TRACING_ERROR = str(e)
    traceable = None  # type: ignore[misc]


class TracingConfig:
    """Configuration for LangSmith tracing."""

    def __init__(
        self,
        enabled: bool = False,
        api_key: str | None = None,
        project_name: str = "Nova-Code",
        workspace_id: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.api_key = api_key
        self.project_name = project_name
        self.workspace_id = workspace_id
        self.endpoint = endpoint
        self._client: Client | None = None  # type: ignore

    def is_configured(self) -> bool:
        """Check if tracing is properly configured."""
        return self.enabled and self.api_key is not None


# Global tracing configuration
_tracing_config = TracingConfig()


def is_tracing_enabled() -> bool:
    """Check if LangSmith tracing is available and enabled.

    Returns:
        True if tracing is available and configured, False otherwise.
    """
    if not _TRACING_AVAILABLE:
        return False
    return _tracing_config.is_configured()


def get_tracing_config() -> TracingConfig:
    """Get the current tracing configuration.

    Returns:
        TracingConfig instance with current settings.
    """
    return _tracing_config


def configure_tracing(
    api_key: str | None = None,
    project_name: str = "Nova-Code",
    workspace_id: str | None = None,
    endpoint: str | None = None,
    enable: bool = True,
) -> TracingConfig:
    """Configure LangSmith tracing with the provided settings.

    This function sets up LangSmith tracing by reading environment variables
    and applying any provided overrides.

    Args:
        api_key: LangSmith API key. If not provided, reads from LANGSMITH_API_KEY.
        project_name: Name of the tracing project. Defaults to "Nova-Code".
        workspace_id: Optional workspace ID for multi-tenant setups.
        endpoint: Optional LangSmith server endpoint.
        enable: Whether to enable tracing. If False, tracing is disabled.

    Returns:
        TracingConfig with the applied configuration.
    """
    global _tracing_config

    # Read from environment if not provided
    env_tracing = os.environ.get("LANGSMITH_TRACING", "").lower()
    env_api_key = os.environ.get("LANGSMITH_API_KEY")
    env_project = os.environ.get("LANGSMITH_PROJECT", "Nova-Code")
    env_workspace = os.environ.get("LANGSMITH_WORKSPACE_ID")
    env_endpoint = os.environ.get("LANGSMITH_ENDPOINT")

    _tracing_config = TracingConfig(
        enabled=enable and (env_tracing == "true" or (api_key or env_api_key)),  # type: ignore
        api_key=api_key or env_api_key,
        project_name=project_name or env_project,
        workspace_id=workspace_id or env_workspace,
        endpoint=endpoint or env_endpoint,
    )

    if _tracing_config.is_configured():
        _setup_langsmith_env()
    elif not enable:
        os.environ["LANGSMITH_TRACING"] = "false"

    return _tracing_config


#: The LangSmith environment variables the ``/trace`` setup flow edits.
LANGSMITH_ENV_VARS: tuple[str, ...] = (
    "LANGSMITH_TRACING",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
)


def reset_langsmith_caches() -> None:
    """Make LangSmith forget what it already read from the environment.

    ``langsmith.utils.get_env_var`` and ``langsmith.utils.get_tracer_project``
    are ``lru_cache``d, and LangSmith keeps a global ``Client``. Both are read
    before the first traced call, so changing the environment afterwards has no
    effect at all: no tracer is attached and the old API key and endpoint keep
    being used. Clearing them is what makes a runtime change take effect.
    """
    try:
        from langsmith import run_trees as ls_run_trees
        from langsmith import utils as ls_utils
    except ImportError:  # pragma: no cover - langsmith is optional
        return

    for func in (
        getattr(ls_utils, "get_env_var", None),
        getattr(ls_utils, "get_tracer_project", None),
    ):
        cache_clear = getattr(func, "cache_clear", None)
        if cache_clear is not None:
            # A cache that refuses to clear must not stop the rest of the
            # reset, or the tracer would keep the old endpoint and key.
            with contextlib.suppress(Exception):
                cache_clear()

    # The cached client captured the API key and endpoint it was built with.
    if getattr(ls_run_trees, "_CLIENT", None) is not None:
        ls_run_trees._CLIENT = None  # type: ignore[attr-defined]


def apply_langsmith_env(values: Mapping[str, str]) -> TracingConfig:
    """Apply LangSmith environment variables and activate tracing right away.

    Args:
        values: The variables to set. Only the names in
            :data:`LANGSMITH_ENV_VARS` are honoured; an empty value removes
            the variable.

    Returns:
        The resulting :class:`TracingConfig`.
    """
    for name in LANGSMITH_ENV_VARS:
        if name not in values:
            continue
        raw = (values.get(name) or "").strip()
        if raw:
            os.environ[name] = raw
        else:
            os.environ.pop(name, None)

    # The environment is only read once per process, so it has to be re-read
    # before the next call can pick the new values up.
    reset_langsmith_caches()

    api_key = os.environ.get("LANGSMITH_API_KEY")
    # Tracing without a key cannot work: the client would fail to authenticate
    # on every call, so report it as off instead of half-enabling it.
    # "true" and "1" are the values LangSmith itself accepts.
    tracing_on = (
        os.environ.get("LANGSMITH_TRACING", "").strip().lower() in ("true", "1")
        and bool(api_key)
    )
    return configure_tracing(
        api_key=api_key,
        project_name=os.environ.get("LANGSMITH_PROJECT") or "Nova-Code",
        endpoint=os.environ.get("LANGSMITH_ENDPOINT"),
        enable=tracing_on,
    )


def _setup_langsmith_env() -> None:
    """Set environment variables for LangSmith tracing."""
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_PROJECT"] = _tracing_config.project_name

    if _tracing_config.api_key:
        os.environ["LANGSMITH_API_KEY"] = _tracing_config.api_key

    if _tracing_config.workspace_id:
        os.environ["LANGSMITH_WORKSPACE_ID"] = _tracing_config.workspace_id

    if _tracing_config.endpoint:
        os.environ["LANGSMITH_ENDPOINT"] = _tracing_config.endpoint


def get_client() -> Client | None:  # type: ignore
    """Get the LangSmith client if configured.

    Returns:
        LangSmith Client instance or None if not configured.
    """
    if not _TRACING_AVAILABLE or not _tracing_config.is_configured():
        return None

    if _tracing_config._client is None:
        _tracing_config._client = Client(  # type: ignore
            api_key=_tracing_config.api_key,
            endpoint=_tracing_config.endpoint,  # type: ignore
        )

    return _tracing_config._client


def wrap_openai_client(model: "ChatOpenAI") -> "ChatOpenAI":
    """Wrap an OpenAI/LangChain client for automatic tracing.

    This wrapper ensures all LLM calls made through the client are
    automatically traced in LangSmith.

    Args:
        model: ChatOpenAI or compatible model instance to wrap.

    Returns:
        Wrapped model that emits traces to LangSmith.

    Example:
        from langchain_openai import ChatOpenAI
        from novacode_cli.tracing import wrap_openai_client

        model = wrap_openai_client(ChatOpenAI(model="gpt-4o"))
        response = model.invoke("Hello")  # This call is now traced
    """
    if not _TRACING_AVAILABLE or not _tracing_config.is_configured():
        return model

    return _wrap_openai(model)  # type: ignore


def trace(
    name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Callable:
    """Create a decorator to trace a function with LangSmith.

    This decorator wraps a function so that its execution is traced
    in LangSmith with the specified metadata.

    Args:
        name: Optional name for the trace. Defaults to function name.
        tags: Optional list of tags for the trace.
        metadata: Optional metadata dictionary for the trace.

    Returns:
        Decorator function.

    Example:
        from novacode_cli.tracing import trace

        @trace(name="my-function", tags=["custom", "analysis"])
        def process_data(data):
            ...
    """
    if not _TRACING_AVAILABLE or not _tracing_config.is_configured():
        return lambda func: func

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
            return func(*args, **kwargs)

        return (
            traceable(
                name=name,
                tags=tags,
                metadata=metadata,
            )(func)
            if callable(func)
            else func
        )  # type: ignore

    return decorator


async def atrace(
    name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Callable:
    """Create an async decorator to trace an async function with LangSmith.

    Similar to trace() but for async functions.

    Args:
        name: Optional name for the trace. Defaults to function name.
        tags: Optional list of tags for the trace.
        metadata: Optional metadata dictionary for the trace.

    Returns:
        Decorator function.

    Example:
        from novacode_cli.tracing import atrace

        @atrace(name="async-process", tags=["async"])
        async def process_async(data):
            ...
    """
    if not _TRACING_AVAILABLE or not _tracing_config.is_configured():
        return lambda func: func

    def decorator(func: Callable) -> Callable:
        return (
            traceable(
                name=name,
                tags=tags,
                metadata=metadata,
            )(func)
            if callable(func)
            else func
        )  # type: ignore

    return decorator


def trace_context(
    name: str,
    inputs: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Create a trace context for manual tracing.

    This is useful when you want to manually create and manage traces
    rather than using decorators.

    Args:
        name: Name for the trace.
        inputs: Optional input dictionary for the trace.
        tags: Optional list of tags.

    Returns:
        Dictionary containing trace information.

    Example:
        ctx = trace_context("manual-task", {"query": "search"}, ["search"])
        # ... perform operations ...
    """
    if not _TRACING_AVAILABLE or not _tracing_config.is_configured():
        return {"id": "disabled", "name": name}

    # For manual tracing, we'd use the langsmith API directly
    # This is a placeholder for more advanced usage
    return {
        "id": f"trace-{name}",
        "name": name,
        "inputs": inputs or {},
        "tags": tags or [],
    }


def create_project(
    project_name: str | None = None,
    description: str | None = None,
) -> dict | None:
    """Create a new tracing project in LangSmith.

    Args:
        project_name: Name of the project to create. Uses configured default if None.
        description: Optional description for the project.

    Returns:
        Project info dict or None if not available.
    """
    client = get_client()
    if not client:
        return None

    name = project_name or _tracing_config.project_name

    try:
        project = client.create_project(project_name=name, description=description)  # type: ignore
        return {
            "id": project.id,
            "name": project.name,
            "url": f"https://smith.langchain.com/project/{project.id}",
        }
    except Exception:
        # Project might already exist
        return {"id": "existing", "name": name}


def list_projects() -> list[dict]:
    """List all tracing projects.

    Returns:
        List of project info dictionaries.
    """
    client = get_client()
    if not client:
        return []

    try:
        projects = client.list_projects()
        return [
            {
                "id": p.id,
                "name": p.name,
                "url": f"https://smith.langchain.com/project/{p.id}",
            }
            for p in projects
        ]
    except Exception:
        return []


def get_traces(
    project_name: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Get recent traces from a project.

    Args:
        project_name: Project to get traces from. Uses configured default if None.
        limit: Maximum number of traces to return.

    Returns:
        List of trace info dictionaries.
    """
    client = get_client()
    if not client:
        return []

    name = project_name or _tracing_config.project_name

    try:
        traces = client.list_examples(project_name=name, limit=limit)
        return [
            {
                "id": str(t.id),
                "name": t.name or "Unnamed",  # type: ignore
                "created_at": str(t.created_at) if t.created_at else None,
                "inputs": t.inputs or {},
                "outputs": t.outputs or {},
            }
            for t in traces
        ]
    except Exception:
        return []


def get_tracing_status() -> dict:
    """Get the current status of LangSmith tracing.

    Returns:
        Dictionary with tracing status information.
    """
    return {
        "available": _TRACING_AVAILABLE,
        "configured": _tracing_config.is_configured(),
        "enabled": _tracing_config.enabled if _tracing_config else False,
        "project": _tracing_config.project_name if _tracing_config else None,
        "error": _TRACING_ERROR,
    }


def log_to_project(
    data: dict[str, Any],
    project_name: str | None = None,
) -> bool:
    """Log custom data to a LangSmith project.

    This is useful for logging metrics, evaluation results, or custom events.

    Args:
        data: Dictionary to log.
        project_name: Target project name.

    Returns:
        True if successful, False otherwise.
    """
    client = get_client()
    if not client:
        return False

    name = project_name or _tracing_config.project_name

    try:
        client.create_run(name="custom-log", inputs=data, project_name=name, run_type="llm")
        return True
    except Exception:
        return False


def auto_configure() -> TracingConfig:
    """Automatically configure tracing from environment and settings.

    This is called at startup to set up tracing based on:
    1. Environment variables
    2. Settings file configuration
    3. User preferences

    Returns:
        The configured TracingConfig.
    """
    from novacode_cli.config.config import Settings

    settings = Settings.from_environment()

    # Check if there's a langsmith config in settings
    langsmith_api_key = os.environ.get("LANGSMITH_API_KEY")

    # Only configure if we have an API key
    if langsmith_api_key:
        return configure_tracing(
            api_key=langsmith_api_key,
            project_name=os.environ.get("LANGSMITH_PROJECT", "Nova-Code"),
        )

    # Return default configuration (disabled)
    return _tracing_config


# Convenience exports
__all__ = [
    "atrace",
    "auto_configure",
    "configure_tracing",
    "create_project",
    "get_client",
    "get_traces",
    "get_tracing_config",
    "get_tracing_status",
    "is_tracing_enabled",
    "list_projects",
    "log_to_project",
    "trace",
    "trace_context",
    "wrap_openai_client",
]
