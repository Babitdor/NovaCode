# NovaCode v1.0.55

## Fixed

- Fixed async subagent dispatch rejecting requests that combined Nova's run context
  with deepagents' redundant parent-thread configurable. Parent lookup continues
  through the child thread metadata.
- Preserve unrelated configurable values and omit Nova context for those runs to
  avoid LangGraph's mutually exclusive `configurable` and `context` request fields.

## Verification

- 18 async model context tests passed.
- 15 configured async subagents accepted and completed through an isolated local
  Agent Server using scripted models.
- 78 in-process and 78 dynamic subagent checks passed.
- Ruff formatting, focused lint and `git diff --check` passed.
