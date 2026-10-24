# NovaCode 1.0.98

## Voice model download progress

- Show separate STT and TTS progress rows above the prompt composer during first use, model warmup, and `/voice download`.
- Display downloaded bytes and percentages when the backend exposes a total; use an indeterminate indicator otherwise. Cached models stay quiet.
- Keep progress responsive while push-to-talk waits for transcription, reject stale pipeline/operation updates, and ignore late worker updates after cancellation.
- Share atomic streaming downloads between Piper, Parakeet, Kokoro, and sanoTTS, with timeout and transfer-length validation.
- Report failed warmup accurately instead of announcing that models are ready.

## Faster speech startup

- Limit the additional spoken-summary model call, including model setup, to two seconds; use a short filtered prose fallback when it exceeds that budget.
- Play Piper sentences as soon as they are synthesized instead of buffering the complete response, and stop generating later sentences after cancellation.
- Discard replies whose voice pipeline was replaced during summarization. Keep short spoken summaries and exclude tool calls, tool results, reasoning, and subagent output.

## Lightweight CPU speech options

- Add optional Kokoro 82M INT8 speech through `voice-kokoro`, with six English voices, CPU-only inference, and bounded inference threads. Weights and voice data download to roughly 120 MB total.
- Add optional sanoTTS through `voice-sano`, with four English voices and roughly 2-4 MB of weights per selected voice. The NumPy runtime needs no GPU or PyTorch.
- Configure both through `/model`, `/settings`, or `/voice settings tts`; preserve existing providers and the default voice selection.
- Pin optional runtimes to `kokoro-onnx==0.4.9` and `sanotts==0.3.0`, which share the compatible phonemizer distribution. Include installation hints and updated setup documentation.
- Include the existing feature inventory document for landing-page reference.

Validation: the offline voice verifier passed 166 tests with one optional-dependency skip; source type checks and new-file lint/format passed. Actual Kokoro and sanoTTS provider wrappers synthesized audio on Windows CPU in disposable environments. Physical microphone/speaker behavior, peak RAM, and universal low-resource performance were not benchmarked. Download footprints are not total runtime RAM requirements.
