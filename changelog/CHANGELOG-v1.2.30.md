# NovaCode 1.2.30

## Responsive MCP status and clickable Nova

- Stop rescanning and validating MCP configuration every second on the TUI
  thread. Connection status reuses the shared discovery task, and discovery
  builds configuration in a worker thread to keep input responsive.
- Left-click Nova to play a four-second happy reaction, including during active
  work. Repeated clicks restart it, then the current activity animation resumes.
  Character and native-image rendering keep the same bottom-right position.
- Clarify Code Mode delegation prompts with a native `task(...)` example,
  self-contained agent instructions, parallel calls and background-result handling.
- Remove the obsolete Vixie desktop-pet WebSocket server and its execution
  callbacks. The terminal `/nova` avatar remains available independently.
  Remove any legacy `novacode_cli.vixie.hook_adapter` entry from your hooks config.

## Validation

Regression checks cover MCP scan responsiveness, repeated status polling, native
and character avatar clicks, resumed activity, shutdown and execution cleanup,
and Code Mode delegation. A 240-call headless Code Mode simulation retained
bounded transcript history and no completed-tool tracking entries.

Physical terminal mouse behavior and long live model sessions have not been
manually verified. No new dependency is required.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.30
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Preserve any optional extras you use:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.30"
```
