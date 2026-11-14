# NovaCode 1.2.23

## Lower Code Mode context overhead

- Compact complete successful `eval` and `MCPCode` responses in model requests.
  Keep the selected result and ordered operation identities while removing
  redundant success counters, status fields and repeated recovery advice.
- Preserve the complete checkpoint and UI audit trail. Failed operations,
  partial failures caught by JavaScript, omitted outcomes, truncated output,
  state warnings and unfamiliar response envelopes retain their full metadata.
  Tools still execute normally; approval policy and verification are unchanged.
- Guide the agent to batch independent reads and searches, select relevant
  output, use scoped JavaScript locals, use the current scratchpad host path,
  and inspect native return values instead of assuming an `execute.stdout` field.

## Validation and limits

- 188 related Code Mode, context, snapshot and compaction tests passed locally.
  Add token-efficiency regression coverage to both platform CI suites.
- An offline replay of one exported session compacted 239 results and removed
  53,668 characters. The tool-result estimate fell from 185,629 to 168,230 tokens
  (9.4%) using the `cl100k_base` reference tokenizer. This measures tool-result
  text only, not total model input, provider billing or live task completion.
- Fewer retries from the revised guidance have not been measured in a live run.
  No new runtime dependency is required.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.23
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Retain any optional extras your installation
needs; for example:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.23"
```
