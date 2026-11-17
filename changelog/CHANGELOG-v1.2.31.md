# NovaCode 1.2.31

## Oracle review mode

- Toggle Oracle above the prompt composer without sending or changing your draft.
  The strip shows the review round, lowest score, target and current status.
- Run independent, read-only reviewer contexts using the configured session model.
  Every reviewer must meet the target. Low scores feed findings to the existing
  lead agent for fixes, followed by a fresh review round.
- Bind reviews to workspace content fingerprints. Changes invalidate previous
  scores, including changes during review or while the UI displays a result.
  Failed, missing or invalid grades cannot pass.
- Expand reviewer rows to inspect findings, first/latest scores and reviewer
  threads. Choose Keep fixing, More rounds, Lower target, Accept and finish or
  Pause when a run reaches a decision point. Explicit acceptance is not shown
  as a passing grade.
- Configure target score, round limit, reviewer count and the new-thread default
  with Settings or `/oracle-mode settings`. Saved conversations remember their
  toggle; resumed conversations never restore old scores as approvals.
- Attribute reviewer calls in usage tracking and retain the existing tool
  approvals, Code Mode, goal and learning workflows. No new dependency is added.

## Defaults and boundaries

Oracle starts disabled. Defaults are a target of 8/10, three automatic rounds
and two reviewers. Limits are 20 rounds and five reviewers per run.

Review requires a readable local Git workspace with an existing commit and a
model supporting structured output. Oversized, binary or unavailable evidence,
remote sandbox files and active teammates pause review rather than silently
passing. Evidence is capped at 64,000 characters.

Automated checks cover grading, fix loops, freshness, cancellation, owner
decisions, persisted toggles, usage attribution and composer controls across
three designs and multiple terminal sizes. Live-provider behavior and token or
speed savings have not been benchmarked.

See [Oracle mode](../docs/oracle-mode.md) for controls and details.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.31
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Preserve any optional extras you use:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.31"
```
