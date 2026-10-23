# NovaCode v1.0.89

## Fixes

- Mirror full terminal prompts to connected remote chats alongside live tool
  activity and streamed replies in the matching session thread.
- Attach local child-session turns to their remote threads when work starts,
  and include the current terminal prompt when connecting during a running turn.
- Prevent duplicate streams when a Discord connection becomes ready while a
  terminal prompt is being mirrored. Report prompt delivery failures without
  interrupting the local agent turn.
- Explain inaccessible Discord parent channels with their channel ID, API error
  code, permission requirements and steps to repair the saved configuration.

## Verification

- Add regressions for full prompt delivery, child-session attachment, late
  connections, delivery failures and concurrent stream attachment.
- Verify Discord thread lifecycle, routing and unavailable-parent handling.
