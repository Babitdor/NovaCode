# NovaCode v1.0.24

- Telegram `/remote` connects each Nova session to a named topic containing its session ID, including main sessions and sessions already running when the bridge starts.
- Separate Nova windows share an authenticated local Telegram polling connection, preventing competing `getUpdates` requests. Topic messages reach their registered Nova process; replies and local streamed output stay in that topic.
- Topic mappings and polling offsets persist, and another connected window takes over polling when the owner exits. A saved session cannot be connected in two windows simultaneously.
- Added private bot topic-mode support, retryable topic capability detection, and visible setup guidance and topic IDs in `/remote`.
- General-chat messages are delivered only when one Nova window is connected; with several windows, send messages in the session's topic.
