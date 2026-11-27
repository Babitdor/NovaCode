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

v1.2.89 installs were affected; this release supersedes it.
