# NovaCode v1.0.83

## Fixes

- Save LangSmith `/trace setup` settings across restarts. Store the API key in
  Nova's credential store and non-secret project, endpoint, workspace, and
  metadata settings in `Nova.config.json`.
- Restore saved tracing settings after credentials load and before the agent is
  created, so newly started sessions can send traces without repeating setup.
- Persist tracing enable and disable choices, and show the destination project
  where new prompts will appear.

## Verification

- Verify saved settings restore across a simulated restart and that the API key
  never enters the JSON config.
- Runtime tests cover setup changes and LangGraph chain, model, and tool traces
  using fake credentials and intercepted uploads; live account ingestion is not
  verified.
