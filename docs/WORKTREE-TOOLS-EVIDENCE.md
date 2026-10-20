# Worktree tools evidence

Spec: [WORKTREE-TOOLS-SPEC.md](WORKTREE-TOOLS-SPEC.md).
Spec approval: not obtained (autonomous implementation). No dependencies added.

## Behavior mapping

`tests/test_worktree_tools.py` verifies entry/exit with real Git commits and dirty
files; separate sessions with identical names; invalid transitions and creation
failure; non-Git/unborn repositories; mixed batches and concurrent operations;
real filesystem and shell rebasing; remote-context rejection; always-bound tool
schemas; exception/cancellation release; and execution through a LangChain graph.
The graph test uses a scripted model, with no provider or network call.

## Checks

- Focused suite: **80 passed** using:
  `python -m pytest tests/test_worktree_tools.py tests/test_tool_search.py tests/test_session_gateway.py tests/test_worktree.py tests/test_cross_session_tui.py tests/test_cross_session_lifecycle.py -q`.
  Pytest-randomly is installed and active for these runs.
- Mutations: `python -m scripts.verify_worktree_tools` killed **3/3** mutants:
  missing entry switch, wrong exit root, and allowing a mixed transition batch.
  Each child process runs a fixed source mutation in memory; source files remain
  untouched. These probes validate these three defenses, not every invariant.
- Ruff check and format pass for the new implementation, tests and mutation script:
  `python -m ruff check novacode_cli/tools/worktree_tools.py tests/test_worktree_tools.py scripts/verify_worktree_tools.py`
  and the same paths with `python -m ruff format --check`.
- Limited local type check passed:
  `python -m mypy novacode_cli/tools/worktree_tools.py --follow-imports=skip --ignore-missing-imports --disable-error-code=misc --disable-error-code=untyped-decorator --cache-dir=.tmp/mypy-worktree-tools`.
  Framework imports are skipped; this does not verify framework API compatibility.
- `git diff --check` passed; diff reviewed. Existing changelog deletions and the
  preceding messaging change were preserved.

## Limits of verification

- Full-suite collection stops at the existing `deepagents` mismatch:
  `python -m pytest tests --collect-only -q --maxfail=1` collected 683 tests before
  `test_context_edit_trigger.py` failed because
  `deepagents.middleware.subagents.create_sub_agent` is absent. Main-agent build
  and declarative-subagent integration cannot run with this installed version.
- Existing files report pre-existing Ruff violations. New files pass independently.
- Coverage unavailable: direct coverage execution fails in installed pytest/plugin
  startup (PyO3 initialization, then closed stdout with LangSmith disabled).
  pytest-cov is not installed. No coverage threshold or percentage is claimed.
- No additional property-testing package was added: this change does not add a
  parser, arithmetic, or serialization algorithm. Explicit lifecycle/ownership
  invariants are exercised with temporary repositories.
- Worktree switching is local to the running agent graph, not persisted across
  resume or agent rebuild. External MCP workspaces, existing jobs and independently
  running agents retain their own roots. The original approval policy remains in
  effect. Exit retains all worktrees and branches; no cleanup/delete API is added.
- The full live Nova CLI cannot start under the installed deepagents mismatch;
  the real-execution checks use Git, a shell subprocess and a scripted agent graph.
