# NovaCode v1.0.76

## Fixes

- Bound background async model turns to four minutes including retry delays, with at most two Nova retries. Disable nested OpenAI-compatible/Anthropic SDK retries and set their request timeout to two minutes without changing the selected model or shared foreground clients.
- Keep monitoring long-running async tasks instead of silently dropping them after 30 minutes. Warn after five minutes, report repeated server connection failures and recovery, and allow monitoring to restart after setup failures. Limit concurrent status requests rather than entire task watches so slow agents do not block completion notifications for later tasks.
- Add offline regression tests for actual SDK request counts, stalled-call cancellation, client isolation, delayed task completion and monitoring recovery.
