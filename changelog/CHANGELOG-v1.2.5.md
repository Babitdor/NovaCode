# NovaCode 1.2.5

## More precise automatic memory recall

- Require at least two shared words, weighted by how rare each word is across
  the memory corpus, before a lesson is supplied automatically. Words that
  appear in hundreds of unrelated lessons no longer count as evidence.
- Never treat markdown rules (`---`), blank lines or separators as lessons.
- Compare the request's meaning only for failures, using the error on screen.
  Ordinary requests and successful tool output no longer pull in weakly
  similar `0.45–0.59` neighbours.
- Cap automatic recall at three distinct topics and show only the single
  built-in symptom that matches the current error.
- Skip a lesson that is over the per-turn character budget instead of stopping
  retrieval before smaller, still-relevant lessons are considered.
- Keep the task behind a short follow-up ("proceed", "fix it") in the retrieval
  query, and ignore pinned-skill or summarization messages when choosing it.
- Honour an explicit request not to use a named memory for the turn.

## Remote approvals

- Stop sending a redundant "Approval response recorded" reply after a remote
  approval is accepted; the approval result is already delivered.

## Verification

- Cover the precision rules with focused tests: separators, corpus-wide words,
  request-only semantic recall, the three-topic cap, budget skipping and
  single-symptom built-in selection.
- Re-ran the memory retrieval, built-in lesson, learning recall, memory notice
  and prompt-cache ordering suites locally on Windows.
