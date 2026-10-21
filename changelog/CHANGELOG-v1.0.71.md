# NovaCode v1.0.71

## Improved

- Web Council advisors now stream independent analyses in parallel, followed by peer ballots and a dedicated chairman synthesis stage.
- The configured judge seat acts as the web chairman, using its provider/model override or inheriting the session model. Matching member model overrides are reused.
- The chairman receives public analyses, vote reasons, tally, clarifications, and failed-member information. Its instructions require reconciling disagreement, identifying unsupported claims, retaining minority concerns, and proposing next steps.
- Web debates show streaming synthesis before enabling final approval. Ballot totals remain advisory; a failed chairman produces an explicit error instead of presenting a vote as consensus.
- Added bounded parallel stream buffering, worker cleanup on cancellation, and off-thread search execution. Follow-up history includes the consolidated answer.
- Planning Council behavior and model routing are unchanged.

## Validation

- Synthetic-provider checks cover simultaneous advisor starts, cancellation cleanup, chairman inputs and streaming, failure handling, model reuse, HTTP SSE synthesis, and final browser approval.
- Existing Council UI, settings, and planning regression checks remain covered. No new runtime dependencies.
