# NovaCode v1.0.42

## Improved

- Added Centered, Banner, and Minimal interface styles in Settings alongside Dashboard and Classic. Styles apply immediately and preserve the conversation and multiline draft.
- Extended Banner and Minimal styling to conversations, tool output, task panels, terminals, status indicators, and modal dialogs, including `/model` and nested confirmations.
- Unified modal tabs, lists, fields, buttons, and backdrops with the selected style and color theme. Minimal uses compact inputs and borderless panels; Banner uses subtle frames.
- Removed the user-message hover background and fixed terminal text colors that ignored the selected theme.

## Fixed

- Apply interface styles on every screen change, including modal screens that open without a resize event.
- Ignore periodic home refreshes after layout widgets have been removed during shutdown.
- Preserve existing Dashboard/Classic preferences when no new interface style is saved.

## Validation and limits

- The focused home, question, subagent, terminal, and reference-screen run passed 83 checks and exposed one shutdown refresh failure; that failure passed after the fix.
- Subsequent focused runs passed the modal styling, keyboard navigation, nested confirmation, live switching, form control, and removed-layout regressions.
- Visually inspected Banner and Minimal model and settings dialogs. Home presentation and preference tests pass Ruff; home presentation passes its focused mypy check. The wider TUI retains existing lint findings.
- Tests used isolated user profiles. The full suite and live model providers were not exercised. No new dependencies were added.
