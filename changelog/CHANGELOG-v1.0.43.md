# NovaCode v1.0.43

## Fixed

- Start approved TUI plans in a fresh execution conversation with the complete plan text, including plans from `/plan`, `/claude-plan`, `/codex-plan`, and main-agent self-planning.
- Import implementation steps and verification checks into the execution todo list before starting. Reject todo updates that remove or rename imported plan items, and report items left open after execution.
- Stop the handoff if the approved plan cannot be loaded, preserve the planning backend until plan resolution, and remove plan-debug chat messages.

## Validation and limits

- The focused plan, command handoff, agent stream, middleware, path, and archive suite passed 82 tests.
- Ruff, formatting, and mypy checks pass for the new plan execution module; `git diff --check` passes.
- The full suite timed out before producing a final report. Existing legacy modules retain lint findings. No new dependencies were added.
- Todo tracking does not prove semantic completion by a model. The existing done gate nudges once when items remain open, and the TUI reports remaining items.
