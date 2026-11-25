# NovaCode 1.2.71

## Serena cancellation

- Pressing Esc during an active Serena tool call now cancels the turn promptly while Nova shuts down the MCP process and its children.
- The next Serena call reconnects automatically once the previous process has stopped.

## Install or upgrade

```sh
uv tool install --python 3.11 --force "novacode-cli==1.2.71"
```

To include browser, voice, server, Bedrock, and computer-use extras, use the command below.

```sh
uv tool install --python 3.11 --force "novacode-cli[browser,voice,agents-server,server,bedrock,computer]==1.2.71"
```
