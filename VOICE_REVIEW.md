# Voice implementation and review — 2026-10-24

User requested Whistle STT support, then a voice pipeline review. Confirmed
preference: keep short spoken summaries. Proceeding under existing authorization;
no separate spec approval or independent agent verification.

## Evidence and plan

- Official model: https://huggingface.co/Cactus-Compute/whistle — 16.9 MB `.cact`,
  16 kHz mono float input, seven languages. Official Python implementation:
  https://github.com/cactus-compute/needle/blob/main/needle/agent/whistle.py.
- Official `needle/agent/fetch.py` includes Windows x64/ARM64, Linux and macOS
  engine wheels. `cactus-needle` 3.2.1 requires Python >=3.9 and huggingface_hub;
  its Whistle runtime is explicitly process-global and not thread-safe.
- Add an opt-in `voice-whistle` extra and lazy provider matching VoiceSTT. Reuse
  Nova capture/VAD. Normalize int16 to float32 and serialize native runtime use
  across instances; no STT model or native binary downloaded during tests.
- Preserve short spoken summaries. Extract only public text blocks from model
  output; never stringify structured reasoning, tool calls or tool results.
- Centralize local synthesis/playback cancellation so cancelled worker threads
  cannot start audio later. Retain original sample rates, padding and buffering.
  Serialize synthesis to avoid re-entering native model objects after cancellation.
- Verify automatic TUI speech accepts only main assistant prose, excludes tool,
  reasoning and subagent events, and rechecks voice settings after async work.

Acceptance: provider discoverable/configurable, lazy/off-loop model use, helpful
missing-dependency error, correct PCM normalization, no concurrent Whistle native
calls; summaries remain short and text-only; cancelled speech cannot resume;
existing provider/voice settings continue working. Test boundaries use fake audio
devices/models, with explicit checks for cancellation races and payload filtering.

Baseline voice command:
`uv run --no-sync python -m pytest tests/test_audio tests/test_voice_catalog.py tests/test_voice_credentials.py tests/test_remote_voice_notes.py -q --tb=short`
Result before voice edits: 98 passed, 1 skipped, 1 existing failure:
`TestCapability::test_install_hint_mentions_extra` expects lowercase `voice` in
an install hint that currently starts `Voice needs ...` in this environment.
Keep this baseline distinct from new regressions.

Review clarification: the baseline failure is in the voice onboarding module
already being extended. Keep its install command intact and make the feature
label explicit (`Nova voice needs ...`), resolving the existing case-sensitive
hint contract without changing assertions.

Known limits: no live microphone/speaker or downloaded-model quality claim;
native synthesis itself cannot be forcibly stopped, but cancelled synthesis must
never play. No default provider change, cloud credential change, release or push.
New dependency is optional `cactus-needle`, justified by Whistle's incompatible
native model/runtime; existing Faster-Whisper cannot load `.cact` weights.

## Completed review and evidence

Implemented optional Whistle provider, registry/picker integration, installer hint,
and shared STT factory path (also used by remote voice notes). Default providers
and saved user settings were not changed. `voice-whistle` resolves cactus-needle
3.2.8; its sole base dependency, huggingface_hub, was already in Nova's lockfile.
The runtime package is Apache-2.0. PyPI's version metadata reported no published
advisories for 3.2.8; this is not a full dependency-tree vulnerability audit.

Five voice regressions were reproduced before fixes:

| Finding | Fix and proof |
| --- | --- |
| Structured summaries spoke Python representations containing reasoning/tool data | Use LangChain's text-only message accessor; structured/nontext response tests |
| Cancelled synthesis could play later | Per-request cancellation state and output ownership in shared playback; real Piper path with fake audio device |
| Simultaneous speech overlapped and cleared echo suppression early | Serialize pipeline speech across event loops; overlap and worker-loop tests |
| A stopped listener submitted an in-flight transcript | Recheck stop/TTS state after STT completes; late-transcript test |
| Subagent text entered automatic speech, and disabling speech during summarization was ignored | Keep only main assistant prose and recheck voice settings; Textual event/worker tests |

Short spoken summaries remain enabled as requested. Tool calls, tool results,
reasoning events and subagent replies are excluded from automatic speech.
Synthesis/playback code now shares padding, buffering, serialization and
cancellation logic across Piper, Pocket, Orpheus and ElevenLabs. A native worker
cannot be forcibly killed, but cancelled work cannot begin playback afterward.

Final reproducible offline command:

`uv run --no-sync python scripts/verify_voice.py`

Final result after code edits: exit 0, **131 passed, 1 skipped** with randomized
ordering (seed 24102026). The skip is the pre-existing Pocket lifecycle test
requiring the optional sounddevice package; shared playback was exercised with
fake audio devices. Combined statement/branch coverage for Whistle, playback
and summarization is **95%** (127 statements, 26 branches; 85% enforced gate).
This is not a 100% changed-line coverage claim. New modules/tests/verification
scripts passed ruff/format; all eight affected audio implementation modules
passed mypy. The existing legacy-file lint baseline did not worsen. Lockfile
validation, CI YAML parsing and `git diff --check` passed. Voice tests were added
to the offline CI selection.

Three negative controls executed and failed assertions: allowing cancelled
synthesis to play, removing Whistle's native lock, and replacing LangChain's
text-only extraction with stringification. Each control records an execution hit;
a control that never runs, survives, times out or exits unexpectedly fails the
verifier. No source files are mutated by these controls. The third control
changes the message accessor contract rather than replacing application source.

Actual native verification, isolated from the project's environment:

`uv run --no-project --with "cactus-needle==3.2.8" --with numpy python scripts/verify_whistle_native.py`

Result: exit 0; actual Windows DLL/model loaded and one second of 16 kHz silence
transcribed to empty text. Minimum supported package 3.2.1 was also exercised.
The probe uses a child process so Windows releases the DLL before its disposable
cache is removed. The initial probe succeeded at inference but failed cleanup
because its DLL was still loaded; the child-process fix was verified and the
initial leftover cache was removed. Models were not installed into user caches
or the project environment, and no microphone or speaker was accessed.

Voice source SHA-256:
`c0da83db6b4b4d395894b5a0b4aa1eda1e797bc12127bb2d6aa666177e89e0d4`.
Hash construction is the same sorted-path/NUL/raw-bytes construction documented
in CODE_MODE_EVIDENCE.md, over:

- `novacode_cli/audio/{stt_whistle,playback,providers,pipeline,__init__,summarize,tts,tts_pocket,tts_orpheus,stt_elevenlabs}.py`
- `novacode_cli/tui/app.py`
- `tests/test_audio/{test_whistle,test_voice_safety,test_summarize}.py`
- `tests/{test_tui_voice_output,test_voice_catalog}.py`
- `scripts/{verify_voice,verify_whistle_native}.py`
- `pyproject.toml`, `uv.lock`, `README.md`, `.github/workflows/ci.yml`

Remaining verification limits: no live microphone/speaker quality test, no live
cloud voice account, no transcription-accuracy benchmark, and no whole-repository
test run. The native smoke test demonstrates runtime compatibility, not speech
quality. Existing provider models remain opt-in. No release or push was made.

## Enable from this checkout

Install the base audio stack and optional provider:

`uv sync --extra voice --extra voice-whistle`

Then select `/voice settings stt whistle`. Whistle downloads its model and native
runtime on first use. Existing `/voice` controls and short TTS summaries are
preserved; Piper or the user's selected TTS provider still handles speech output.


## Download visibility and speech startup follow-up

Added separate STT/TTS progress rows above the composer for first use, warmup,
and `/voice download`. Direct Piper/Parakeet transfers report real bytes;
voice-scoped Hugging Face public progress hooks report bytes when exposed by
the installed backend. Unknown totals or older/previously imported vendor hooks
fall back to indeterminate preparation. Cached operations stay quiet. Errors do
not announce readiness, cancelled scopes ignore late thread updates, and an
operation sequence prevents stale updates on Windows clocks with coarse timing.
The widget handles messages independently so progress renders even while the
app's push-to-talk action awaits transcription. No new dependency was added.

Speech startup review found and fixed:

- Long replies awaited an unbounded second LLM request. Model setup and summary
  invocation now share a two-second budget; slow calls fall back to short,
  filtered reply prose. Short replies still bypass the summary model.
- Piper accumulated every sentence before playing audio. It now plays each
  complete sentence immediately, retaining cancellation ownership, serialization,
  sample rate and output padding. Cancellation stops further sentence generation.
- Resetting the voice pipeline during summarization could route an old reply to
  the new pipeline. Identity checks now discard that reply. Preparation and
  download reporting are owned by `VoicePipeline.speak`, avoiding duplicate TUI
  download/warmup logic.

Verification: `uv run --no-sync python scripts/verify_voice.py` exited zero:
151 passed, one optional-dependency skip, 89% combined statement/branch coverage
for the three gated voice modules, and all three deliberate negative controls
failed their assertions as expected. After the final cancellation regression,
21 summary/playback tests passed; the final command/progress/TUI selection passed
25 tests. New source/test lint and source mypy passed; legacy-file lint did not
worsen. Tests use fake transfers, models and audio devices.

Limits: this bounds summary waiting, not total end-to-end speech latency.
First model download/load and first sentence synthesis still take their native
time. Piper sentence boundaries retain safe playback padding and incur the next
sentence's synthesis time; optional Pocket/Orpheus/cloud providers retain their
existing buffered synthesis. No live microphone/speaker timing or real network
model-download test was performed in this follow-up.


## Lightweight TTS choices from the supplied model list

Selected two model families, exposed as optional CPU providers with ten English
voice choices. Existing providers and the saved/default selection are unchanged.

- **Kokoro 82M INT8**: 92,361,271-byte quantized weights plus 28,214,398-byte
  voice pack, via `kokoro-onnx`. Original hexgrad and ONNX-community entries are
  the same family, so they do not become duplicate providers. Nova uses the
  runtime publisher's compatible v1.0 INT8 export, forces CPUExecutionProvider,
  and limits inference to two intra-op threads and one inter-op thread.
- **sanoTTS**: four English piperlite voices, 1.08-1.83M parameters with roughly
  2-4 MB weights each. Default `amy-1p1m` favors the smallest practical voice.
  Native NumPy runtime, no PyTorch or GPU. Download only the selected pack;
  runtime validates its published size/hash. Lower-quality nano voices are not
  exposed for assistant speech.

Sources: [Kokoro ONNX model card](https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX),
[runtime v1.0 assets](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0),
[sanoTTS model card](https://huggingface.co/ampixa/sanoTTS).
Other listed families were not added: heavyweight generators require more
compute; model collections, tuners, and standalone vocoders are not independent
lightweight assistant backends. VieNeu remains a possible language-specific
follow-up; no complete low-resource memory assessment was performed for its
backbone, codec and frontend.

New optional extras: `voice-kokoro` pins `kokoro-onnx==0.4.9`; `voice-sano` pins
`sanotts==0.3.0`. Both use `phonemizer-fork` and bundled espeak-ng. The newer
Kokoro runtime depends on the separate overlapping `phonemizer` distribution;
using the validated version avoids competing distributions writing that module.
The extras resolve together and do not change the default installation. Kokoro
weights/runtime are Apache-2.0/MIT respectively; sanoTTS is GPL-3.0.

Both providers reuse shared atomic byte-progress downloads, speech cancellation,
serialization and sentence playback. `/voice settings`, `/model` and `/settings`
use the existing registry. Missing optional runtimes produce installation hints
that preserve a uv tool installation's source/extras.

Proof: full offline voice verifier exited zero with **166 passed, 1 skipped**,
all deliberate negative controls killed, source mypy and new-file lint/format
passed. Focused selection/voice-command tests passed after hint consolidation.
Separate disposable environments exercised actual provider wrappers and real
weights on Windows CPU with audio output captured instead of played. sanoTTS
produced 22,050 Hz audio (about 4.33 s cold including download, 0.16 s warm for
`Tests passed.`); Kokoro produced 24,000 Hz audio (about 21.66 s cold including
120 MB download, 1.83 s warm). These short samples are not a standardized speed
comparison, nor a claim of real-time speed on every low-resource machine.
Physical speaker playback and peak process RAM were not measured. Weight sizes
are download footprints, not total RAM requirements. Nothing was pushed.


## Orpheus freeze after Settings download

User reported an unresponsive TUI after downloading Orpheus through Settings.
Inspection of the installed `orpheus-cpp` 0.0.3 implementation found its
constructor passes `n_ctx=0` to llama-cpp-python (full trained context) and creates
an unrestricted ONNX decoder session. Native loading runs off-loop already,
but these defaults can allocate a very large KV cache and contend for CPU just
after the GGUF transfer finishes. No multi-GB allocation was attempted to
reproduce memory exhaustion on the user's machine.

Nova now initializes the runtime with a 4,096-token context, 128-token prompt
batches, and two generation/batch threads. The SNAC decoder uses CPU only with
two intra-op threads and one inter-op thread. Upstream exposes no context-size
constructor argument, so the adapter initializes its two model fields while
retaining upstream streaming/decoding. Pin the existing optional dependency to
0.0.3 because this relies on those implementation fields; upgrading it requires
reviewing that contract. No new dependency was added.

The progress row switches from transfer bytes to model preparation while native
initialization continues. Orpheus cache detection now checks both GGUF and SNAC
assets, and command cache detection (including optional imports) runs off-loop.
Previously an Orpheus-only missing download could be incorrectly reported as
already downloaded.

Proof: resource-limit regression failed against the previous full-context
configuration, then passed after the fix. Settings regression uses the actual
Download models button and holds native initialization after the GGUF transfer;
Escape closes Settings and typing reaches the prompt before loading is released.
Combined Orpheus, command, progress, and Settings checks passed 53 tests.
The full offline voice verifier passed 173 tests with one optional-dependency
skip; its lint, formatting, source type checks and negative controls passed.
A disposable environment with the real pinned Orpheus package exercised its
streaming/decoder methods against fake native boundaries, confirming adapter
compatibility without downloading weights or playing audio. Physical playback,
peak RAM, and a full native Orpheus load on the user's machine remain unverified.
Orpheus remains a multi-GB model; this change does not make it lightweight.
