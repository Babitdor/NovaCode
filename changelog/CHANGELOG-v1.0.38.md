# NovaCode v1.0.38

## Improved

- The prompt composer starts at one text row and grows as lines or soft wraps are added, leaving more room for the conversation when idle.
- Explicitly enables soft wrapping and adds TUI coverage for multiline cursor navigation, in-place editing, growth, and shrinkage.

## Validation and limits

- TUI and clipboard tests passed (107 tests).
- Ruff still reports existing findings in the touched files; none were on the added lines.
