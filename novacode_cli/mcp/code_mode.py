"""Compatibility import for the original MCP-only Code Mode middleware."""

from novacode_cli.agents.code_mode import CodeModeMiddleware as MCPCodeModeMiddleware

__all__ = ["MCPCodeModeMiddleware"]
