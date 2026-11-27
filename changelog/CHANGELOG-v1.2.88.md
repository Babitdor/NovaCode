# NovaCode 1.2.88

## Tool call repairs: what the model sees is now as important as the fix

Following the harness-engineering playbook (validate-then-repair at the tool
boundary), three additions to the existing repair layer:

- **Unrepairable invalid arguments get a compact, model-readable summary of
  the validator's issues** (at `path`: what was wrong, up to five lines)
  instead of the raw validation blob the model could not parse — logged
  `tool_input_invalid` so repair rates per tool are observable.
- **Relational invariants are taught the intent**: `read_file` with `limit`
  but no `offset` now reads from the start (and vice versa, `limit` = 2000),
  surfaced in the result with no error prefix so the model can self-correct —
  each field was independently valid; the pair was incomplete.
- **Object-string parse**: a JSON-encoded object string
  (`'{"case": false}'`) where an object is expected is parsed, the object
  sibling of the existing json-array-parse.
