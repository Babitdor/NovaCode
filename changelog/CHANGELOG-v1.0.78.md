# NovaCode v1.0.78

## Fixes

- Classify `spawn_agent` teammates separately from remote LangGraph async tasks. Team workers now use inline status lines and cannot open the remote preview that incorrectly reported "The agent server is not available."
- Keep persistent teammates marked running when the lead finishes or cancels its current turn. Team lifecycle events update the same inline line when the teammate finishes.
- Add offline coverage for real in-process lifecycle events, `spawn_agent` tool invocation without server startup, and team status rendering without remote-server discovery.
