"""Sandbox integrations for NovaCode CLI.

This package provides sandbox backends for executing commands in isolated
environments. Each backend implements ``SandboxBackendProtocol`` from
deepagents.

Provider Protocol
-----------------
All sandbox providers follow the ``SandboxProvider`` protocol::

    @contextmanager
    def create_provider_sandbox(
        *, sandbox_id=None, setup_script_path=None, **kwargs
    ) -> Generator[SandboxBackendProtocol, None, None]:
        ...

Use the unified ``create_sandbox(provider, ...)`` in :mod:`sandbox_factory`
to dispatch by provider name.

Built-in backends (in :mod:`sandbox_factory`):
    - docker — Docker containers
    - modal — Modal.com ephemeral sandboxes
    - runloop — Runloop devboxes
    - daytona — Daytona sandboxes
    - inmemory — In-memory test adapter (no Docker/Modal SDK needed)
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Protocol, runtime_checkable

from deepagents.backends.protocol import SandboxBackendProtocol


@runtime_checkable
class SandboxProvider(Protocol):
    """Protocol for sandbox provider factories.

    A sandbox provider is a context manager that yields a
    ``SandboxBackendProtocol`` instance and cleans up on exit.

    The minimal signature shared by all providers::

        @contextmanager
        def provider(
            *, sandbox_id=None, setup_script_path=None
        ) -> Generator[SandboxBackendProtocol, None, None]:
            ...

    Providers may accept additional keyword arguments (e.g. Docker accepts
    ``ports``, ``mount_dir``, ``persist``, ``session_id``). Those are passed
    through by :func:`~novacode_cli.integrations.sandbox_factory.create_sandbox`.
    """

    def __call__(
        self,
        *,
        sandbox_id: str | None = None,
        setup_script_path: str | None = None,
        **kwargs,  # noqa: ANN401
    ) -> Generator[SandboxBackendProtocol, None, None]:
        """Create or connect to a sandbox.

        Args:
            sandbox_id: Optional existing sandbox ID to reuse.
            setup_script_path: Optional path to setup script to run after start.

        Yields:
            A sandbox backend instance.

        Raises:
            RuntimeError: If the sandbox creation fails.
        """
        ...  # pragma: no cover


# Re-export SandboxBackendProtocol so consumers can import from here.
__all__ = [
    "SandboxProvider",
    "SandboxBackendProtocol",
]
