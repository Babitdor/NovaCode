"""Tests for Pattern A: OS-level shell confinement and sandbox-mode resolution.

Covers the pure wrapper logic in ``novacode_cli.shell.os_sandbox`` (cross-platform
via injected backend) and the ``resolve_sandbox_type`` default-resolution helper in
``novacode_cli.main``.
"""

from __future__ import annotations

import shlex

import pytest

from novacode_cli.shell import os_sandbox
from novacode_cli.shell.os_sandbox import (
    BWRAP,
    SANDBOX_EXEC,
    OSSandboxPolicy,
    wrap_command,
)

WS = "/home/user/project"


# ── wrap_command: bwrap (Linux) ──────────────────────────────────────────────


def test_bwrap_wraps_command_and_confines_workspace():
    policy = OSSandboxPolicy(workspace_root=WS, backend=BWRAP)
    wrapped = wrap_command("pytest -q", policy)

    assert wrapped != "pytest -q"
    assert "bwrap" in wrapped
    # Whole FS read-only, workspace bound read-write over it, chdir into it.
    assert "--ro-bind" in wrapped
    assert "--bind" in wrapped
    assert WS in wrapped
    assert "--chdir" in wrapped
    assert "--die-with-parent" in wrapped
    # The original command is handed to bash -c (as a single quoted token).
    assert "bash -c" in wrapped
    assert "pytest -q" in wrapped


def test_bwrap_cache_uses_private_tmpfs_for_unprivileged_users():
    args = shlex.split(wrap_command("true", OSSandboxPolicy(WS, backend=BWRAP)))
    assert args[args.index("--setenv") + 1 : args.index("--setenv") + 3] == [
        "XDG_CACHE_HOME",
        "/tmp/nova-cache",  # noqa: S108 — private sandbox tmpfs, not a host directory
    ]
    assert "HOME" not in args
    assert "/root/.cache" not in args


def test_bwrap_allows_network_by_default():
    wrapped = wrap_command("pip install x", OSSandboxPolicy(workspace_root=WS, backend=BWRAP))
    assert "--share-net" in wrapped
    assert "--unshare-net" not in wrapped


def test_bwrap_blocks_network_when_disabled():
    policy = OSSandboxPolicy(workspace_root=WS, backend=BWRAP, allow_network=False)
    wrapped = wrap_command("curl example.com", policy)
    assert "--unshare-net" in wrapped
    assert "--share-net" not in wrapped


# ── wrap_command: sandbox-exec (macOS) ───────────────────────────────────────


def test_sandbox_exec_builds_seatbelt_profile():
    policy = OSSandboxPolicy(workspace_root=WS, backend=SANDBOX_EXEC)
    wrapped = wrap_command("ls", policy)

    assert "sandbox-exec" in wrapped
    assert "-p" in wrapped
    # allow-all then deny writes, re-allowing only the workspace + temp dirs.
    assert "(allow default)" in wrapped
    assert "(deny file-write*)" in wrapped
    assert WS in wrapped
    assert "bash -c" in wrapped


def test_sandbox_exec_network_allowed_by_default_blocked_when_disabled():
    allowed = wrap_command("ls", OSSandboxPolicy(workspace_root=WS, backend=SANDBOX_EXEC))
    assert "(deny network*)" not in allowed

    blocked = wrap_command(
        "ls", OSSandboxPolicy(workspace_root=WS, backend=SANDBOX_EXEC, allow_network=False)
    )
    assert "(deny network*)" in blocked


# ── wrap_command: graceful degradation ───────────────────────────────────────


def test_disabled_policy_is_noop():
    policy = OSSandboxPolicy(workspace_root=WS, enabled=False, backend=BWRAP)
    assert wrap_command("echo hi", policy) == "echo hi"


def test_no_backend_returns_command_unchanged(monkeypatch: pytest.MonkeyPatch):
    # enabled, but the platform has no usable backend → run unconfined.
    monkeypatch.setattr(os_sandbox, "detect_backend", lambda: None)
    policy = OSSandboxPolicy(workspace_root=WS, backend=None)
    assert wrap_command("echo hi", policy) == "echo hi"


def test_empty_command_passthrough():
    assert wrap_command("", OSSandboxPolicy(workspace_root=WS, backend=BWRAP)) == ""


# ── resolve_sandbox_type: default-mode resolution ────────────────────────────


@pytest.mark.parametrize(
    ("arg", "no_sandbox", "platform", "expected"),
    [
        (None, False, "linux", ("os", False)),
        (None, False, "darwin", ("os", False)),
        # Windows default is plain host execution, not Docker (Docker is opt-in).
        (None, False, "win32", ("none", False)),
        ("none", False, "linux", ("none", True)),
        ("os", False, "win32", ("os", True)),
        ("modal", False, "linux", ("modal", True)),
        ("docker", False, "win32", ("docker", True)),
        (None, True, "linux", ("none", True)),
    ],
)
def test_resolve_sandbox_type(
    arg: str | None,
    no_sandbox: bool,  # noqa: FBT001
    platform: str,
    expected: tuple[str, bool],
):
    from novacode_cli.integrations.sandbox_factory import resolve_sandbox_type

    assert resolve_sandbox_type(arg, no_sandbox, platform=platform) == expected


def test_resolve_rejects_docker_off_windows():
    from novacode_cli.integrations.sandbox_factory import resolve_sandbox_type

    with pytest.raises(ValueError, match="Windows-only"):
        resolve_sandbox_type("docker", no_sandbox=False, platform="linux")


def test_resolve_rejects_no_sandbox_with_explicit_sandbox():
    from novacode_cli.integrations.sandbox_factory import resolve_sandbox_type

    with pytest.raises(ValueError, match="conflicts"):
        resolve_sandbox_type("modal", no_sandbox=True, platform="linux")
