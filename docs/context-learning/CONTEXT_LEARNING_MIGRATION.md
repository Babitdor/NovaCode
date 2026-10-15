# Local migration and recovery

Keep the existing agent directory, `learning.sqlite3`, checkpoint database,
prompt history, and archives. This change does not delete old generated files.
Do not move activation artifact directories independently of their manifests:
the manifest names their immutable paths.

## Review and activate

Run `/learning review` in the project. The listing includes exact revision,
scope, target, source hash, evidence, intended content, and revision history.
Recognized generated prompt overrides and unpinned review lesson files are
staged as candidates. Ambiguous/manual prompt overrides remain untouched and
are identified in the listing. Legacy lesson imports retain their originals.

Use `/learning approve <id@revision>` or `/learning reject <id@revision>` with
the displayed revision. Approval remains pending until the next model turn.
The current turn keeps its captured configuration. Invalid targets, changed
source hashes, invalid syntax, or changed required template variables become
failed applications; review a new proposal. Approval never grants a tool
permission, and repository-local allow rules still cannot grant permission.

Generated artifacts live under `learning-artifacts/versions`; the atomic
`learning-artifacts/activation.json` selects active versions. Do not manually
edit these immutable artifacts. A failed publication leaves approval pending;
restart or start another turn to recover idempotently. SQLite is authoritative
for decisions even if the human-readable export cannot be written.

## Rollback

Run `/learning review`, then `/learning rollback <current-id@revision>`.
Rollback is queued and restores the preceding manifest entry at the next turn
boundary. The record remains inspectable as `rolled_back`. If an active
artifact has changed manually, application reports a conflict and preserves
the edited bytes. Save those edits separately and review their intended
replacement; do not overwrite them to force a rollback.

Turning learning off stops new automatic review preparation; it retains
records and already reviewed activation state.

## Recover conversation history

Use these commands through console, TUI, headless, or Telegram routing:

```text
/context recovery list
/context recovery inspect <archive-id>
/context recovery restore <archive-id>
```

The default store is `<Nova home>/recovery`. Complete manifests reference
SHA256-addressed message payloads and binary blobs. Inspection checks manifest
and blob integrity before restoration. Version 1 manifests are retained and
labelled as lacking manifest checksums; their payload checksums are still
verified. Older Markdown/JSONL archives remain on disk and appear in recovery
listings as incomplete legacy references. They lack a complete message/checkpoint
manifest and cannot be restored through `/context recovery restore`.

Restore creates a new checkpoint thread and selects it in the calling
session. It does not overwrite the original thread. Packed checkpoint archives
restore pending approval interrupts, which must still be answered normally.
An archive containing approvals without their checkpoint structure is refused
rather than continuing past the approval. Headless recovery honors the chosen
text, JSON, or streaming JSON output format and reports failure through its
exit code.

SQLite writes require valid renewable ownership. A competing process receives
an active-writer restriction; a superseded writer cannot publish. A writer
whose lease lapsed without being taken over (a stalled heartbeat) still holds
the newest fence, so its next write extends the lease. After a crash the lease
expires, and a fresh writer obtains a higher fencing token. Unknown persistence adapters are explicitly restricted from mutation
until equivalent coordination is implemented. The coordinated graph exposes
asynchronous mutation methods; synchronous mutation methods report this
restriction.

## Verification limits

Provider-prepared accounting supports the installed OpenAI and Anthropic
adapters; hidden wrapper transformations remain approximate/unavailable.
Token estimates are not billing measurements. Offline Google fixtures validate
local preset wiring and OAuth behavior, not a live account or upstream server
deployment. Corpus timing and recall observations do not establish better live
coding quality. See the audit and machine-readable results for actual test,
coverage, type-check, mutation, and independent-review outcomes.
