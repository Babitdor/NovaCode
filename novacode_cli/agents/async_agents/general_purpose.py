"""General-purpose delegation on Nova's background agent server."""

from __future__ import annotations

from typing import Any


def build_graph() -> Any:
    """Build with the server's workspace, async model role and unattended policy."""
    from deepagents import create_deep_agent
    from deepagents.backends.filesystem import FilesystemBackend

    from novacode_cli.agents.async_agents._specialist import BACKGROUND_NOTE
    from novacode_cli.agents.async_context import AsyncModelOverrideMiddleware, NovaAsyncContext
    from novacode_cli.agents.async_workspace import workspace_root
    from novacode_cli.agents.model_retry import NovaModelRetryMiddleware
    from novacode_cli.config.model_create import build_async_agent_model
    from novacode_cli.prompts import render_template
    from novacode_cli.security.delegated_approval import DelegatedApprovalMiddleware
    from novacode_cli.shell.middleware import ShellMiddleware
    from novacode_cli.skills.libraries import background_library
    from novacode_cli.tools import docs_search, fetch_url, package_info, web_search

    root = workspace_root()
    return create_deep_agent(
        name="general-purpose-async",
        model=build_async_agent_model(),
        tools=[web_search, docs_search, fetch_url, package_info],
        backend=FilesystemBackend(root_dir=str(root), virtual_mode=True),
        system_prompt=render_template("async_agents/general_purpose.jinja") + BACKGROUND_NOTE,
        middleware=[
            AsyncModelOverrideMiddleware(),
            NovaModelRetryMiddleware(),
            DelegatedApprovalMiddleware(),
            ShellMiddleware(workspace_root=str(root)),
            background_library("general-purpose-async"),
        ],
        context_schema=NovaAsyncContext,
    )


graph = build_graph()
