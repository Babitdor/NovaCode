# NovaCode 1.2.43

## Code Mode

- `eval` resolves final tool promises and async function calls. A race still
  cancels outstanding calls after selecting its winner.
- Stop saves completed operations and named checkpoints from the interrupted
  snippet in the conversation and shows the receipt on its tool card. Already
  started external operations may still finish.
- A stopped interpreter is removed from reuse immediately and closes after its
  worker has finished unwinding.
- Calls to unknown subagents produce a failed operation. Successful races keep
  their success status and show cancelled operations in the card.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.43
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use.

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.43"
```
