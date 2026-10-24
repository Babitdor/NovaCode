# NovaCode 1.0.97

## Code Mode for native tools

- Add a persistent Code Mode toggle below the prompt composer, with mouse and keyboard support.
- Compose ordinary registered Nova tools through JavaScript using `tools.list`, `tools.describe`, and `tools.call`, including sessions without MCP servers.
- Preserve existing `MCPCode` and `mcp.*` scripts. Nested calls retain argument validation, policy checks, workspace guards, tracing, and execution limits.
- Keep interactive approvals, delegation, and graph-control tools on their direct call paths to avoid replaying completed actions or losing state updates.

## Whistle speech input

- Add Whistle as an optional local STT provider through the `voice-whistle` extra, using the Cactus runtime instead of Faster-Whisper.
- Expose Whistle in voice settings and the model picker, with lazy model loading, normalized audio input, and serialized native calls across instances.
- Install with `uv sync --extra voice --extra voice-whistle`, then select `/voice settings stt whistle`. Existing default providers remain unchanged.

## Voice response fixes

- Preserve short spoken summaries while excluding tool calls, tool results, reasoning, and subagent payloads from automatic speech.
- Prevent cancelled synthesis from starting playback later, serialize overlapping speech, and preserve microphone echo suppression while speaking.
- Drop late transcripts after listening stops and recheck voice settings after asynchronous summarization or warmup.
- Share playback buffering, padding, and cancellation handling across Piper, Pocket, Orpheus, and ElevenLabs.

Validation: 268 focused Code Mode tests and 131 voice tests passed. Native Whistle inference passed on Windows with `cactus-needle` 3.2.8. Live microphone/speaker quality and transcription accuracy were not benchmarked. Added one optional runtime dependency, `cactus-needle`; no default-provider dependency changes.
