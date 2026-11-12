# NovaCode 1.2.11

## Session loops (#10)

- Add `/loop` with duration intervals, quoted five-field cron schedules and
  model-selected wakeups through `schedule_wakeup`.
- Reload project/user `loop.md` instructions on every file-driven iteration,
  with `.nova` taking precedence and `.agent` accepted as a compatibility alias.
- Run iterations through the normal session agent and approval paths. Wait while
  busy and coalesce missed ticks without overlapping turns or catch-up queues.
- Add `/loop list`, `/loop status`, `/loop stop [id|all]`, waiting-state Escape
  cancellation, and a default 72-hour TTL configurable up to seven days.
- Keep loops in memory, discard them on conversation reset/exit, and fence
  expired or stale-session work. Expiration produces one final notification.
- Support nested agent-driven skill invocations. Spawned-process/headless tabs
  do not host loops; switching away from a local owner tab pauses its dispatch.

## Tracing compatibility

- Fix dropped LangGraph traces with the updated LangSmith SDK when optional
  agent metadata labels are configured alongside a project.
- Migrate Nova's labels to `NOVA_LANGSMITH_AGENT_ID` and
  `NOVA_LANGSMITH_AGENT_ENVIRONMENT`, preserving saved setup and legacy inputs
  while keeping them separate from the SDK's reserved destination variables.

## Learning and memory reviews (#4, #8)

- Revalidate retrieval relevance, fallback behavior, learning approval/activation,
  dream isolation, wiki maintenance, memory tiers and context properties/replay:
  **218 passed, 2 skipped**.
- The loop/session integration selection passed **83 tests**. Tracing runtime,
  setup, modal and TUI compatibility checks passed **21 tests** after reproducing
  the prior CI failure. The scheduler modules passed Ruff and Mypy.
- These are offline contract checks; live-model quality and long-running external
  deployments are not certified. No new runtime dependency is required.

## Install or upgrade

```text
uv tool install --python 3.11 --force "novacode-cli==1.2.11"
```

Select optional extras as needed, for example:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice,agents-server,server,bedrock,computer]==1.2.11"
```

Existing PyPI installations can use `uv tool upgrade novacode-cli`.
