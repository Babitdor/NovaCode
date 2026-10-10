"""Guards Nova's monkey-patches into deepagents internals.

``core_agent`` reaches into three private-ish deepagents symbols and replaces
them at import time:

* ``backends.utils.validate_path`` — accept Windows absolute paths
* ``backends.utils.perform_string_replacement`` — tolerate a leading UTF-8 BOM
* ``middleware.subagents.create_sub_agent`` — cache the compiled subagent graph

Patching by name means a deepagents upgrade can silently break any of them: a
renamed function makes the patch a no-op (the fix quietly disappears), and a
changed signature makes it raise at call time. Neither shows up as an import
error, so this file pins the contract each patch depends on.

If one of these fails after a deepagents bump, the patch in
``novacode_cli/agents/core_agent.py`` needs updating — not this test.
"""

from __future__ import annotations

import inspect

import pytest

# Importing core_agent is what installs the patches.
import novacode_cli.agents.core_agent  # noqa: F401
import deepagents.backends.utils as da_utils
import deepagents.middleware.subagents as da_subagents


def test_patch_targets_still_exist():
    """The symbols Nova replaces must still be there to replace."""
    assert callable(da_utils.validate_path)
    assert callable(da_utils.perform_string_replacement)
    assert callable(da_subagents.create_sub_agent)


def test_patches_are_actually_installed():
    """A rename upstream would leave the originals in place — silently unpatched."""
    assert da_utils.validate_path.__name__ == "_patched_validate_path"
    assert (
        da_utils.perform_string_replacement.__name__
        == "_patched_perform_string_replacement"
    )
    assert da_subagents.create_sub_agent.__name__ == "_cached_create_sub_agent"


def test_validate_path_keeps_its_keyword_contract():
    """Nova's patch forwards ``allowed_prefixes`` by keyword."""
    params = inspect.signature(da_utils.validate_path).parameters
    assert "path" in params
    assert "allowed_prefixes" in params


def test_windows_absolute_path_is_normalized():
    """The reason the patch exists: LLMs emit ``C:\\...`` on Windows."""
    assert da_utils.validate_path("C:/nope/some_file.py").startswith("/")


def test_bom_prefixed_content_can_still_be_edited():
    """A leading U+FEFF must not make the first edit of a file fail."""
    out = da_utils.perform_string_replacement("\ufeffhello world", "hello", "goodbye")
    text = out[0] if isinstance(out, tuple) else out
    assert "goodbye" in text


def test_create_sub_agent_signature_is_what_the_cache_wraps():
    """The cache forwards ``state_schema``/``response_format`` by keyword."""
    params = inspect.signature(da_subagents.create_sub_agent).parameters
    assert "spec" in params
    assert "state_schema" in params
    assert "response_format" in params


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("deepagents", "create_deep_agent"),
        ("deepagents", "RubricMiddleware"),
        ("deepagents.backends", "CompositeBackend"),
        ("deepagents.backends.protocol", "BackendProtocol"),
        ("deepagents.backends.protocol", "SandboxBackendProtocol"),
        ("deepagents.backends.store", "StoreBackend"),
        ("deepagents.middleware.subagents", "SubAgent"),
        ("deepagents.middleware.subagents", "GENERAL_PURPOSE_SUBAGENT"),
        ("deepagents.middleware.async_subagents", "AsyncSubAgent"),
        ("deepagents.middleware.async_subagents", "AsyncSubAgentMiddleware"),
    ],
)
def test_imported_deepagents_symbols_exist(module: str, name: str):
    """Every deepagents symbol Nova imports by name across the codebase."""
    mod = __import__(module, fromlist=[name])
    assert hasattr(mod, name), f"{module}.{name} disappeared"


def test_create_deep_agent_accepts_every_kwarg_nova_passes():
    """core_agent builds the graph with these; a dropped kwarg breaks startup."""
    from deepagents import create_deep_agent

    params = inspect.signature(create_deep_agent).parameters
    for kwarg in (
        "name",
        "model",
        "skills",
        "system_prompt",
        "tools",
        "checkpointer",
        "backend",
        "middleware",
        "store",
        "interrupt_on",
        "subagents",
    ):
        assert kwarg in params, f"create_deep_agent no longer accepts {kwarg!r}"


def test_task_tool_names_its_dispatch_on_the_subagents_stream():
    """The subagents panel previews a row by the id the task tool stamps.

    Breaks if deepagents renames ``_build_task_tool`` (the patch becomes a no-op)
    or stops running the subagent under the tool's ambient config.
    """
    import asyncio

    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG002
            return self

    call = {"name": "task", "args": {"description": "do it", "subagent_type": "helper"}}
    main = Model(
        responses=[
            AIMessage(content="", tool_calls=[{**call, "id": "call_A"}, {**call, "id": "call_B"}]),
            AIMessage(content="done"),
        ]
    )
    helper = {
        "name": "helper",
        "description": "h",
        "system_prompt": "p",
        # One reply per dispatch: a repeated message object is streamed only once.
        "model": Model(responses=[AIMessage(content="hi"), AIMessage(content="hello")]),
        "tools": [],
    }
    agent = create_deep_agent(model=main, subagents=[helper])

    async def stamped() -> set:
        seen = set()
        stream = agent.astream(
            {"messages": [("user", "x")]}, stream_mode=["messages"], subgraphs=True
        )
        async for namespace, _mode, (_message, metadata) in stream:
            if namespace:
                seen.add(metadata.get("nova_task_id"))
        return seen

    assert asyncio.run(stamped()) == {"call_A", "call_B"}
