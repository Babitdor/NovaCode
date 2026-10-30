# Plugin compatibility evidence

Date: 2026-10-28. Package version: 1.1.3. Spec approval: not obtained
(autonomous run). This is agent-authored evidence against the local contract,
not independent proof or a claim of full Claude Code host compatibility.

## Final verification

`uv run python scripts/verify_plugin_compatibility.py` exited 0 on Windows:

- 168 focused tests passed (plugin compatibility, legacy plugins, native hooks,
  skills runtime, subagent resilience and deepagents compatibility).
- 88 plugin tests passed in randomized order with seed 428.
- Package branch-aware coverage: 81.937%, with an enforced 80% minimum.
  This is package coverage, not 100% changed-line coverage; unexecuted branches
  remain, particularly in component schema variants and POSIX locking.
- Ruff lint/format passed for six plugin modules, the command handler and tests.
  Mypy passed for six plugin modules with imported modules followed silently.
  Diff-aware Ruff inspection found no violations on modified integration lines;
  existing large-file formatting/lint debt was not rewritten.
- Four reversible manual mutations were killed by assertion failures: path
  containment, activation flag, hook denial and registry-write rollback. The
  runner requires pytest exit 1 and FAILED output, then restores exact bytes.
  These selected faults do not imply exhaustive mutation coverage.
- Actual `nova plugin --help` printed help. A temporary local synthetic bundle
  passed actual shell validation. Shell result/error printing has a regression
  test observed red before its fix; the TUI service remains print-free.
- The filename/content boundary regression was observed red, then green with
  framed records including permissions, link targets and file-content hashes.
- `git diff --check` passed. No new dependencies or credentials were added.

Local outputs: `test-results/plugin-verification-final.log` and
`test-results/plugins/{coverage.json,negative-controls.json,source-state.json}`.

## Contract mapping

| Criterion | Evidence |
| --- | --- |
| 1: validate before publication, no installation execution, containment, concurrency | Compatibility tests cover invalid names/JSON/paths, external symlinks, unchanged old installs, no package scripts/hooks/MCP execution, thread and independent-process registry writes; generated traversal/name properties. |
| 2: marketplace identity, source/ref/subdirectory | Fake Git tests exercise ref/subdirectory retention and errors; marketplace schema and containment tests reject unsupported source types and unsafe relative paths. |
| 3: shared commands and scope precedence | Service tests cover management actions, user/project/local overrides, quoted Windows paths, no download from settings, scope-specific TUI actions, shell output and print-free TUI help. |
| 4: immutable updates and migration | Tests cover legacy registry discovery, scope uninstall, retained snapshots, failed fetch/timeout/validation rollback, settings restoration after registry failure and framed snapshot identities. |
| 5: component loading and namespaces | Tests cover YAML metadata, inline/default/custom paths, namespaced commands/skills/agents, metadata qualification before dedup, on-demand skills and root expansion. |
| 6: runtime overlays and owner settings | Tests cover enable/disable/uninstall, legacy exact MCP ownership, preserved owner edits and nonpersistent environment expansion. |
| 7: hooks preserve Nova authority | Harmless subprocess tests exercise JSON stdin, working directory, environment, matchers, context, time/output limits, sync/async denial and plan-mode suppression. A fake-model LangChain graph proves denial precedes effects. Allow cannot bypass normal policy; subagents do not emit owner lifecycle events. |
| 8: TUI and reload | Headless Textual tests cover tabs, narrow layout, actions, notifications, scope, stale discovery generations, session-local autocomplete and bundle-specific lifecycle context after enable/disable/update/reload. |
| 9: explicit compatibility limits | Unsupported capability diagnostics tested; no claim of Claude host emulation. |

## Additional checks and limits

The remaining offline CI integration batch passed 779 tests, with 4 skipped and
11 subtests passed (`test-results/plugin-ci-remaining-final.log`). An earlier
monolithic offline CI run timed out during asyncio executor teardown; a complete
single-run full-suite pass is **not established**, nor is the timeout proven
preexisting. Tab-mention tests passed separately (15). Earlier supplementary
skills/MCP/headless checks passed 104 tests; native/MCP boundary verification
passed its 75- and 195-test batches and expected negative controls. These earlier
checks preceded the final localized hash and shell-output fixes.

A built wheel contained all six plugin runtime modules before the final localized
fixes. Read-only validation of an unmodified upstream Ponytail 5.3.2 checkout
found six commands, six skills and two hook configurations; `userConfig` was
diagnosed unsupported. No upstream hook was executed and no global install was
made. This is loading evidence, not an end-to-end community-plugin runtime test.

Live community MCP servers/hooks and POSIX OS locks were not exercised locally;
Linux/Windows hosted CI remains the platform check after push. Git branches/tags
are supported; full commit-SHA fetching is not implemented. Updates are manual;
marketplace removal requires uninstalling its plugins first, and old snapshots
are retained without garbage collection. Mods, LSP, channels, workflows,
outputStyles, userConfig, dependencies, desktop bundles and account/org sync
are unsupported. Plugin execution retains normal Nova permissions and is not
a security sandbox. Docs and this evidence remain local per owner instruction.
