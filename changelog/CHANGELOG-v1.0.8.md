# NovaCode 1.0.8

## Fixed

- Command output renders only visible rows, with bounded row caches, instead of rendering entire logs on each paint. Hidden output remains available and reflows when revealed or resized.
- Removed output and chat widgets immediately release their text, markdown visuals, and render caches.
- Late background output and stream callbacks cannot revive removed cards or restart their paint timers.
- Transcript scrollback is bounded by content size as well as widget count, while preserving active replies and tool/subagent cards. Persisted session and agent context remain intact.
- Streaming previews read only recent fragments instead of repeatedly copying the full growing answer and reasoning buffers.
- Long turns retain speech-summary text only when voice responses are enabled, with a bounded summary buffer.
