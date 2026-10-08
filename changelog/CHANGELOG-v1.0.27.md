# NovaCode v1.0.27

- On Telegram reconnect, validate saved session topics and recreate topics that Telegram confirms were deleted. Persist the replacement ID and bind incoming messages and outgoing responses to it.
- Reuse valid topics and reopen closed ones. Network failures, rate limits, and permission errors leave the saved mapping intact and report a connection warning instead of creating duplicate topics.
