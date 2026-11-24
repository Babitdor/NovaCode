# NovaCode 1.2.68

## Agent workers

- Worker previews show startup, live tool calls and results, errors, and requests for input. The Textual preview shows a short worker ID so workers with the same agent name are easy to distinguish.
- `summon_agent` returns long results from the beginning in pages. The requesting agent can retrieve subsequent pages from the same worker via `next_offset` and sees a truncation flag if the result exceeds the retention limit.

## Install or upgrade

```sh
uv tool install --python 3.11 --force "novacode-cli==1.2.68"
```

To include browser, voice, server, Bedrock, and computer-use extras, use the command below.

```sh
uv tool install --python 3.11 --force "novacode-cli[browser,voice,agents-server,server,bedrock,computer]==1.2.68"
```
