# NovaCode 1.2.6

## TUI reasoning display

- Show reasoning collapsed while streaming with a `thinking...` header.
- On completion, keep the trace collapsed and retitle it to `Thought - <seconds>s`
  (elapsed reasoning time), while preserving the full trace in the expandable body.
- Update reasoning scroll test expectations to match the collapsed finalized header.

## Verification

- `python -m py_compile novacode_cli/tui/app.py`
- `pytest tests/test_reasoning_scroll.py -q`
- `pytest tests/test_reasoning_visibility.py -q` (10 failed, 2 passed locally; failures assert core `ReasoningDelta` emission, not TUI header/collapse behavior)
