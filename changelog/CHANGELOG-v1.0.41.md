# NovaCode v1.0.41

## Improved

- Added a persistent **Redesigned home screen** toggle under **Settings → Appearance**. Turn it off for the classic banner and chat layout; changes apply immediately without replacing the conversation.
- Classic mode skips dashboard resource sampling and hides its taller banner in short terminals to keep the composer usable.
- Removed decorative vertical accent bars from messages, task cards, notices, terminals, and detail panels, including rendering code that recreated bars during updates.

## Fixed

- `/clear` now rebuilds the full fresh-chat context estimate, including tool schemas, before refreshing the CTX indicators and home panels. Opening `/context` no longer changes the count merely by including those schemas.
- Ignore late system-resource samples after the dashboard panel has been removed during shutdown.

## Validation and limits

- All 121 focused offline home-layout, context, animation, message, task-card, reference, workspace, harness, and terminal regression checks passed across two runs.
- New test files pass Ruff lint and formatting checks; the wider repository retains existing lint findings.
- Live-provider behavior was not exercised. No new dependencies were added.
