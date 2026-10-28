# NovaCode 1.1.4

## Prime project memory and continuation

- Save project-scoped task, check and evidence records in the existing durable
  store. Share findings across linked Git worktrees while retaining each run's
  checkout path, revision and declared input fingerprints.
- Recall up to five relevant check summaries within a 2,000-character allowance.
  Keep findings as drafts until observed failure is followed by passing checks
  against changed code. Mark changed or missing inputs stale or unavailable.
- Add `/prime tasks`, `/prime resume <id>`, `/prime checks`, and locally confirmed
  `/prime forget <id>`. Preparing a task starts no agent work; continuation uses
  the current workspace, model, permissions and limits, with separate child runs.

## Observed verification

- Define acceptance checks before normal tool execution and bind results to actual
  tool-call IDs from the owning run. Evaluate exit codes and literal output
  assertions mechanically; fabricated, missing, truncated or unsupported evidence
  cannot establish a pass.
- Preserve execution approvals, plan restrictions and cancellation handling.
  Redact and bound execution artifacts before persistence, and load detailed
  definitions and evidence only when inspected.
- Report unmet criteria and verification limits even when all six Prime phases
  have completed. Preserve inspection of older session reports.

## Prime interface

- Add a compact Prime strip and an expandable, independently scrollable drawer
  with task history, evidence and explicit continuation controls.
- Preserve composer drafts, keyboard access, theme styling and transcript scroll
  position. Keep progress, confirmations and storage warnings temporary.
- Add structured Prime event IDs for shared TUI, headless and remote consumers,
  plus offline contract and mutation checks in the Windows/Linux CI matrix.
