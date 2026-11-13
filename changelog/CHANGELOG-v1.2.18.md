# NovaCode 1.2.18

## Fixes

- Plan mode always requests explicit user approval before execution, including
  when auto-approve is enabled. The shared agent loop and command handlers use
  the existing approval flow; rejecting or refining a plan keeps planning active.
- Preserve OpenRouter cache-write counts through both streaming and non-streaming
  OpenAI-compatible SDK responses. Preserve gateway cache-read aliases as well.
- Include Anthropic's separate 5-minute and 1-hour cache-write counters, and
  recognize raw Bedrock, Gemini, DeepSeek, and Ollama usage formats.
- Show missing cache counts as unavailable instead of zero. Mark partial
  reporting and preserve these distinctions across session serialization.

## Validation

Offline regression coverage includes provider formats, streaming SDK conversion,
reported zeros versus absent counts, persistence, and approval with auto-approve
enabled. A two-request live probe using the configured OpenRouter
`recursive/flint-v1.0:free` model returned 0 cached tokens initially and 2,304 of
2,394 input tokens on the repeat; Nova's parsed counts matched both responses.
Both responses explicitly reported zero cache-write tokens. Other providers were
checked offline, not against live services. No new dependency is required.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.18
uv tool upgrade novacode-cli
```

Optional extras:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.18"
```
