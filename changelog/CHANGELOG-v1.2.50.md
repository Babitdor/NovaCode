# NovaCode 1.2.50

## Context usage

- The `/context` dashboard and `ctx` meter use the same model request count.
  A checkpoint refresh uses the provider count when available and the request
  estimate otherwise.
- A live count received during a checkpoint read takes precedence. Turn-end
  usage totals preserve the latest context count.

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
