# Knowledge cache and teammate Auto Approve acceptance

The supplied implementation plan is the task contract. This is an autonomous
implementation; separate executable-spec approval was not obtained.

Failure model: cached private or stale source could enter context; concurrent
producers could duplicate calls or republish after clear/off; cache corruption
could interrupt work; automatic desktop input could bypass pause, replay after
restart, or affect another agent. Tests must exercise those boundaries.

Named checks:
- Knowledge: cold/warm calls and usage, restart/exact/semantic retrieval,
  namespace/model/prompt isolation, complete hash invalidation, expiry and limits.
- Knowledge security: backend refusal, traversal, escaping links, credentials,
  private runtime paths, source changes during generation.
- Knowledge concurrency: leases, cancellation, stale producers, clear/off epochs,
  corruption, bounded storage and recoverable fallback.
- Integration: core tool scope, local-only embeddings, immediate scoped cache
  commands, TUI/headless compatibility, offline replay of repeated overviews.
- Teammates: default off, persistent isolated boolean setting, owner-only command,
  durable automatic decision and single execution, pending proposal unchanged,
  disable/pause/takeover/restart/uncertain-action controls.
- UI: polling sends no commands, authoritative rollback on save failure, pending
  review takes priority in status and preview.

Use installed Python/pytest/ruff/mypy and standard-library SQLite. No dependency,
release, push, destructive migration, or automatic model download is authorized.
Preserve unrelated edits. Focused regressions first; report any omitted checks.
