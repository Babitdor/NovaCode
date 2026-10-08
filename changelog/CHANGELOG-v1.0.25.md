# NovaCode v1.0.25

- Telegram group messages rejected by sender authorization now display a throttled explanation in Nova, including the sender ID and the command to authorize that user.
- Added local `/remote allow-user telegram <USER_ID>` and `/remote deny-user telegram <USER_ID>` commands (also available for Discord). Permissions persist and update active bridges immediately in the current Nova process.
- Preserve registered session topic IDs when incoming Telegram messages omit `is_topic_message`, keeping prompts and replies routed to the correct session.
