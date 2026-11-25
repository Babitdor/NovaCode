# NovaCode 1.2.73

## Fixed: Serena MCP failing to start

- The Serena preset launched via `uvx` without a Python version pin, so uv
  picked the machine's newest interpreter. Serena pins `pyyaml==6.0.2`, which
  has no wheels for Python 3.13+/3.14 — uv then attempted a source build that
  requires MSVC C++ Build Tools, and the server died before speaking MCP.
- The preset now passes `--python 3.11` to `uvx`, so the launch resolves to an
  interpreter with prebuilt wheels regardless of the machine default.
- Existing installs keep their saved configuration; re-adding Serena via
  `/mcp` picks up the pinned command.
