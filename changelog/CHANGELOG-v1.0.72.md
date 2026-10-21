# NovaCode v1.0.72

## Improved

- Council web workspace now follows the supplied workspace reference with session presets, Synthesized consensus and Full debate views, advisor/model cards, and a live three-stage pipeline.
- Code Audit, Architecture, and Creative Debate presets change web-only specialist perspectives while retaining configured provider/model overrides. Saved Council settings and planning behavior remain unchanged.
- Advisor states, completed responses, valid ballots, and elapsed time reflect actual SSE events. Hardware statistics and confidence percentages are not simulated.
- Full debate displays specialist analyses side by side on desktop; the synthesis view keeps the chairman's streamed and final answer prominent. Clarifications, failures, stop controls, and user approval remain available.
- Clear view removes displayed messages while retaining discussion context. New discussion resets context. Added keyboard-accessible view tabs and responsive controls.

## Validation

- Added preset immutability/model-preservation tests, real HTTP/SSE preset routing checks, and browser checks for view switching, pipeline stages, metrics, clear behavior, keyboard navigation, and mobile layout.
- Reviewed desktop and phone screenshots with synthetic streams. No new runtime dependencies.
