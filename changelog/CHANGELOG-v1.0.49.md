# NovaCode v1.0.49

## Fixed

- Follow-up messages in the main terminal, session tabs and Telegram reach a
  running agent at its next model step. Input received during a final response
  can continue the same turn, and unread late input remains available for a
  subsequent turn. In-flight model and tool calls finish before pickup.
- Model switches and MCP reloads preserve the shared live-input queue.
- Closing or sending input to a child session no longer cancels another
  session's turn. Input retains its intended session across tab switches.
- Model routing actions use a responsive grid so Cancel and the other buttons
  remain visible and clickable on smaller terminals.

## Validation and limits

- 211 focused steering, session, remote and TUI regression tests passed. The
  broader run also detected a user-config change at teardown; subsequent
  45-test and 21-test runs passed with the config guard clean.
- Router regressions passed at five terminal sizes. Live model and Telegram
  service calls were not run. No dependencies were added.
