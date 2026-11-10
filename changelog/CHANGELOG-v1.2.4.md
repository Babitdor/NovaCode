# NovaCode 1.2.4

## Windows `/update` reliability

- Hand the update off to a fresh Python interpreter that waits for the running
  Nova process to exit before replacing the environment, instead of letting `uv`
  mutate a process that still has the old `.pyd` files mapped.
- Stop auto-restarting after a failed hand-off, so a half-updated environment is
  never silently restarted.
- Launch updater and relaunch commands with the isolated interpreter flag so a
  source checkout cannot shadow installed code.

## Stacked guardrails (opt-in)

- Add LangChain-style PII middleware (`PIIMiddleware`) with `redact`/`mask`/`hash`/
  `block` strategies across input, model output and tool results; enabled with
  `/guardrails on`, off by default.
- Default PII rules redact email addresses and mask credit card numbers; the `url`
  type is not enabled because redaction mangles expected links.
- Add deterministic blocked-term filtering through before/after-agent content
  filters with a preflight refusal and final-answer replacement.
- Add optional model-based output review; unavailable or malformed verdicts fail
  open and surface a skip notice, explicit denials withhold the response.

Human-in-the-loop approval stays the sole approval layer; PII and content
filters are additional opt-in layers and inherit to local subagents.

## Verification

- Focused guardrail tests, update-flow restart tests and regression suites for the
  agent loop, agent invocation and approval paths were run locally on Windows.
