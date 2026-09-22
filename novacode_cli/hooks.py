"""Lightweight hook dispatch for external tool integration.

Loads hook configuration from `~/.nova/hooks.json` and fires matching
commands with JSON payloads on stdin. Subprocess work is offloaded to a
background thread so the caller's event loop is never stalled. Failures are
logged but never bubble up to the caller.

Config format (`~/.nova/hooks.json`):

```json
{"hooks": [{"command": ["bash", "adapter.sh"], "events": ["session.start"]}]}
```

If `events` is omitted or empty the hook receives **all** events.

Available Events:
    - session.start: When a new session begins
    - session.end: When a session ends
    - session.save: When a session is saved
    - session.continue: When a session is continued
    - model.switch: When the model is switched
    - tool.call: Before a tool is executed
    - tool.result: After a tool completes
    - agent.message: When the agent sends a message
    - user.message: When the user sends a message
    - error: When an error occurs
    - remote.message: When a remote (Discord/Telegram) message arrives
    - context.warning: When context usage is high (warning/critical)
    - compact: When conversation compaction occurs
    - init.complete: When /init pipeline finishes
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_hooks_config: list[dict[str, Any]] | None = None
"""Cached config — loaded lazily on first dispatch."""

_background_tasks: set[asyncio.Task[None]] = set()
"""Strong references to fire-and-forget tasks to prevent GC."""

# Default hooks directory
HOOKS_DIR = Path.home() / ".nova"
HOOKS_FILE = HOOKS_DIR / "hooks.json"

# Shell metacharacters that indicate command injection
_SHELL_METACHARACTERS = set("`$|;&")

# Known API key / token environment variable suffixes.
_API_KEY_SUFFIXES = frozenset({"_API_KEY", "_API_TOKEN", "_TOKEN", "_SECRET"})



_HOOK_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="nova-hook")

def _validate_command(command: list[str]) -> str | None:
    """Validate a hook command tuple.

    Returns ``None`` if valid, or an error string if invalid.
    """
    if not command:
        return "Command list is empty"
    if not isinstance(command, list):
        return "Command must be a list of strings"
    if not all(isinstance(part, str) for part in command):
        return "All command parts must be strings"
    # Check the binary (first element) exists on PATH
    binary = command[0]
    if "/" in binary and not Path(binary).is_file():
        return f"Command binary not found: {binary}"
    if "/" not in binary and not shutil.which(binary):
        return f"Command not found on PATH: {binary}"
    # Check all parts for shell metacharacters
    for part in command:
        if any(c in part for c in _SHELL_METACHARACTERS):
            return f"Shell metacharacters not allowed in command part: {part}"
    return None


def _sanitize_env_for_hook(env: dict[str, str]) -> dict[str, str]:
    """Strip API key / token env vars from a subprocess environment."""
    to_strip: set[str] = set()
    for key in env:
        upper = key.upper()
        for suffix in _API_KEY_SUFFIXES:
            if upper.endswith(suffix):
                to_strip.add(key)
                break
    for k in to_strip:
        env.pop(k, None)
    return env


def reload_hooks() -> None:
    """Drop the cached hook config so the next dispatch re-reads ~/.nova/hooks.json.

    Used by /reload-plugins to pick up hooks a plugin merged in this session.
    """
    global _hooks_config  # noqa: PLW0603
    _hooks_config = None


def _load_hooks() -> list[dict[str, Any]]:
    """Load and cache hook definitions from the config file.

    Returns:
        An empty list when the file is missing or malformed so that normal
            execution is never interrupted.
    """
    global _hooks_config  # noqa: PLW0603
    if _hooks_config is not None:
        return _hooks_config

    if not HOOKS_FILE.is_file():
        _hooks_config = []
        return _hooks_config

    try:
        data = json.loads(HOOKS_FILE.read_text())
        if not isinstance(data, dict):
            logger.warning(
                "Hooks config at %s must be a JSON object, got %s",
                HOOKS_FILE,
                type(data).__name__,
            )
            _hooks_config = []
            return _hooks_config
        hooks = data.get("hooks", [])
        if not isinstance(hooks, list):
            logger.warning(
                "Hooks config 'hooks' key at %s must be a list, got %s",
                HOOKS_FILE,
                type(hooks).__name__,
            )
            _hooks_config = []
            return _hooks_config
        _hooks_config = hooks
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load hooks config from %s: %s", HOOKS_FILE, exc)
        _hooks_config = []

    return _hooks_config


def _run_single_hook(command: list[str], event: str, payload_bytes: bytes) -> None:
    """Execute a single hook command, writing the JSON payload to its stdin.

    Validates the command before executing and strips API keys from the
    subprocess environment to prevent secret leakage.

    Uses `subprocess.run` which automatically kills the child process on
    timeout, preventing zombie/orphan process leaks.

    Args:
        command: The command and arguments to run.
        event: Event name (for logging).
        payload_bytes: JSON payload to write to the command's stdin.
    """
    # Validate command before execution
    error = _validate_command(command)
    if error:
        logger.warning("Hook command validation failed for event %s: %s — %s", event, command, error)
        return

    # Sanitize environment: strip API keys to prevent secret leakage
    clean_env = _sanitize_env_for_hook(os.environ.copy())

    try:
        subprocess.run(  # noqa: S603
            command,
            input=payload_bytes,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            timeout=5,
            check=False,
            env=clean_env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Hook command timed out (>5s) for event %s: %s", event, command)
    except (FileNotFoundError, PermissionError) as exc:
        logger.warning("Hook command failed for event %s: %s — %s", event, command, exc)
    except Exception:
        logger.debug(
            "Hook dispatch failed for event %s: %s",
            event,
            command,
            exc_info=True,
        )


def _dispatch_hook_sync(
    event: str, payload_bytes: bytes, hooks: list[dict[str, Any]]
) -> None:
    """Dispatch matching hooks, running them concurrently via a thread pool.

    Iterates over all configured hooks, skipping those whose event filter
    does not match or whose `command` is missing/invalid. Matching hooks are
    executed concurrently with a 5-second timeout per command. Errors are caught
    per-hook and logged without propagating.

    Args:
        event: Dotted event name (e.g. `'session.start'`).
        payload_bytes: JSON payload to write to each command's stdin.
        hooks: List of hook definition dicts from the config file.
    """
    matching: list[list[str]] = []
    for hook in hooks:
        command = hook.get("command")
        if not isinstance(command, list) or not command:
            continue

        events = hook.get("events")
        # Empty/missing events list means "subscribe to everything".
        if events and event not in events:
            continue

        matching.append(command)

    if not matching:
        return

    if len(matching) == 1:
        _run_single_hook(matching[0], event, payload_bytes)
        return

    with ThreadPoolExecutor(max_workers=len(matching)) as pool:
        futures = [
            pool.submit(_run_single_hook, cmd, event, payload_bytes) for cmd in matching
        ]
        for future in futures:
            future.result()


async def dispatch_hook(event: str, payload: dict[str, Any]) -> None:
    """Fire matching hook commands with `payload` serialized as JSON on stdin.

    The `event` name is automatically injected into the payload under the
    `"event"` key so callers don't need to duplicate it.

    The blocking subprocess work is offloaded to a thread so the caller's
    event loop is never stalled. Matching hooks run concurrently, each with
    a 5-second timeout. Errors are logged and never propagated.

    Args:
        event: Dotted event name (e.g. `'session.start'`).
        payload: Arbitrary JSON-serializable dict sent on the command's stdin.
    """
    try:
        hooks = _load_hooks()
        if not hooks:
            return

        payload_bytes = json.dumps({"event": event, **payload}).encode()
        # A dedicated pool, not asyncio's default: hooks fire on every tool
        # call/result and each can take its full 5s timeout, so on the shared
        # default pool (16 threads) a busy turn starved everything else that
        # uses it (context breakdown, session listing, skill lookup...).
        await asyncio.get_running_loop().run_in_executor(
            _HOOK_POOL, _dispatch_hook_sync, event, payload_bytes, hooks
        )
    except Exception:
        logger.warning(
            "Unexpected error in dispatch_hook for event %s",
            event,
            exc_info=True,
        )


def dispatch_hook_fire_and_forget(event: str, payload: dict[str, Any]) -> None:
    """Schedule `dispatch_hook` as a background task with a strong reference.

    Use this instead of bare `create_task(dispatch_hook(...))` to prevent the
    task from being garbage collected before completion.

    Safe to call from sync code as long as an event loop is running.

    Args:
        event: Dotted event name (e.g. `'session.start'`).
        payload: Arbitrary JSON-serializable dict sent to the command's stdin.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("No running event loop; skipping hook for %s", event)
        return
    task = loop.create_task(dispatch_hook(event, payload))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def reload_hooks() -> None:
    """Reload hooks configuration from disk.

    Clears the cached configuration so it will be reloaded on next dispatch.
    """
    global _hooks_config  # noqa: PLW0603
    _hooks_config = None


# Event type constants for convenience
class HookEvent:
    """Constants for hook event names."""

    # Session events
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    SESSION_SAVE = "session.save"
    SESSION_CONTINUE = "session.continue"

    # Model events
    MODEL_SWITCH = "model.switch"

    # Tool events
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"

    # Message events
    AGENT_MESSAGE = "agent.message"
    USER_MESSAGE = "user.message"

    # Error events
    ERROR = "error"

    # Lifecycle events
    REMOTE_MESSAGE = "remote.message"
    CONTEXT_WARNING = "context.warning"
    COMPACT = "compact"
    INIT_COMPLETE = "init.complete"

    # In-terminal notification (long-running task completed/failed, etc.)
    NOTIFICATION = "notification"


__all__ = [
    "HookEvent",
    "dispatch_hook",
    "dispatch_hook_fire_and_forget",
    "reload_hooks",
]
