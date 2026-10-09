# NovaCode v1.0.28

## Added

- Added approval-gated `/session launch` for opening an existing approved folder as a session tab with an initial task.
- The new tab receives a dedicated Telegram topic through the current session's remote bridge and routes topic messages to that tab.
- Added project pickers to the local TUI and Telegram so approved folders can be selected without typing paths.
- Renamed the local session management command to `/tabs`; `/tab` in Telegram opens the project picker, and `/tab close` offers a picker of running tabs.
- Session tabs animate while agents work and show live Telegram/Discord connection indicators.
- Added a keyboard-accessible jump-to-latest button in every transcript; tabs remember whether you are reading history.
- Telegram photos and image documents now reach the corresponding Nova session as image inputs, with optional captions and queued delivery while busy.
- Tabs inherit the source tool approval mode and send it per turn; remote connections never enable auto-approve. Dismissed tool approval dialogs reject the request.
- Telegram tab pickers are removed after launch approval, cancellation, closing, or expiry. Repeated confirmations cannot start another tab or reach the agent as prompts; new pickers supersede older requests in the same requester/topic scope.
- Tab switching returns immediately while history replays in small batches. Background token bursts no longer repaint the tab bar per token, and spinner frames avoid layout when their width stays the same.
- Stream buffers retain their parts across switches; delayed stream/tool repaint callbacks belong to their originating pane. Approved-folder disk checks run off the UI thread, and concurrent launches reserve capacity before starting processes.
- The footer names the selected tab and shows only its tasks. Child tabs have their own task snapshots and `/tasks` panel; terminate, restart, and logs controls route to that child's process. Main-tab completion logs, notes, and notifications remain in the main conversation.
- Task panel polling transfers metadata only; log requests fetch one bounded tail and match the originating tab and request ID.
- Escape and Ctrl+B in child tabs no longer kill or detach main-tab commands. Closing a worker stops its background commands, and exited tabs clear running task indicators.
- Windows termination stops the whole process tree before the shell exits, preventing orphaned child processes. Background launch failures report failure instead of success. Task tool descriptions explain session-local IDs and how to stop and verify tasks.

## Limitations

- Telegram can request and approve a launch from its originating topic. Existing approved folders only are supported.
