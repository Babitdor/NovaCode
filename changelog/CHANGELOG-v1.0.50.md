# NovaCode v1.0.50

## Added

- `/statusline` opens a checkbox picker with a live preview, Save, Reset and
  Cancel. Saved field choices persist across launches and apply across designs.
- `/auto-approve [on|off|status]` controls the current session's approval mode.
  An amber composer and explicit label indicate automatic tool approval. Child
  tabs and Telegram topics retain independent approval preferences.
- A provider reasoning-support report documents supported controls and limits.

## Fixed

- `/effort` preserves the current provider, model and shared steering list when
  rebuilding, instead of falling back to the globally saved model. `default`
  removes the override; the command describes model-specific limitations.
- OpenCode's Claude route forwards thinking settings. OpenRouter receives its
  unified reasoning control, and Command Code/OpenCode DeepSeek V4 models
  receive low/high effort settings.
- Session switches refresh status details and preserve per-session effort and
  plan-scoped approval state. Explicit approval choices survive remote shutdown.

## Validation and limits

- Focused runs passed: 143 provider/UI tests and 145 session/worker/TUI tests.
  Final checks passed 42 worker/control tests and 17 provider-wiring tests,
  including serialized HTTP-body checks with an offline mock transport.
- New picker modules and tests pass Ruff lint/formatting; picker modules pass
  mypy. Diff whitespace checks pass. Existing broader lint issues remain.
- Live authenticated model calls were not run. Supported reasoning controls
  still depend on the model and route; `off` can mean minimum effort.
- No dependencies were added. Personal configuration and unrelated local files
  are excluded from this release.
