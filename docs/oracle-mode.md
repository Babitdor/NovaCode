# Oracle mode

Click **Oracle: off** beside **Code Mode** in the composer to enable independent
review rounds for subsequent requests. The toggle preserves your draft. Oracle
works with normal tool calling and Code Mode; it also checks that local teammates are no longer
active before allowing a passing result. Planning requests remain read-only and
do not enter the fix loop.

The composer strip shows the round, lowest reviewer score, target and current
state. Click it to expand reviewer rows. Each row shows its assignment and its
first and latest scores. Expand it for findings or choose **Open reviewer thread** to read that
reviewer's inputs and results across rounds. Review threads are read-only and
remain available for the current conversation while Nova is running.

The reviewer panel opens when a new round starts. It refreshes once per second
with each reviewer's status and elapsed time, a finished count and the timeout.
Paged reviewers also show how many evidence pages they have read.
Results appear as individual calls finish;
completed timers stop. Reviewer threads also update while a call is running.
Running means Oracle is waiting for the model response; it does not expose the
model's private reasoning. You can collapse the panel while reviews continue.

## Review and fix rounds

After the lead finishes, Oracle gives independent reviewers the request, lead
response, diff for the selected scope and captured test tool results. Reviewers
use the current session model with structured scores
from 0 to 10. Reviewers cannot edit files, approve tool calls, or see peer scores.
The lowest score controls the result: every reviewer must meet the target.

Each reviewer scores one assigned aspect. The default three cover correctness,
regressions, and security. Raising the reviewer count adds verification and
maintainability, in that order. Explicitly saved reviewer counts still apply.
Assignments stay with their reviewer slots
across rounds, and any reviewer can flag a critical defect outside their
assignment.

Oracle sends evidence inline up to 64,000 characters. Larger reviews
automatically use independent read-only reviewer agents. Each agent requests
pages of the captured request, response, file evidence and test
output. The agent carries review notes between calls. Oracle accepts a score
only after that reviewer has received every page.

When a score falls below the target, the existing lead agent receives the
findings as untrusted review data, fixes valid issues using its normal tools and
approvals, then enters a fresh review round. Existing goal and learning workflows
continue to use their existing streams.

Each review records a content fingerprint of the Git workspace, including
nonignored untracked files. Changes during review force another round. Oracle
checks freshness again before finishing, and checks displayed results in a
background worker when relevant filesystem changes arrive. Modified results
become **stale**. Old scores cannot approve new changes. Missing, invalid, timed-out or failed reviews
never count as passing scores.

## Settings and decisions

Choose **Settings** beside the Oracle toggle, or run `/oracle-mode settings`, to set:

- Target score: greater than 0 and at most 10; default **8**.
- Automatic round limit: **1–20**; default **3**.
- Reviewer count ranges from **1–5** and defaults to **3**.
- **Review scope** defaults to **This task**, which compares file contents and
  modes from the start of the turn through the latest review round. It includes
  edits to already-dirty files and edits committed during the turn. Choose
  **Workspace changes** to review the current workspace against the Git commit
  at the start of the turn, including existing edits and nonignored untracked
  files. Before the first commit, workspace scope starts from an empty tree.
  Older settings without a scope use **This task**.
- Whether new threads start enabled; default **off**. `/clear` uses this default.

Each saved conversation remembers its toggle. Resuming it does not restore old
scores as passing evidence; a new request gets fresh reviews.

Settings apply to subsequent runs. At a round limit or review error, Oracle
pauses for your decision:

- **Keep fixing** sends the findings to the lead and permits another round.
- **More rounds** extends the run by up to three review rounds without forcing
  an extra edit first. The absolute limit remains 20 rounds per run.
- **Lower target** applies the number entered beside that control. It can use
  existing scores only when every review is valid and the workspace is current.
- **Accept and finish** explicitly ends the run without claiming the target
  passed. The receipt says **accepted**.
- **Pause** ends automatic work without passing. A new request starts a new run.

Turn Oracle off with its composer toggle or `/oracle-mode off`. This stops further
review/fix rounds; it does not cancel the lead's already-running tools. Escape
or the normal stop controls cancel the current run, including pending reviews.

## Cost and boundaries

Inline reviews make one model call per reviewer with a two-minute timeout.
Paged reviews require model tool calling and make multiple calls with up to
ten minutes per reviewer. Reviewers run concurrently in both modes. Oracle
attributes reviewer calls to `oracle/reviewer-N` in usage tracking.
Independent contexts avoid manual agent-to-agent coordination, but speed and
token savings have not been benchmarked.

Oracle requires a readable local Git workspace and a model supporting
structured output. The paged path accepts up to 512,000
characters of evidence in 16,000-character pages. Evidence beyond that limit,
changed binaries, unreadable files, submodules, and incomplete page coverage
pause review. Sandbox files that are
not mirrored into the local workspace are outside this review path. Split large
tasks or explicitly accept the result when automated review cannot inspect it.
Noninteractive consumers cannot grant an Oracle owner decision; they pause at
decision points. Scores are advisory model judgments, not a replacement for
tests or your review.

**This task** skips existing binary edits whose contents stay unchanged.
Binary changes in the selected scope pause review and show the affected paths
in the panel. Task scope keeps a bounded baseline for dirty files and pauses if
it cannot reconstruct a changed file's original contents or compare a file
within its size and line limits. **Workspace changes** uses the same overall
evidence budget. **Retry review** retries without an extra lead edit.
**Accept and finish** records your acceptance separately from reviewer scores.

Snapshots retain binary-file information, including for untracked files. Deleting
a binary or replacing it with text during a task still requires manual review.
Snapshot reads handle UTF-8 characters spanning read buffers without classifying
valid text as binary.

Git diff output is spooled to temporary files and size-monitored while Git runs.
Oracle stops oversized output, terminates unfinished Git processes, and applies
the active evidence budget across diff batches and file bodies.
