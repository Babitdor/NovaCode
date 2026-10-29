# Core runtime fixes: evidence

Implemented the four review findings and session isolation for events and
remembered approvals. No new dependencies, commits, releases or pushes.
Existing FEATURES.md and other pre-existing working-tree files were preserved.

Baseline: main, 96d6ef487370166427a290418c80e9df005599dc.
The implementation is the uncommitted source/test diff plus
scripts/verify_runtime_fixes.py and tests/test_runtime_boundary_fixes.py.
Contract: core-runtime-fixes-spec.md.
Final source/test manifest SHA256:
96607d38345e5856c6429124c016b40d01d9dff6c042d9ce8c701f1769757ec5.
The manifest is the sorted unique paths from git diff --name-only -- novacode_cli
tests, plus the two new Python files named above. Hash each UTF-8 path followed
by a newline and its raw file bytes, in that order. Documentation is excluded.
Spec approval: not obtained (autonomous run). Independent agent verification:
not performed. These checks provide bounded regression evidence, not a complete
security audit or proof that every runtime behavior is correct.

## Reproduce

Use the existing project virtual environment:

```text
.venv/Scripts/python.exe -m scripts.verify_runtime_fixes --static --mutations --coverage
```

Result on the final implementation: **316 passed, 1 skipped**, exit 0.
The skip is the existing platform-dependent symlink test. Four existing dependency
warnings were reported. Tests use isolated configuration and no external providers.

## Behavior evidence

| Contract | Proof |
| --- | --- |
| Local execution cannot skip authorization | Sync/async composed Plan Mode, hard-deny and Cowork tests; real LangChain graph; existing shell/background parity tests |
| Planning blocks native mutations | Parameterized installed-tool tests, HTTP method tests, existing plan/MCP/path/checkpoint suites |
| Git inspection allows match complete commands | Explicit alias/remote/output/help cases, permitted inspection cases, Hypothesis-generated command suffixes |
| Kernel exchanges serialize and cancel safely | Real subprocess tests for parallel responses, active/queued cancellation, active close, timeout, malformed responses, EOF recovery and event-loop ownership |
| Session state stays with its owner | Separate-project namespace/event/approval tests, worker-thread event production, explicit approval callback routing and coordinated session cleanup |
| Existing runtime contracts remain intact | Focused headless, TUI cancellation, recovery, backend, checkpoint and deepagents compatibility suites |

The original regressions were observed failing before their fixes. Additional
tests exposed cross-loop lock reuse, close waiting for a running request, and
an unread response after invalid JSON; these were fixed without lowering gates.
The shell parity test now requires traversal of the downstream handler, matching
the corrected execution contract.

## Static checks and coverage

- New regression module and verifier: ruff clean.
- Changed production modules: no increase in ruff diagnostic counts against HEAD.
  This compares counts by rule/message; it does not claim legacy files are clean.
- Formatted ownership modules and new/expanded kernel regression tests: format check passed.
- Strict targeted mypy: events.py, session_allow.py, python_kernel_tool.py passed
  with follow-imports=silent. The entire repository was not type-checked.
- Branch-aware coverage across events, remembered approvals and kernel ownership:
  **96%**, enforcing an unchanged 85% minimum. This is module coverage, not full
  changed-line coverage across the large existing TUI/bootstrap modules.
- git diff --check passed; final implementation diff reviewed.

Five isolated in-memory mutations were rejected by their intended regression
tests: Git prefix matching, omitted plan execution restrictions, direct execute
bypass, shared event queue, and unserialized kernel exchange. The verifier
requires pytest exit 1 and the expected FAILED test identifier for every mutant;
an import/collection failure or surviving mutant fails the verifier.

## Full-suite limitation

The full-suite attempt encountered failures and later a pytest timeout in a local
HTTP request stack. A subsequent fail-fast run isolated:

```text
tests/test_async_general_purpose.py::test_async_general_purpose_graph_keeps_workspace_and_approval_controls
AssertionError: missing NovaModelRetryMiddleware
```

That run stopped with 1 failed, 175 passed and 1 skipped. The same test fails in
an untouched HEAD archive, using the same environment (1 failed). Reproduce the
baseline failure with:

```text
.venv/Scripts/python.exe -m scripts.verify_runtime_fixes --baseline
```

This intentionally exits 1. The unrelated async-agent retry assertion was left
unchanged. Full-suite completion remains unverified. Live provider calls, Docker
and native OS confinement were not exercised; their implementations were not changed.
No randomized suite-order run or independent security review was performed.

Tool versions from uv.lock: Python 3.11 environment, deepagents 0.7.10,
pytest 9.1.0, hypothesis 6.169.0, coverage 7.16.2, mypy 2.1.0, ruff 0.15.17.
