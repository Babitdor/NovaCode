# Oracle mode

Click **Oracle: off** beside **Code Mode** in the composer to enable independent
review rounds for subsequent requests. The toggle preserves your draft. Oracle
works with normal tool calling and Code Mode; it also checks that local teammates are no longer
active before allowing a passing result. Planning requests remain read-only and
do not enter the fix loop.

The composer strip shows the round, lowest reviewer score, target and current
state. Click it to expand reviewer rows. Each row shows its first and latest
score; expand it for findings or choose **Open reviewer thread** to read that
reviewer's inputs and results across rounds. Review threads are read-only and
remain available for the current conversation while Nova is running.

The reviewer panel opens when a new round starts. It refreshes once per second
with each reviewer's status and elapsed time, a finished count and the
two-minute timeout per reviewer. Results appear as individual calls finish;
completed timers stop. Reviewer threads also update while a call is running.
Running means Oracle is waiting for the model response; it does not expose the
model's private reasoning. You can collapse the panel while reviews continue.

## Review and fix rounds

After the lead finishes, Oracle sends fresh, independent reviewer contexts the
request, lead response, Git diff, changed text-file contents and captured test
tool results. Reviewers use the current session model with structured scores
from 0 to 10. They have no
executable tools and cannot edit files, approve tool calls, or see peer scores.
The lowest score controls the result: every reviewer must meet the target.

When a score falls below the target, the existing lead agent receives the
findings as untrusted review data, fixes valid issues using its normal tools and
approvals, then enters a fresh review round. Existing goal and learning workflows
continue to use their existing streams.

Each review records a content fingerprint of the Git workspace, including
nonignored untracked files. Changes during review force another round. Oracle
checks freshness again before finishing, and checks displayed results in a
background worker every two seconds. Modified results become **stale**; old
scores cannot approve new changes. Missing, invalid, timed-out or failed reviews
never count as passing scores.

## Settings and decisions

Choose **Settings** beside the Oracle toggle, or run `/oracle-mode settings`, to set:

- Target score: greater than 0 and at most 10; default **8**.
- Automatic round limit: **1–20**; default **3**.
- Reviewer count: **1–5**; default **2**.
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

Each round makes one model call per reviewer, concurrently, plus any lead fix
turn. Oracle attributes reviewer calls to `oracle/reviewer-N` in usage tracking.
Independent contexts avoid manual agent-to-agent coordination, but speed and
token savings have not been benchmarked.

Oracle currently requires a readable local Git workspace with an existing commit
and a model supporting structured output. Evidence is limited to 64,000
characters; oversized, binary or unreadable files changed during the task and submodules pause
review rather than silently receiving a passing grade. Sandbox files that are
not mirrored into the local workspace are outside this review path. Split large
tasks or explicitly accept the result when automated review cannot inspect it.
Noninteractive consumers cannot grant an Oracle owner decision; they pause at
decision points. Scores are advisory model judgments, not a replacement for
tests or your review.

Binary edits already present when a task starts do not block unrelated code
reviews if their contents stay unchanged. They are listed as pre-existing and
unreviewed in the reviewer evidence. Text diffs already present in the workspace
remain available for requests to review existing changes. Binary files added,
modified or deleted during the task still block approval, with their paths in
the panel. **Retry review** retries without an extra lead edit; **Accept and
finish** ends without claiming Oracle passed. A blocked review offers Retry,
Accept and Pause; score-based controls return when review evidence is available.
