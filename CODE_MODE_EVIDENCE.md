# Native Code Mode evidence — 2026-10-24

Contract: [CODE_MODE_SPEC.md](CODE_MODE_SPEC.md). Spec approval was not obtained
separately: the user authorized planning and implementation together. No independent
agent verification was performed. This report describes focused test evidence,
not a proof of safety for every third-party tool.

## Reproduce

`uv run --no-sync python scripts/verify_mcp_code_mode.py`

Final run, after product edits: exit 0. It ran 74 randomized Code Mode/toggle tests
and 194 related regressions: **268 passed, 2 skipped**. Skips were Windows hosts
not permitting symlink creation in the Cowork/team workspace tests. Four negative
controls were killed by assertion failures: removing argument validation, the
call budget, approval evaluation, and control-tool exclusion. Controls patch the
loaded canonical module in isolated subprocesses; they never mutate source files
or rely on bytecode cache invalidation. Unexpected exit codes fail the verifier.

Combined statement/branch coverage: **92%**, over 267 statements and 82 branches
in the shared dispatcher/runtime. The existing 85% gate was retained; this is
not a 100% changed-line coverage claim. New/shared modules and native/toggle tests
passed ruff; four implementation/compatibility modules passed mypy. Existing
legacy-file lint counts did not increase (core 123, config 47, TUI 752, MCP
middleware 34, policy 9); these files are not globally lint-clean. Scoped format,
`git diff --check`, and a parse of the CI YAML passed.

## Contract mapping

| Contract | Executable evidence |
| --- | --- |
| MCP compatibility | `tests/test_mcp_code_mode.py`, original imports, real offline stdio server |
| Native discovery without MCP | `test_discovery_schema_calls_and_private_intermediates_without_mcp`, `test_core_registers_code_mode_without_mcp` |
| Session/deferred inventory | `test_catalogs_do_not_leak_between_graphs_or_deferred_tools` |
| Original schemas / hostile arguments | discovery schema test, `test_bad_arguments_rejected_before_execution` and validation negative control |
| Controls / state changes | parametrized control tests, declared function/subclass Command tests, unexpected Command error test |
| Actual native file operations | `test_real_filesystem_tools_read_write_and_edit` with temporary files and real Deep Agents tools |
| Approval / workspace / plan guards | native approval tests, downstream guard test, real workspace/plan tests, team/Cowork regression suites |
| No replay of prior actions | `test_failed_later_approval_does_not_replay_earlier_write`, existing MCP interrupt test |
| Limits / failure accounting | native budget, pagination bounds, bounded parallel cancellation; existing MCP sandbox/resource tests |
| Pagination properties | 12 generated examples in `test_native_catalog_page_matches_registered_inventory` |
| Toggle | `tests/test_code_mode_toggle.py`, `test_toggle_off_rejects_native_calls` |
| Preserved integration | deepagents compatibility, tool search, attribution, strict schema and policy regression suites |

The first native acceptance run had 22 failures demonstrating the missing
`tools` namespace, plus the pre-existing toggle rejection test passing. An added
subclass test subsequently exposed a real pre-execution filtering gap; it failed
before the fix and passed afterward. One intermediate verifier ran while imports
were still being edited and stopped on lint; final run was fresh and clean.

## Source and environment

Baseline commit: `41ffeaf981e8f09a4e057956361170bb8390bdb5`.
Task-source SHA-256: `a233dab234b139858e210c74f39065f0a9fc7508419a5fe8d5d6d159e5411bf4`.
To reproduce the hash, sort these relative paths, then hash each UTF-8 path,
a NUL byte, and its raw file bytes in sequence:

`novacode_cli/agents/{code_mode.py,code_runtime.py,core_agent.py}`,
`novacode_cli/config/nova_config.py`, `novacode_cli/tui/app.py`,
`novacode_cli/mcp/{middleware.py,code_mode.py,code_runtime.py}`,
`novacode_cli/security/policy.py`, `novacode_cli/prompts/mcp.jinja`,
`tests/{test_mcp_code_mode.py,test_native_code_mode.py,test_code_mode_toggle.py}`,
`scripts/verify_mcp_code_mode.py`, `.github/workflows/ci.yml`, `docs/mcp-code-mode.md`.
This hash identifies the Code Mode task state before subsequent voice work.

Windows, Python 3.11.13; langchain 1.3.18, langgraph 1.2.11, deepagents 0.7.10,
quickjs-rs 0.2.4, pytest 9.1.0, coverage 7.16.2, ruff 0.15.17, mypy 2.1.0,
hypothesis 6.169.0. Dependency manifests/lockfile were not changed.

## Limits

The entire unrelated repository suite and other operating systems were not run;
the focused verifier includes the responsible layers and existing compatibility
guards. No live provider, microphone, or remote MCP account was used. Real
execution used the production graph builder, temporary filesystem, local MCP
fixture and QuickJS runtime. Third-party tools with undeclared state mutations
can execute before their unexpected result is rejected. Interactive approvals,
media/graph-state results and delegation still require direct tool calls.
Completed external effects and synchronous work cannot be rolled back by timeout
or cancellation. Existing plan/workspace denials remain authoritative. No new
dependencies, commit, release, or push.
