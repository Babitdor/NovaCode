# NovaCode v1.0.34

## Improved

- The coding workflow checks what numeric values include and verifies ambiguous classifications against the full category definition.
- The optional post-edit reminder recognizes failed checks and checks with no tool result.
- Terminal Bench analysis records post-edit check attempts and compares matched jobs; the evaluation launcher supports SWE-bench Verified.

## Validation and limits

- A matched Terminal Bench financial-document task improved from reward 0 to 1, with seven verifier tests passing. This single task does not establish a general accuracy gain and used more tokens.
- Focused agent and prompt regressions passed (87 tests). The full suite was kept separate; five pre-existing scoped mypy errors remain.
- A separate HTML-filter benchmark has a flawed clean-input comparison; its score was not used as evidence for a harness change.
