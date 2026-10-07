# Changelog - v1.0.5

## Improvements

- **Async subagent preference**: Nova now prefers available async specialists for independent background work, while retaining synchronous `task` delegation when results are needed immediately, no suitable async agent is available, or background dispatch fails.
