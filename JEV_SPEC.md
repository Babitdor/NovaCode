# Jev authentication and System One endpoint support

Acceptance criteria (autonomous implementation; spec approval not obtained):

- `/auth` lists Jev and custom System One credentials; Jev uses `TYPESAFE_API_KEY`, custom servers use `SYSTEM_ONE_API_KEY`.
- Saving a Jev key stores it through the existing credential store. Authentication alone does not change the selected endpoint, model or enable toggle. Failed saves and cancellation do not change settings.
- Existing endpoint/model configuration and local Tev1 defaults remain compatible. Arbitrary names such as Kev1 and future System One models are passed unchanged.
- Client construction reads stored credentials before environment credentials. TypeSafe keys are used only for the exact official endpoint; other endpoints receive only the separate custom credential (or the existing local placeholder).
- Missing Jev credentials cause no HTTP request. HTTP failures cannot echo response bodies containing secrets. Invalid probabilities cannot clear a tool result.
- Summary generation, offloading and default-disabled behavior are preserved.
- Discovered during implementation: verdict caches must be separated by endpoint and model so a switch cannot reuse another model's decisions.

Failure model: wrong-host key disclosure (endpoint separation tests), credential saves changing decision settings (UI tests), missing key causing unauthenticated traffic (transport test), invalid response causing a clear (parser tests), regressions in existing auth/offloading (existing suites).

Setup: use existing Python, pytest, Ruff and Textual; no new dependencies or paid live requests. Add contract/UI tests and an evidence report. Run applicable repository checks; disclose unavailable tools and unrun checks. User authorized committing and pushing the completed work to main.

`/model` owns a Decisions tab and an enable/disable toggle. `/auth` stores only
credentials, while Decisions stages the toggle/model/endpoint until Save.
Save rebuilds the live agent with its existing chat model. Presets include Jev,
Tev1 and custom models; arbitrary model names remain supported. Cancel changes
nothing, missing Jev keys block enabling Jev, and invalid endpoints/config-write
failures keep the dialog open. Nested `/auth` preserves the draft. Existing
Models and Voice tabs retain their behavior.

User steering: audit CTX and SESSION accuracy. Latest-call input tokens must
drive CTX, while SESSION sums distinct main-model calls (repeated streamed
usage snapshots count once). Normalized cache tokens must not be counted twice.
Compaction resets context after recording session usage, including subsequent
library compactions, and estimates use the effective summary/tail rather than
archived checkpoint messages. Model changes discard the previous window and
recompute context. The SESSION percentage labels its configured token budget.
