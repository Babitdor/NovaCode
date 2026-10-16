# NovaCode v1.0.39

## Improved

- Redesigned the home screen with navigation tabs, quick task starters, and theme-aware system, model, task, and context panels.
- Added live CPU, RAM, and network counters through the new direct `psutil` dependency. The composer spans the terminal width and navigation tabs align left.
- Goals now retain their objective, lifecycle, and turn budget across session saves. Added pause, resume, and budget controls to the TUI, shared commands, and child sessions.
- Goal continuation yields to planning and queued input, pauses on interruption, avoids tool-free loops, and requires an explicit completion declaration with evidence.

## Fixed

- Added a reducer for concurrent `_context_snapshot` writes, preventing the remote `INVALID_CONCURRENT_GRAPH_UPDATE` error while preserving a complete diagnostic.
- Prevented completed or paused goals and old rubrics from leaking into later agent requests or background threads.
- Restored the full conversation area when a shell command starts from the home screen.

## Validation and limits

- Goal, agent-stream, worker, headless, and snapshot checks passed (144 tests in one focused run); final TUI/session/worker checks passed (114 tests in another run). These runs overlap.
- Targeted Ruff and mypy checks passed. The wider repository retains existing lint findings.
- The full suite reached a shell background-handoff timeout. That timeout, three context-metric assertion failures, and the async delegation prompt assertion failure were reproduced on unchanged `7f4b87e`.
- One TUI scroll-timing assertion failed in the broad TUI run and passed on its focused rerun and subsequent session run.
