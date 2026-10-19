# NovaCode v1.0.46

## Improved

- Startup resume, `/resume`, and reopening saved tabs share a searchable session browser. Search tasks, projects, models, or session IDs while preserving the correct session selection.
- A scrollable details pane shows the saved task, project, model, status, blockers, and next step. Legacy sessions clearly indicate when their original model cannot be restored.
- Resume dialogs follow the selected design and color palette. Compact layouts keep search, actions, and the selected session visible while preserving the current draft.
- The resume-mode dialog explains the existing summary-first and as-saved choices.

## Validation and limits

- 79 focused regression tests passed, covering resume choices, saved-model restoration, saved tabs, session clearing, and design preferences. The final nine UI tests also passed after the compact-selection adjustment.
- The new dialog and its tests pass Ruff and formatting checks; the new dialog passes mypy. Headless previews were reviewed at desktop and compact sizes.
- No new dependencies or live provider calls. Native terminal rendering was not manually tested.
