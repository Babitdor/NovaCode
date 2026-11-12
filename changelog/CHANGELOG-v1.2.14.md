# NovaCode 1.2.14

## Fixes

- Keep progress, tool activity and enabled home animations moving when another
  window has focus. Animation preferences and low-resource mode still apply (#15).
- Command Center and tab badges share live activity and attention state. Hidden
  child tabs no longer use stale render state. Nova terminals sharing a session
  directory publish status once a second, with ten-second crash expiry. Sessions
  open elsewhere stay in their owning terminal (#16).
- Coalesce accidental parallel direct `write_todos` calls before execution,
  preserving distinct tasks and using the last status for identical task text.
  Invalid batches still report validation errors. Planning remains a direct tool;
  use one full snapshot when removing tasks, and never call it inside `eval` (#17).

## Learning, memory recall and Wiki

- Use the active model to interpret recall intent before retrieval. It generates
  focused search phrases and can select relevant scoped topics directly, even
  without matching keywords. The existing LLM reranker then orders retrieved
  evidence. Planning and ranking each have a ten-second timeout and local fallback.
  Each explicit recall can make two model requests; no new provider is configured.
  The planner sees up to 128 topic summaries, while its queries search the entire
  authorized corpus. It cannot invent source IDs or broaden project visibility.
- Reuse identical pending learning proposals, including concurrent submissions,
  without duplicating review entries. Changed evidence, source versions, scope or
  provenance produces a separate proposal. Approval and rollback rules still apply.
- Recall short technical terms such as SQL, API, CLI, JWT and UI. Exclude generated
  memory-Wiki navigation from search evidence so related-page labels do not create
  false matches or contaminate excerpts.
- `/ask` searches Wiki titles, summaries and page bodies using question terms.
  Relevant passages can come from deep in a page, include source paths, and fit a
  bounded context block. Reads run off the UI thread; stale or unsafe index entries
  do not prevent retrieval from valid pages.

## Verification

- Local checks passed: 114 focused UI/memory regressions, 49 LLM recall and
  memory-tool checks, 67 final learning/presence checks, and 43 updater/version
  checks. Focused Ruff and Mypy checks also passed.
- Regression coverage for focus-independent animation, cross-terminal status
  transitions and expiry, real Code Mode graphs, concurrent learning proposals,
  technical-term recall and Wiki evidence retrieval.
- LLM recall tests cover paraphrases with no lexical match, direct topic selection,
  project isolation, malformed output, timeouts and cancellation using fake models.
  Live provider behavior has not been exercised in this release verification.
- No new runtime dependencies. Same-terminal `/update` behavior is preserved.
- Three existing banner-width assertions also fail against v1.2.13; this release
  does not change banner geometry. Full repository test-suite coverage is not claimed.

## Update

Use `/update` inside Nova, or install this version explicitly:

```text
uv tool install --python 3.11 --force "novacode-cli==1.2.14"
uv tool install --python 3.11 --force "novacode-cli[browser,voice,agents-server,server,bedrock,computer]==1.2.14"
```
