# NovaCode 1.2.70

## Startup and agent profiles

- Nova provisions Serena and Browser Use and connects them in the background on launch. Existing MCP definitions and disabled settings are preserved. Serena and Nora unlock after their allowed tools are discovered.
- Nora can use the built-in Browser Use screenshot tool alongside Nova’s search and page-fetch tools. Existing browser trust selections take precedence.
- The startup spinner gives feedback while the agent runtime loads.
- Nova, Nora, Serena, and Ralph have clearer handoff instructions for research, symbol-level edits, integration, and measurable experiments.
- Serena can ask Nova to launch long shell commands as background tasks and return their task IDs.

## Ratatui

- Run `nova --ratatui` to opt into the Rust TUI. The client includes styled Markdown, readable diffs, tool result cards, and live activity indicators.

## Install or upgrade

```sh
uv tool install --python 3.11 --force "novacode-cli==1.2.70"
```

To include browser, voice, server, Bedrock, and computer-use extras, use the command below.

```sh
uv tool install --python 3.11 --force "novacode-cli[browser,voice,agents-server,server,bedrock,computer]==1.2.70"
```
