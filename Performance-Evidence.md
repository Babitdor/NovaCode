# Multi-session resource improvements

Implemented against `c7b3e16`, for the reported 16 GB Windows machine with two
sessions and voice/MCP enabled. These changes reduce avoidable work; the reported
freeze on the other machine has not been reproduced or proven resolved.

## Behavior

- Voice models load on demand. Default push-to-talk no longer preloads every
  voice model at startup. Speaking constructs TTS without microphone, VAD or STT.
  Explicit `/voice download` still warms the full stack. First voice use can take
  longer while its model loads; subsequent use reuses the pipeline.
- Status polling is 2 Hz while idle/unfocused and 10 Hz during focused turns.
  Activity and focus changes reschedule the single timer immediately.
- The home banner draws a static themed frame; `NOVA_ANIMATIONS=1` restores rain.
  Static rendering supports resize, theme changes and measurement before mount.
- Pending live tool output retains at most 32 buffers of 65,536 characters each.
  Large output retains its newest tail with a truncation marker. This limit
  applies to pending display output, not the stored session/tool result.
- OpenMP, MKL, NumExpr and OpenBLAS default to one native thread. Explicit user
  environment settings take precedence.

## Measurement

Two separate headless TUI processes, five seconds sampled after settling, using
the same benchmark/interpreter for HEAD and the changed tree:

| Per-session measurement | HEAD | Changed tree |
| --- | --- | --- |
| Idle status ticks | 100 / 100 | 10 / 10 |
| Banner animation ticks | 76 / 76 | 0 / 0 |
| CPU (% of one core) | 4.05 / 4.37 | 1.25 / 0.31 |
| Working set (MiB) | 116.90 / 116.96 | 116.54 / 116.65 |
| Event-loop delay p95 (ms) | 12.56 / 12.98 | 12.07 / 12.41 |

These short samples establish reduced idle work. They exclude full agent setup,
native voice model loading, MCP servers and network inference. RAM was essentially
unchanged in this text-only benchmark; model-memory savings were not measured.
Mean loop delay increased (about 7.3 to 11 ms), so these samples do not establish
a general latency improvement. External MCP processes can still consume memory.

## Verification

- Focused regressions: **129 passed, 1 skipped**. Covers resource contracts,
  audio, MCP sessions, crash recovery, clearing sessions and active subagents.
- Output buffer: **100% statement and branch coverage** (35 statements,
  10 branches). Other changed modules do not have a full coverage gate.
- Four deliberate regressions detected by assertion failures: removing the
  output cap, retaining the wrong tail, restoring rapid idle polling and loading
  input components during speech. Source mutations run only in child memory.
- Seeded adversarial chunk invariants and concurrent producer/flush stress pass.
  This is not a Hypothesis/fuzz-framework campaign.
- New module/tests/scripts pass Ruff lint and format checks. New buffer module
  passes mypy. Existing modified modules retain 789 baseline Ruff diagnostics;
  no increase by file/rule count. They are not lint-clean or fully type-checked.
- Wider suite: **2,497 passed, 5 failed, 18 skipped, 1 xpassed**, 311.72 seconds.
  Stopped after five failures at about 84%; the remainder was not run. All five
  failures reproduced in an isolated unchanged HEAD checkout:
  - `test_lazy_heavy::test_langchain_degrades_gracefully_rather_than_crashing`:
    the locked LangChain version removed the private `_HAS_TRANSFORMERS` field.
  - Three `test_remote_voice_notes` decoding/transcription tests: optional
    `faster-whisper` is absent from this verification environment.
  - `test_tui_app::test_tui_sessions_screen`: existing screen expectation mismatch
    (`SessionsScreen` versus `PickScreen`).

The run was autonomous, without spec approval or independent agent verification.
Tests isolate user configuration, home and SQLite state; live user sessions were
not modified. Runtime dependencies and lockfile are unchanged.

## Reproduce

PowerShell, from the repository root (Python 3.13.5; locked Textual 8.2.7,
DeepAgents 0.7.10, LangChain 1.3.18):

```powershell
$env:UV_PROJECT_ENVIRONMENT = Join-Path (Get-Location) '.tmp-resource-env'
uv sync --frozen --group dev --no-install-project --python 3.13
uv pip install --python .tmp-resource-env/Scripts/python.exe coverage==7.15.4
& .tmp-resource-env/Scripts/python.exe scripts/verify_resource_efficiency.py --test tests/test_resource_efficiency.py --test tests/test_audio --test tests/test_mcp_sessions.py --test tests/test_tui_crash_autosave.py --test tests/test_session_crash_recovery.py --test tests/test_session_clear.py --test tests/test_tui_subagent_panel.py --coverage
& .tmp-resource-env/Scripts/python.exe scripts/mutate_resource_efficiency.py
& .tmp-resource-env/Scripts/python.exe scripts/verify_resource_efficiency.py --full --maxfail=5
python scripts/benchmark_idle.py --sessions 2 --seconds 5
python -m ruff check novacode_cli/tui/output_buffer.py tests/test_resource_efficiency.py scripts/benchmark_idle.py scripts/verify_resource_efficiency.py scripts/mutate_resource_efficiency.py
python -m ruff format --check novacode_cli/tui/output_buffer.py tests/test_resource_efficiency.py scripts/benchmark_idle.py scripts/verify_resource_efficiency.py scripts/mutate_resource_efficiency.py
& .tmp-resource-env/Scripts/python.exe -m mypy --follow-imports=skip --ignore-missing-imports novacode_cli/tui/output_buffer.py
git diff --check
```

For the baseline benchmark, archive HEAD into a separate directory and copy only
the benchmark script there before running it. The global SDK environment used
for the paired benchmark differs from the locked test environment; do not compare
its absolute memory values with a fully initialized production session.
