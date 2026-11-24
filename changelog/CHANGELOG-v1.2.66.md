# NovaCode 1.2.66

## Agent collaboration

- Nova, Nora, and Serena now get clearer guidance on when to delegate. Ralph handles measurable experiments, Nora verifies sources, Serena handles precise code changes, and Nova handles broader implementation and integration.
- Background worker briefs specify the expected result, verification, and whether edits are allowed. The requesting agent can continue working while the worker runs and collects its result before ending the turn.
- Nora can inspect local source material using read-only tools and access additional search and extraction operations from trusted Tavily, Parallel, Exa, and Firecrawl MCP servers.
- Ralph can load a bundled autoresearch playbook in Code Mode. It covers baselines, reversible experiments, correctness checks, and stopping conditions.
- Serena's prompts now direct directory inspection to `list_dir` and reserve `read_file` for confirmed file paths.

## Textual output

- Coalesced inline terminal repaints and reduced redundant layout work for busy logs while preserving reflow when a line wraps.
- Moved ANSI decoding for live tool output off the paint path and increased background shell read sizes.

## Install or upgrade

```sh
uv tool install --python 3.11 --force "novacode-cli==1.2.66"
```

To include the optional browser, voice, server, Bedrock, and computer-use features, use the extras below.

```sh
uv tool install --python 3.11 --force "novacode-cli[browser,voice,agents-server,server,bedrock,computer]==1.2.66"
```
