"""Legacy one-shot tool execution cannot inherit lead authority."""

from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage

from novacode_cli.teams.gateway import Binding, current_binding
from novacode_cli.teams.legacy import LegacyMailboxMiddleware, mailbox_identity
from novacode_cli.teams.models import Identity


@pytest.mark.parametrize("fails", [False, True])
async def test_legacy_middleware_scopes_identity_and_restores_after_handler(*, fails: bool):
    binding = Binding(Identity("project", "root", team="team"), AsyncMock())
    middleware = LegacyMailboxMiddleware("reviewer")
    token = current_binding.set(binding)
    response = ToolMessage("done", tool_call_id="call")

    async def handler(request: ToolMessage) -> ToolMessage:
        assert request is response
        assert current_binding.get() is None
        assert mailbox_identity.get() == ("project", "root", "team", "reviewer")
        if fails:
            message = "tool failed"
            raise RuntimeError(message)
        return response

    try:
        if fails:
            with pytest.raises(RuntimeError, match="tool failed"):
                await middleware.awrap_tool_call(response, handler)
        else:
            assert await middleware.awrap_tool_call(response, handler) is response
        assert current_binding.get() is binding
        assert mailbox_identity.get() is None
    finally:
        current_binding.reset(token)
