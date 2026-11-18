# NovaCode 1.2.42

## Code Mode

- Stop releases the turn during interpreter startup. Nova discards a worker
  that finishes starting after cancellation.
- Nested tools recheck that the conversation is active just before dispatch.
- Settled JavaScript races return the winning result and cancel pending sibling
  tool calls. The next snippet restores the tool bridge if JavaScript reassigned
  it.
- Timeout reports identify the interpreter, script, or nested tool deadline.
  Transport errors during cancellation appear alongside the nested tool timeout.
- Named checkpoints preserve small selected results after a script failure or
  timeout. Background shell task IDs stay in the receipt so Nova can recover
  test results and logs from the original job.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.42
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use.

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.42"
```
