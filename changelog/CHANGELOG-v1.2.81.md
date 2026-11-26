# NovaCode 1.2.81

## Fixed: first launch after a PC restart hung on a blank terminal

The agent build consulted models.dev for gateway-model context windows via
`urllib` on the UI loop. The 10-second socket timeout does not bound DNS
resolution (`getaddrinfo`), so on a freshly booted machine with a cold
resolver the fetch blocked the blank terminal for minutes — while a second
instance (warm resolver) worked instantly. It only bit when the weekly
models.dev disk cache was stale, which is why it was intermittent.

- The models.dev fetch now runs on a single-flight daemon thread; callers
  wait at most 3 seconds and fall back to the stale disk cache while the
  thread populates the shared state for later reads.
- `entrypoint` marks `process_start` before anything else, so the previously
  unlogged blank window is measured on the next cold boot — if a hang ever
  reappears, `~/.nova/logs/startup.log` names the exact phase.

Found via the startup trace: the cold-boot launch logged boot_animation but
never `agent_built` — the hang sat inside the agent build's only unbounded
network call.
