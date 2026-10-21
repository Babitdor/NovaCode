# NovaCode v1.0.74

## Fixed

- Team `spawn_agent` builders now pass Nova's shared store into both in-process and separate-process graphs. Learning-enabled teammates previously failed before their first model call with `'NoneType' object has no attribute 'aget'`.
- In-process teammate records now report the selected model identity instead of leaving the model field unset.
- Added a five-teammate regression using real LangGraph execution and learning middleware, and extended process-worker probes to exercise the same store dependency. CI now includes team runtime and usage-accounting checks.

## Validation

- Reproduced all five failures with the exact reported error before applying the fix; synthetic-model probes verify successful completion, nonzero model calls, and populated model reporting afterward.
- No new runtime dependencies or changes to the existing `task` delegation path.
