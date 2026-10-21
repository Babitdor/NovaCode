# NovaCode v1.0.68

## Improved

- Council decision briefs support up to eight options and explicit criteria.
- Advisors can ask clarification questions with suggested choices or a free-text answer before presenting independent recommendations, risks, and next steps.
- Decision ballots vote for actual options. Invalid ballots abstain, ties remain unresolved, and the user approves, revises, or rejects the recommendation.
- Added a stop control, SSE heartbeats, bounded buffering, disconnect cancellation, pending-question cleanup, and a single terminal event after history is recorded. Dropped browser streams close explicitly instead of retrying a new debate.

## Validation

- Added model-free clarification, option-voting, abstention, HTTP answer-token, heartbeat, cancellation, and end-to-end SSE clarification checks.
- Existing discussion mode and responsive Council/Cowork browser regressions remain covered.
