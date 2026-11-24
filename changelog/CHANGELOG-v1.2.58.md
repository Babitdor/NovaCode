# NovaCode 1.2.58

## Loop guard removed

- `LoopGuardMiddleware` — the identical-repeat tool-call breaker — is gone from the
  main agent, every subagent, and the plan agent. Its near-match heuristic treated
  legitimate retries (a read retried after a truncation error, a check re-run with an
  edited artifact) as stalls and blocked them with "LOOP STOPPED" walls; the
  escalation/hard-stop machinery then ended productive turns. The model's own
  error-driven recovery and the existing turn guards are the mechanism now.
  `TextRepetitionGuard` (streamed-prose repeat detection in the agent loop) remains —
  it is a different failure shape and not affected.

## Code Mode recovery hints are failure-aware

- The "Completed side effects remain in place. Resume unstarted work…" hint fired on
  every failed snippet — including plain bad-path and oversized-page read failures,
  where nothing could have changed. The hint is now side-effect-aware: read-only
  snippets get "The dispatched calls were reads — no state changed. Fix the reported
  error and retry."; the conservative boilerplate remains for writes/executes,
  timeouts, and elided outcomes.
- Oversized-page read errors now carry numbers: what the display pipeline kept vs the
  rendered size, and a computed `limit=N` retry suggestion — one retry instead of
  bouncing off the guard repeatedly.
- Not-found reads gain "did you mean" path suggestions: one bounded `rg --files` pass
  matches the requested basename (substring, then near-misses) and appends up to
  three existing virtual paths, so a bad path recovers in one call.

## MCP management screen no longer crashes on slow retries

- Pressing Retry (or Authenticate / Select tools) and leaving the screen mid-await
  crashed the worker with `NoMatches: #mcp-configured` — `is_mounted` reads True
  during teardown while the DOM is already gone. `_reload` is now tear-down-safe and
  every post-await DOM touch in the screen checks attachment first.

## Composer: pasted images are explained, not deferred around

- When vision is unavailable for the routed model, the failure placeholder now demands
  immediate resolution — ask the user to run `/vision on` (a multimodal model sees
  images directly) or describe the image in text — instead of letting the model defer
  image-dependent design work to "later" and never ask. Regression tests pin both
  routing modes: a multimodal main model receives the pasted image block; a text-only
  model gets it stripped with the `[image-N]` marker intact.

## Background-tasks bar rebuilt as a compact summary; down arrow opens the panel

- The collapsed indicator now matches the coding-CLI shape: a dim `Background`
  header over a count line — `1 active shell` (agents and waiters append to the
  same line) — with a `(↓ tasks)` hint. Per-command detail moved to the expanded
  panel instead of cramming truncated commands into one row.
- Pressing Down on the last composer line — after input history is exhausted —
  opens the Background Tasks panel: the natural "look down at what's running"
  gesture. Ctrl+B (empty prompt), a click on the bar, and `/tasks` open the same
  panel; the composer modes line shows `· ↓ tasks` while background work is
  active.
- All sync entry points into the panel (click, two Ctrl+B branches, `/tasks`,
  down arrow) route through a single `open_background_tasks` helper. (Correction:
  the "dangling coroutines" root-cause claim originally in this section was
  wrong — the pre-1.2.58 call sites worked via `@work`. v1.2.59 fixes the
  double-worker wrap this refactor introduced and adds a real-path regression
  test.)

## Shell tool description accuracy

- On Linux/macOS the `shell` tool runs `/bin/sh` — dash on Debian/Ubuntu — not bash.
  The description now says so and points bash-specific syntax (arrays, `[[ ]]`,
  `${var,,}`) at the `bash` tool, so the two no longer read as aliases.

## Packaging

- New tests: composer footer routing, paste vision routing, `file_outline`,
  the background-tasks bar and down-arrow gesture, and the first-party import
  contract test. Loop-guard test files removed with the feature.
