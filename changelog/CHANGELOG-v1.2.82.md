# NovaCode 1.2.82

## Startup: faster boots

- One-shot CLI subcommand imports (skills, mcp, help, migrate) are now lazy —
  pipe/headless sessions never touch them at startup, and command-only runs
  skip the LangChain-heavy `ralph_handler` chain entirely (~1.4s).
- The async-subagent TCP probe dropped from 400ms to 150ms: a live local
  server answers a connect in single-digit ms, and an unreachable one burns
  the whole timeout — twice per boot.

Measured: pipe boot to ready 8.1s → 6.1s; one-shot command runs save 2–3s.
