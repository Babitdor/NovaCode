# NovaCode 1.2.51

## Code Mode

- When Nova blocks a repeated todo update, it temporarily removes `write_todos`
  from the model's tools. The tool becomes available again after a successful
  work call or at the start of a new user turn.

## Install or upgrade

Close NovaCode before upgrading from your shell.

```text
uv tool upgrade novacode-cli
```

For a fresh installation, run `uv tool install --python 3.11 novacode-cli`.
Include any optional extras you use.

```text
uv tool install --python 3.11 "novacode-cli[browser,voice]"
```
