# NovaCode v1.0.31

## Added

- Telegram remote commands now use the normal TUI handlers in the main session topic, including skills, model selection, settings, and plugin commands.
- Interactive command panels expose numbered choices in chat. Use `/choose N`, `/set N value`, `/input N value`, `/panel N`, and `/back` to select actions, edit fields, submit forms, navigate pages, and close panels. Controls belong to the requesting sender and topic; password fields are masked.
- Telegram's native slash-command autocomplete menu includes registered commands and enabled skills. Menus refresh in the background after connection, skill management, and plugin registration; unchanged menus do not issue another registration request.
- Child sessions resolve slash commands through shared CLI handlers and invoke skills in their own workspace, preserving skill arguments and pinned prompts.
- Included context-display screenshots (`CTX1.png` and `CTX2.png`) and regression coverage for context-gauge updates while idle.

## Fixed

- Commands sent during a running main-session task are queued until the turn finishes, with a bounded queue and cleanup on shutdown.
- Telegram command names with a bot suffix route correctly, including panel controls. Menu names use underscores in place of hyphens and resolve back to the original commands.
- Failed, cancelled, expired, and undeliverable remote command panels release their resources; expired panels dismiss waiting dialogs.
- Large command panels can be paged, and long command output is split for delivery.

## Limits and validation

- Telegram permits at most 100 command-menu entries, shared across the chat's topics. Commands take priority, followed by enabled skills; other skills remain callable with `/skill:name`.
- TUI-only management panels are available in the main topic. Child topics use the shared CLI handlers for other commands.
- Remote command, autocomplete, streaming, topic, and session-worker regression tests pass with mocked Telegram APIs. Live Telegram behavior has not been verified.
