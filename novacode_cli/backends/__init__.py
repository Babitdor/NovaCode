"""Nova's custom backend wrappers extending deepagents backends.

Provides performance and feature enhancements to deepagents backends while
maintaining full backward compatibility.
"""

from novacode_cli.backends.filesystem import (
    AgentMemoryBackend,
    ConversationHistoryBackend,
    OptimizedFilesystemBackend,
    OptimizedLocalShellBackend,
)

__all__ = [
    "AgentMemoryBackend",
    "ConversationHistoryBackend",
    "OptimizedFilesystemBackend",
    "OptimizedLocalShellBackend",
]
