# Multi-session resource work

This is an autonomous implementation under the user's request to reduce Nova's
memory use, latency and CPU load. Spec approval: not obtained (autonomous run).
Verification is scoped to the behaviors below, not a guarantee that every source
of PC lag is fixed. Use the existing Python/pytest/Textual toolchain, standard
library measurements and no new dependencies. No automatic commits or pushes.

## Acceptance criteria

1. Opening a text session must not construct or preload voice models just because
   the default mode is push-to-talk. Explicit voice activation and configured
   listening still work. Speech-only use builds TTS without capture, VAD or STT.
   An explicit `/voice download` retains the full-stack warmup path.
2. Idle status checks run at most twice a second; focused active turns update at
   ten frames a second, and unfocused turns at most twice a second. Becoming
   active restarts the timer promptly. Notification/bridge updates remain live.
3. The home banner renders a static themed frame by default. Users can opt into
   animated rain with `NOVA_ANIMATIONS=1`. Resizing and theme changes still work.
4. Live output awaiting painting has a bounded tail per call (64 Ki characters)
   and at most 32 call buffers. Overload truncates display only, with a marker;
   persisted tool results and session recovery remain intact. Concurrent producer
   threads and flushes must neither corrupt buffers nor lose scheduling.
5. Native CPU libraries respect explicit environment thread settings; otherwise
   OpenMP, MKL, NumExpr and OpenBLAS default to one thread per Nova process.

## Failure model and verification

- Accidental native model loading: fake provider boundaries verify which actual
  pipeline paths construct components; startup tests exercise the actual policy.
- Stalled active UI or duplicate timers: headless Textual tests exercise timer
  transitions, focus and cleanup; preserve existing rendering regressions.
- Backlog growth or corruption: adversarial multi-megabyte chunks, many call IDs,
  concurrent producer/flush stress; assert limits and the newest tail content.
- Changed session correctness: existing crash-save, clear, session and subagent
  checks; no checkpoint retention changes in this pass.
- CPU/latency: reproducible headless idle benchmark before and after, including
  process CPU time, working set where available, tick counts and loop latency.
  It excludes network/model inference and native voice model memory, and cannot
  reproduce the reported freeze on a different machine.

Run new regression tests RED before implementation, then targeted existing
checks, scoped lint/types/coverage and deliberate regressions where feasible.
Record tool/version/environment limitations and skipped layers in evidence.

## Setup and verification amendments

- The global environment's SDK versions do not satisfy the lockfile. Verification
  therefore uses a workspace-local environment built with `uv sync --frozen
  --group dev --no-install-project`; it does not replace the user's installation.
  Coverage 7.15.4 (already installed globally) is also installed in that temporary
  environment for the output-buffer gate. No runtime dependencies change.
- Tests use a temporary home, user configuration, saved UI profile and memory
  store, so live Nova sessions cannot interfere with them. The store connection
  is closed before deleting that temporary home on Windows.
- Static banner drawing also needs to handle Textual measuring the widget before
  `on_mount`. A new regression test covers that case, in addition to resize and
  theme changes.
- Full output-buffer statement/branch coverage is a required gate. Coverage of
  every changed line across the boot function and entire existing TUI is not
  claimed; those paths are checked by the mapped behavioral regressions.
