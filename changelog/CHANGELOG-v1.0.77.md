# NovaCode v1.0.77

## Fixes

- Apply the shared bounded model retry policy to built-in in-process subagents, file-defined dynamic agents, and team workers. Normal async execution allows at most two Nova retries within a four-minute model-call budget, disables nested OpenAI-compatible/Anthropic SDK retries, and keeps slow tool execution outside that budget.
- Display local subagents as plain, non-clickable status lines in the chat transcript. Update the same line on completion, failure or cancellation instead of mounting expandable cards and local task panels.
- Preserve inline task status through cancellation and isolate it between session tabs. Remote async tasks retain their existing task panel and persistent monitoring.
- Add offline compiled-graph and TUI regression coverage for request limits, stalled calls, slow tools, duplicate events, clicks, cancellation and session cleanup.
