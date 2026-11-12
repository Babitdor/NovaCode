# NovaCode 1.2.16

## Model-controlled context compression

- Enable native compression adapted from billion-context/acp-kernel, with no
  Node.js proxy or new runtime dependencies. The active model chooses completed
  ranges and writes summaries through its normal tool loop.
- Add `compress`, `decompress`, `search_context` and `acp_status`. Combine blocks
  into up to three summary tiers and recover exact archived message JSON in
  bounded pages. Search includes visible history, summaries and originals.
- Persist blocks and archives with the owning graph thread, including across
  SQLite checkpoint reopen and session resume. Preserve original user messages
  and complete tool exchanges. Reject partial overlaps and parallel compression.
- Retain existing automatic compaction, `/compact` and provider-overflow
  recovery. Keep folded summaries together with preserved user messages when
  choosing a fallback cutoff. Reused provider call IDs cannot overwrite archives.
- Keep context tools directly available in Code Mode. Set `context_hierarchical`
  to `false` in Nova configuration to disable new model-authored folds.

Documentation: [Context compression](https://github.com/Babitdor/NovaCode/blob/main/docs/context-compression.md).
Nova uses the [billion-context](https://github.com/ranxianglei/billion-context)
and [acp-kernel](https://github.com/ranxianglei/acp-kernel) design; the upstream
copyright, license and attribution terms ship with the package.

## Verification and limits

- 135 compaction/UI regression checks passed, followed by 80 boundary checks
  and 16 final native integration checks. These groups overlap.
- Tests cover synchronous and asynchronous graphs, Deep Agents integration,
  SQLite reopen, exact recovery, nested summaries, thread isolation, fallback
  cutoffs and reused tool-call IDs. Focused Ruff and Mypy checks passed for the
  new module. The verification wheel contained the implementation and license.
- Tests use scripted models. Live-provider summary quality, cost reductions
  and billion-token scaling have not been established for Nova. Checkpoint
  archive storage continues to grow with history. Optional upstream proxy,
  CCR/absorb/crush and semantic-search features are not included.

## Update

Use `/update` in Nova to update and resume in the same terminal. Finish other
Nova sessions using the installation first. When upgrading from a version older
than 1.2.15, exit those other Nova processes before starting the update.

```text
uv tool install --python 3.11 --force "novacode-cli==1.2.16"
uv tool install --python 3.11 --force "novacode-cli[browser,voice,agents-server,server,bedrock,computer]==1.2.16"
```

For shell installation commands, exit all Nova processes using that installation.
