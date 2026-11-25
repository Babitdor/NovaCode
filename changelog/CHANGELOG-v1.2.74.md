# NovaCode 1.2.74

## Fixed: TUI freezes while the agent works (input dead, tasks still running)

The stall watchdog recorded the loop blocked during normal agent turns; the
two repeat offenders are fixed:

- **OS keychain reads on the UI loop.** Every Code Mode tool dispatch called
  `trusted_browser_servers()`, which constructed a fresh `NovaConfig` — whose
  `Settings.from_environment()` reads the Windows credential vault (bounded at
  3 s). A slow vault read froze all input for its duration, once per tool
  call. The trusted-server list is now cached by config content hash + home
  path + reader identity: repeats read one small JSON file; any config edit or
  redirected home invalidates.
- **Cold adapter imports on the UI loop.** The hermes learning review
  unconditionally imported `langchain_anthropic` / `langchain_openai` even
  when the session's model used the other adapter — a cold import measured at
  6.6 s on the loop. Adapters are now only imported when already loaded; the
  isinstance semantics are unchanged.

Diagnosis data came from Nova's own stall watchdog (`~/.nova/logs/freeze.log`),
which samples the loop thread's stack whenever it stops answering.
