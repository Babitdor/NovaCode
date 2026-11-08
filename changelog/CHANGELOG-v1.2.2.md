# NovaCode 1.2.2

## Session resume

- Browse saved conversations across projects with `/resume` and `nova --resume`.
- Choose the saved workspace or current workspace when resuming another
  project's session, with optional “Always use” preferences. Reset the choice
  with `nova config set resume_directory ask`.
- Open the saved workspace in an isolated session tab while preserving the
  current chat. Choosing the current workspace restores the conversation in place.
- Preserve workspace trust and tool approvals, context-mode choices and saved
  session identity. Switch to an already-open session instead of loading it twice.
- Keep the existing 200-session browser limit and exclude cleared sessions.

## Fixes

- Fix the Textual widget error when displaying a compacted conversation summary.

## Packaging

- Show the release summary and changelog link on PyPI, and attach this changelog
  to the GitHub release.
