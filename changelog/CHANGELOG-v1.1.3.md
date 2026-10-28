# NovaCode 1.1.3

## Ponytail compression mode

- Add an exclusive Off / Caveman / Ponytail composer selector, preserving drafts
  and focus without adding toggle messages to the transcript.
- Add Ponytail lite, full and ultra levels, plus review, audit, debt, gain and
  help commands. Settings persist across restarts and migrate existing Caveman
  preferences. One-shot helpers leave the selected mode unchanged.
- Preserve the pinned upstream Ponytail skill bodies and level filtering, with
  golden checks against its JavaScript instruction builder. Bundle the MIT license
  and source checksums without requiring Node at runtime.
- Apply Ponytail to main and delegated model requests without rewriting stored
  conversations or tool results. Existing permissions remain authoritative.
- Add an opt-in real-provider validation script for all ten compression options.
  The recorded DeepSeek run passed 19 of 20 checks; one ultra response omitted an
  exact line reference. Ponytail used more tokens on these small tasks, so token
  savings and identical model responses are not guaranteed.

## System One approval fixes

- Keep approval gates active when auto-approval is enabled, including custom tools,
  delegated actions and Code Mode. Preserve native question and plan interactions.
- Treat uncertain, malformed, low-confidence or unavailable reviews as requests for
  owner approval. A surfaced request cannot be silently approved or reviewed again
  by the TUI, headless runner or pipe adapter.
- Bind review context to the owner's actual request, reuse configured provider
  authentication and close owned review transports. Preserve hard policy denials
  and fail closed when policy or configuration loading fails.

## Jev tool pruning

- Score only eligible uncached results while retaining full narrative context
  within the scorer's token budget. Pair tool calls and results chronologically.
- Publish scoring passes only after every batch validates; reject nonfinite and
  out-of-range scores. Retain bounded in-memory cache hits across runs and version
  persistent cache keys so earlier entries are not reused under changed semantics.
- Fit scoring context with incremental token accounting. A synthetic 80-message
  benchmark reduced estimator calls from 15,614 to 394 while preserving the fitted
  state and token count.
- Extend offline CI coverage for approval decisions, compression modes and pruning.
