# NovaCode 1.2.49

## Updates and conversation history

- Nova now checks the installed version before reporting a package update as
  successful. On Windows, a locked launcher is reported as an error when the
  installed package did not advance.
- Resumed tabs and older scrollback hide system prompts and internal setup
  messages.

## Install or upgrade

Close NovaCode before upgrading from your shell.

```text
uv tool upgrade novacode-cli
```

For a fresh installation, use `uv tool install --python 3.11 novacode-cli`.
Include any optional extras you use.

```text
uv tool install --python 3.11 "novacode-cli[browser,voice]"
```
