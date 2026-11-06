"""OS-level shell sandbox (Pattern A) — confine local command execution.

Nova edits files directly on the host, but arbitrary shell commands are the real
blast radius (``rm -rf``, dependency installs, network egress). On Linux/macOS we
wrap each locally-executed command in an OS kernel sandbox that confines
filesystem *writes* to the workspace while leaving reads and network intact, so
the agent can still ``pip``/``npm``/``git`` but cannot scribble over ``/etc`` or
``$HOME``.

- **Linux:** ``bwrap`` (bubblewrap). The whole filesystem is bind-mounted
  read-only, then the workspace (and a writable HOME/cache) are bind-mounted
  read-write over it. Network is shared (allowed) by default.
- **macOS:** ``sandbox-exec`` with a generated Seatbelt profile that denies
  ``file-write*`` everywhere except the workspace and the temp dirs. Deprecated
  by Apple but still functional (the same primitive Codex/Claude Code rely on).
- **Windows / no backend:** returns the command unchanged — there is no
  lightweight primitive, so confinement degrades to the dangerous-command
  blocklist + HITL. Windows instead defaults to the Docker sandbox.

This module is intentionally dependency-free and side-effect-free apart from the
one-time backend probe, so :func:`wrap_command` is pure and unit-testable across
platforms by injecting the backend.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass

# Backend identifiers returned by :func:`detect_backend`.
BWRAP = "bwrap"
SANDBOX_EXEC = "sandbox-exec"

# Cached probe result. ``False`` means "not yet probed"; once probed it holds the
# backend string or ``None``.
_CACHED_BACKEND: str | None | bool = False


@dataclass
class OSSandboxPolicy:
    """Filesystem/network confinement policy for locally-run shell commands.

    Args:
        workspace_root: Absolute host path the command may write to.
        allow_network: When True (default), network egress is permitted inside
            the sandbox so dependency installs work. Filesystem confinement is
            independent of this.
        enabled: Master switch. When False, :func:`wrap_command` is a no-op
            (used for ``--no-sandbox`` / ``none`` and the Windows fallback).
        backend: Forced backend ("bwrap"/"sandbox-exec"/None). When None,
            :func:`wrap_command` calls :func:`detect_backend`. Injectable for
            tests.
    """

    workspace_root: str
    allow_network: bool = True
    enabled: bool = True
    backend: str | None = None
    writable_roots: tuple[str, ...] = ()


def detect_backend(*, force_probe: bool = False) -> str | None:
    """Return the available OS-sandbox backend for this platform, or None.

    Linux requires a working ``bwrap`` with unprivileged user namespaces (some
    hardened kernels disable them), so we run a real, harmless probe rather than
    just checking the binary exists. macOS only needs ``sandbox-exec`` on PATH.
    The result is cached for the process lifetime.
    """
    global _CACHED_BACKEND  # noqa: PLW0603
    if _CACHED_BACKEND is not False and not force_probe:
        return _CACHED_BACKEND  # type: ignore[return-value]

    backend: str | None = None
    try:
        if sys.platform.startswith("linux"):
            # Probe: bind / read-only and run `true`. Exercises the exact
            # user-namespace path real commands use; exit 0 ⇒ usable.
            proc = subprocess.run(
                ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "true"],  # noqa: S607
                capture_output=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0:
                backend = BWRAP
        elif sys.platform == "darwin":
            proc = subprocess.run(
                ["sandbox-exec", "-p", "(version 1)(allow default)", "true"],  # noqa: S607
                capture_output=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0:
                backend = SANDBOX_EXEC
        # win32 and everything else: no backend.
    except (OSError, subprocess.SubprocessError):
        backend = None

    _CACHED_BACKEND = backend
    return backend


def _bwrap_command(command: str, policy: OSSandboxPolicy) -> str:
    """Build a ``bwrap`` invocation that runs *command* confined to the workspace.

    The whole FS is read-only; the workspace and a writable HOME/cache are bound
    read-write so reads work everywhere but writes are contained. ``--die-with-parent``
    ties the sandbox lifetime to Nova so a killed turn never orphans it.

    ``policy.workspace_root`` is expected to already be an absolute host path
    (core_agent passes ``str(workspace_root)``); we don't re-absolutize it here
    so a POSIX path is never mangled when this module is imported on Windows.
    """
    ws = policy.workspace_root
    # A writable HOME inside the sandbox so pip/npm/uv caches (~/.cache, ~/.npm)
    # don't fail against the read-only root.
    args: list[str] = [
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",  # noqa: S108 — in-sandbox tmpfs, not a host path
        "--tmpfs",
        "/root/.cache",
        "--bind",
        ws,
        ws,
        "--chdir",
        ws,
        "--die-with-parent",
    ]
    args += ["--share-net"] if policy.allow_network else ["--unshare-net"]
    for root in policy.writable_roots:
        args += ["--bind", root, root]
    # bwrap runs the program directly (no shell): hand it bash -c <command>.
    args += ["bash", "-c", command]
    # We return a single shell string (callers use create_subprocess_shell), so
    # quote every arg. The inner command is one already-quoted token.
    return " ".join(shlex.quote(a) for a in args)


def _seatbelt_profile(policy: OSSandboxPolicy) -> str:
    """Generate a Seatbelt (.sb) profile: allow all, deny writes outside workspace."""
    ws = policy.workspace_root
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f"(allow file-write* (subpath {_sb_quote(ws)})"
        ' (subpath "/private/tmp") (subpath "/private/var/tmp")'
        ' (subpath "/dev"))',
    ]
    rules.extend(
        f"(allow file-write* (subpath {_sb_quote(root)}))" for root in policy.writable_roots
    )
    if not policy.allow_network:
        rules.append("(deny network*)")
    return "".join(rules)


def _sb_quote(path: str) -> str:
    """Quote a path for a Seatbelt profile string literal."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sandbox_exec_command(command: str, policy: OSSandboxPolicy) -> str:
    """Build a ``sandbox-exec`` invocation running *command* under a Seatbelt profile."""
    profile = _seatbelt_profile(policy)
    args = ["sandbox-exec", "-p", profile, "bash", "-c", command]
    return " ".join(shlex.quote(a) for a in args)


def wrap_command(command: str, policy: OSSandboxPolicy) -> str:
    """Return *command* wrapped in the OS sandbox, or unchanged when unavailable.

    The result is a shell command string (the local executor uses
    ``create_subprocess_shell``). When the policy is disabled or no backend is
    available the original command is returned verbatim — confinement degrades
    gracefully to the blocklist + HITL rather than failing the turn.
    """
    if not policy.enabled or not command:
        return command
    backend = policy.backend if policy.backend is not None else detect_backend()
    if backend == BWRAP:
        return _bwrap_command(command, policy)
    if backend == SANDBOX_EXEC:
        return _sandbox_exec_command(command, policy)
    return command


__all__ = [
    "BWRAP",
    "SANDBOX_EXEC",
    "OSSandboxPolicy",
    "detect_backend",
    "wrap_command",
]
