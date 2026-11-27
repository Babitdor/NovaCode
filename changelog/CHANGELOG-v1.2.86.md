# NovaCode 1.2.86

## Fixed

- MCP servers that drift from their startup preset (a version pin like
  `browser-use==1.0.6`, custom env, extra flags) no longer print their logs
  over the TUI. The SDK defaults a stdio server's stderr to the terminal, so
  Serena's INFO lines and friends scrolled across the screen mid-session;
  every stdio server's stderr is now captured to
  `~/.nova/logs/mcp/<server>.log` — readable after the fact, never on screen.
  This also removes a big source of the display corruption when server logs
  interleaved with the UI's frame writes.
