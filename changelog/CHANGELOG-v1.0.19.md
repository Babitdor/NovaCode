# NovaCode 1.0.19 — Live Discord and Telegram responses

- Stream local TUI turns, including Ctrl+B background agent tasks, to connected Discord and Telegram chats. Show a separate live activity message for the session, plan, tool calls, and their results, alongside an editable answer.
- Stream replies to remote prompts, including routed child sessions, without sending the final answer twice. Start the TUI message consumer when a bridge is enabled after launch and avoid duplicate consumers.
- Coalesce token updates off the TUI rendering path. Retry the latest answer after failed edits, retain bounded buffers, and stop streaming pumps on completion or cancellation.
- Render Telegram Markdown as supported HTML and paginate without breaking tags. Keep Discord fenced code blocks balanced across message pages. Reconcile all pages after answer replacement or shrinkage instead of dropping overflow text.
- Refresh /remote connection status live and explain that connected chats receive local tasks. Suppress automatic Discord mentions in model output.
- Honor Telegram flood-control retry delays and treat unchanged edits as successful. Preserve final Markdown indentation and keep an unavailable debug log from preventing remote startup.
