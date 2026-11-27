# v1.2.88 — Tool call repairs round two

**Released 2026-11-27** · PyPI `novacode-cli==1.2.88` · tag `v1.2.88` (commit `ce9572f`)

Hardens how Nova repairs malformed tool calls, following the harness-engineering
tool-call-repair guide, and extends the same repair layer to every dispatch path
including Code Mode.

## Tool call repairs

- **Tri-state repair result.** When a repair is impossible, the model now
  receives a compact, model-readable summary of the schema issues instead of
  the raw validator blob, so the retry attempt is informed, not blind.
- **Object parse repair.** A JSON-encoded object string where an object is
  expected is decoded at the failing path (`{"a":1}` as a string).
- **Invalid-repair telemetry.** Repairs log `tool_input_repaired:<tool>` with
  the kinds applied; unrepairable failures log their compact issue summary.
- **Relational defaults.** `read_file` gets its missing `offset`/`limit`
  (`0`/`2000`) filled from the call context when the schema requires them.
- **Consistent return shape.** `evaluate_args` returns the same nested shape
  for valid-with-autolink and repaired calls; `_prepare` unpacking corrected.
- **Code Mode dispatches use the same layer.** The `eval`/`code_mode`/`mcp.call`
  trust boundary validated args with a raw gate and bounced failures with the
  validator blob. Dispatched calls now get validate-then-repair: the repair
  catalogue runs at the failing paths, the repaired args must still pass the
  original gate (acceptance semantics unchanged), the outcome records what was
  fixed (`step.repaired`), and unrepairable failures surface the compact
  summary instead of the blob.

## Tests

- New Code Mode regression tests: a repairable dispatch runs with repaired
  args and records the repair kinds; an unrepairable dispatch fails with the
  model-readable summary.
- arg-repair tests for the tri-state, object parse, relational defaults, and
  retry message.
