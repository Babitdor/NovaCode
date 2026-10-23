# Caveman mode and token-efficiency research

## Available now

The **Caveman: off/on** button below the prompt composer activates the selected features.
Click it, focus it and press Enter, or use `/caveman`, `/caveman on`,
`/caveman off`, or `/caveman status`. The adjacent **Options** button and
`/caveman settings` open six independent switches. Each is selected by default;
the master mode remains opt-in. Save applies options; Cancel discards them.

The preference is off by default and saved as `caveman_enabled` in the user's
`Nova.config.json`. It applies to subsequent model calls through Nova's core
agent builder, including existing graphs; an answer already streaming finishes
with the instructions it started with. Per-feature preferences are saved under
`caveman_features`. This is a global preference, shared by
Nova sessions using the same configuration. Another running UI reads it at its
next startup, while its agent reads the current value before each model call.

The Say less instruction asks for concise prose, exact technical details and full clarity
when needed. User requests for detail take precedence. It does not rewrite user
messages, code or standing instructions. Read less, Browse and Waste fixes change
only the model's copy of tool results. Auto can override the model for a run;
it leaves the saved session model and global effort setting alone.
Async calls read preferences on a worker thread, keeping filesystem access off
the TUI event loop. No new package, proxy, account or background request is added.
Actual model compliance and token savings depend on the model and task.

## Research: the six features

Reviewed 2026-10-23 against the upstream Caveman project. Nova implements native
versions of these six capabilities; it does not install the upstream proxy.

| Feature | Nova implementation approach | Required checks |
| --- | --- | --- |
| Say less | Request-time system instruction controlled by the composer preference. Implemented in `agents/caveman.py`. | Live enable/disable, saved preference, unchanged input/history, system block metadata, mouse/keyboard accessibility. |
| Read less | Minifies JSON without changing numeric lexemes or duplicate keys; reduces repeated terminal lines and passing test rows; previews large CSV tables. Stores exact originals in thread checkpoints. | Failure lines, byte-for-byte text recovery, malformed/unknown/oversized input fallback, unchanged message IDs/status and image blocks. |
| Auto | Classifies each text request against configured `/router` routes, including per-route effort. Effort is passed through Nova's provider adapters and included in the model cache key. | Failed classification falls back to the session model; image requests keep the session's vision path; an explicitly enabled `/router` takes precedence. |
| Blocks | Indexes prior execution payloads by content hash and checkpoints their exact arguments. The model can list, recover and rerun them using the original execution tool. | Read-only retrieval; no new executable paths or permission bypass; retained across removal of old messages within the same checkpoint thread. |
| Waste fixes | Replaces duplicate large tool results with recovery references. `/caveman audit` reports per-tool estimated token use, duplicates and provider-reported cache reads. | No transcript upload or automatic file edits; estimates separated from usage observations; no speculative cache-warming requests. |
| Browse | Reduces HTML tool output to text, links and control descriptions; removes excessive blank lines from fetched Markdown, including content strings inside JSON result envelopes. | Exact recovery of the original tool result, preserved code fences, smaller output gate, existing fetching and permission policies remain in place. |

## Using the other features

Configure routes and criteria in `/router`, with an optional **Reasoning effort**
for each route. With Auto selected, Caveman chooses from those routes once per
text run. No routes means no routing requests or model change. If `/router` is
explicitly enabled, that independent setting takes precedence over Caveman Auto.
Image-bearing requests conservatively retain the original session model so its
existing direct-vision/VISION-tool behavior remains consistent. Unsupported
provider effort controls retain the provider adapter's existing behavior.

The model has one read-only utility: `caveman(action="recover"|"blocks"|"audit")`.
Recovery references identify exact original text, not filesystem paths. Recovery
supports `offset` and `limit` (up to 12,000 characters per call) and remains
available after the master switch is turned off. Source and block state belong
to the invoking checkpoint thread; references never grant access to another
thread. Changing/clearing the session starts a separate thread. Original data
does not enter the model request unless explicitly recovered.

Recovery retains up to 8,000,000 characters per thread. Inputs above 2,000,000
characters are not structurally compressed. At capacity, new results pass through;
Nova never emits a lossy view without its saved source. Compression must reduce
character count by at least 15%, including the recovery marker. These are size
gates, not a tokenizer-based savings guarantee. Scripts retain at most 100 blocks,
each up to 50,000 characters. These limits bound the extra checkpoint data.
Recovered scripts still run with current file contents, dependencies and approval
rules; previous results are never reused as fresh execution evidence.

`/caveman blocks` lists saved script references for the active thread. Ask Nova to
reuse one; it recovers the exact arguments and submits them through the original
tool. `/caveman audit` prints a local report. Waste fixes automatically removes
redundant model-view copies of large tool results. Active prompt-cache warming
is intentionally not implemented: this release issues no background paid calls.
Cache-read statistics describe observations, rather than claiming that a cache
was kept warm or that provider charges were reduced.

Native request/tool middleware fits Nova better than installing a second generic
provider proxy: it keeps provider adapters, routing, approvals, tracing and tool
protocol handling in their existing paths. This is an architectural assessment
based on the local code, not a measured performance result.

## What the upstream evidence establishes

The [Caveman overview](https://github.com/JuliusBrussee/caveman#six-rocks-one-install)
describes six independently switchable parts. Its advertised savings come from
specific workloads, rather than a universal reduction for every model or session.

The [compression engine](https://github.com/JuliusBrussee/caveman/blob/main/docs/technical/engine.md)
detects data formats, checks that transformations are smaller, and supports exact
source recovery. Nova's Read less feature uses the same recoverable-view principle;
plain truncation would lose important debugging evidence.

The [Auto benchmark](https://github.com/JuliusBrussee/caveman/blob/main/docs/AUTO-BENCHMARK.md)
is an interim 85-pair evaluation. Its Claude-only pool solved 43.7% against a
44.3% baseline at 0.46 times the cost per attempt. It does not establish identical
answers, or equivalent results across Nova's providers.

[Blocks](https://github.com/caveman-ai/blocks) provides reusable scripts within a
repository. Nova should reuse this pattern while keeping execution inside its
existing path and approval boundaries.

The [local tools documentation](https://github.com/JuliusBrussee/caveman/blob/main/docs/technical/local-tools.md)
distinguishes compact browser snapshots, recovery and actions. A small semantic
snapshot can replace verbose markup, but browser actions still require the host's
normal permissions.

The [local analysis documentation](https://github.com/JuliusBrussee/caveman/blob/main/docs/technical/learn.md)
labels inferred token costs separately from verified usage. The
[cache planner](https://github.com/JuliusBrussee/caveman/blob/main/docs/technical/cache-planner.md)
also notes that cache hints alone do not establish cache hits. Nova should measure
provider usage before claiming savings or scheduling cache refresh requests.

## Verification

Run the offline behavioral and composer checks with:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_caveman.py tests/test_caveman_features.py tests/test_tui_help_coverage.py -q
```

These checks verify all six switches, request preservation, source recovery after
history removal, cross-thread isolation, script retrieval, routing effort and UI interaction.
They do not call a paid model or establish a token-reduction percentage. Benchmark
the same tasks with the same model, recording input/output/cache tokens and task
correctness before publishing savings.

### Live checks, 2026-10-24

Tested with the saved `commandcode / Qwen/Qwen3.7-Flash` configuration, real
provider inference, the packaged core prompt, Caveman middleware and
prompt-injection middleware. Feature comparisons used process-local switches;
saved settings and credentials were not changed. Inputs were synthetic tool
results, not a full interactive terminal session or live browser fetch.

- Say less: one paired mutable-default explanation used 695 output tokens with
  Say less off and 389 on (provider totals include reasoning). This single pair
  does not establish a general savings rate or equivalent code quality.
- Read less: 11,453 log characters became 265; the answer retained the exact
  assertion error and exit code. A 6,390-character CSV became 297 characters;
  the model called recovery twice and found the requested omitted row.
- Browse: 12,087 HTML characters became 201; the exact release URL path survived.
- Blocks: the model listed a stored script and recovered its exact command
  through the tool. No script was executed or granted approval by this test.
- Waste: the model called the audit tool and reported no paid idle cache warming.
  Duplicate synthetic results shrank from 22,906 to 11,593 characters while
  preserving the assertion error in the answer.
- Auto: the saved classifier selected the `general` route without fallback.
  A separate run through Caveman's actual routing middleware used that route's
  `commandcode / GPT-5.4` model and correctly answered the arithmetic probe.

Character reductions describe tool views only, not total request tokens or cost.
The raw local results are in `reports/caveman-live-2026-10-24.json` (ignored by Git).
