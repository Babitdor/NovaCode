# NovaCode 1.2.34

## Oracle binary-change checks and blocked-review controls

- Allow code review when pre-existing binary edits remain unchanged during the
  task. Reviewer evidence explicitly lists those assets as unreviewed.
- Continue to block automatic approval for binary files added, modified or
  deleted during the task, and identify the affected paths. Non-UTF-8 files now
  produce a readable filename instead of a decoder error.
- Anchor review diffs to the task's starting commit, so commits made during a
  task cannot hide its changes. Preserve text diffs for requests to review
  existing workspace edits.
- Show blocked status and reviewer failure summaries in the Oracle panel.
  Blocked reviews offer **Retry review**, **Accept and finish** and **Pause**;
  accepting explicitly finishes without a passing review.
- Wrap controls in narrow terminals and scroll long errors within a bounded
  area, keeping the prompt accessible and preserving its draft.
- Make the Code Mode integration test fail promptly on an unexpected Oracle
  decision instead of waiting indefinitely for a manual response.

## Validation and limits

Regression checks cover binary changes, commits made during a task, reviewer
errors, live progress, narrow layouts, error scrolling and draft preservation.
No runtime dependency is added. Binary content changed during a task still
requires manual review.

A local QuickJS timeout also reproduces on unchanged 1.2.33. This release does
not change Code Mode execution limits.

See [Oracle mode](https://github.com/Babitdor/NovaCode/blob/v1.2.34/docs/oracle-mode.md)
for controls and settings.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.34
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Preserve any optional extras you use:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.34"
```
