# v1.2.90 — Restore the missing tavily dependency

**Released 2026-11-27** · PyPI `novacode-cli==1.2.90` · tag `v1.2.90`

## Fix: "Missing required CLI dependencies" on every launch

The v1.2.89 release accidentally dropped `tavily-python` from the project
dependencies while re-pinning `langsmith`. With the package missing,
`check_cli_dependencies` aborted every Nova launch — including the process
behind `/sessions` — with:

```
❌ Missing required CLI dependencies!
  - tavily-python
```

- `tavily-python` is declared again (resolved to 0.8.5) and back in `uv.lock`.
- A new guard test (`tests/test_cli_dependency_declaration.py`) parses
  `check_cli_dependencies` and fails if any package it gates on is no longer
  declared in `pyproject.toml`, so an edit can never silently strand a
  dependency again.

## Fix: the home banner fills its container in every home style

The Matrix-rain banner pinned its own width to the first grid guess, and the
dashboard never re-gridded on resize (the correction only ran for the
classic/signal viewport path). Each home style's chain between the terminal
and the banner differs — classic's header border 2 columns, dashboard's header
padding 2, the assumed transcript padding 4 — so the grid locked in whatever
the first guess got wrong and left a blank band down the right side of the
backdrop. The banner now always fills its container, every resize re-grids
from the laid-out box, and rendering pulls one refit per box/grid mismatch
(a resize event cannot fire when the box itself no longer changes). The banner
test runs all three home styles through an isolated config with a settle loop
instead of asserting mid-convergence with whatever `home_style` a developer's
own config leaks in.

v1.2.89 installs were affected; this release supersedes it.
