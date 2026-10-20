# NovaCode v1.0.53

## Added

- Agent teams and autonomous swarm switches in `/settings` under Experimental.
  Swarm requires teams; both remain disabled by default. Settings apply at the
  next prompt without changing the model or clearing the conversation.
- Usage examples for manual teams and worktree swarm tasks in the agent teams guide.

## Fixed

- Refresh team feature gates when rebuilding an existing session, including the
  swarm scheduler on a reused coordinator.
- Preserve ownership of existing workers when disabling teams. Running workers
  are not cancelled, new swarm assignments pause, and re-enabling teams restores
  access to the same coordinator.

## Verification and limits

- 83 focused settings, invocation and team tests passed offline. A final 36-test
  rerun also passed with a strengthened running-worker lifecycle regression.
- Type checks passed for configuration, invocation and team bootstrap. No new
  Ruff findings were introduced; existing formatting debt remains in the settings
  screen and its older tests.
- No new dependencies or paid calls. Live provider and container execution were
  not evaluated; agent teams and swarm remain experimental.
