# NovaCode 1.2.65

## Themes

- Added `nova-obsidian`, a low-glare dark theme with indigo bars and peach highlights, and `nova-editorial`, a parchment theme with oxblood accents and plum activity indicators. Choose either with `/theme` in the Textual or Rust TUI.
- Softened the Textual Light, Catppuccin Latte, Solarized Light, Rosé Pine Dawn, and Atom One Light palettes while preserving saved theme names.
- The Rust TUI applies the selected palette to the workspace, composer, session tabs, and popups.

## Composer and Rust TUI

- The Textual composer groups agent selection and mode toggles in a responsive row. Serena and browser research remain accessible through the agent picker and their slash commands.
- The Rust composer shows the selected agent and active modes, supports keyboard and mouse controls, and exposes agent selection and MCP setup through the server.
- Rust responses use the active agent’s name and color. The worker panel shows subagent progress and context usage when available.

## Install or upgrade

```sh
uv tool install --python 3.11 --force "novacode-cli==1.2.65"
```

With optional browser, voice, server, Bedrock, and computer-use support:

```sh
uv tool install --python 3.11 --force "novacode-cli[browser,voice,agents-server,server,bedrock,computer]==1.2.65"
```
