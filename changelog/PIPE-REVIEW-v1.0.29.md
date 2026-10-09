# Persistent Remote App pipe review — v1.0.29

## Implementation

- Public `nova --mode pipe` / `nova --headless`, independent of the private tab protocol and the one-shot `-p` interface.
- Shared agent/session-worker execution with a versioned JSONL renderer, request correlation, readiness, task and notification events, FIFO prompts, human approvals, and clean shutdown.
- Protected original stdout descriptor; diagnostics and inherited subprocess writes go to stderr.
- Process-local main-model selection, existing project trust, explicit approval configuration, and existing session continuation after restart.
- Documented wire protocol and dependency-free Node adapter with absolute binary/cwd configuration, shell-free spawning, readiness deadlines, stop, restart, and interrupted-request reporting.

## Findings fixed during review

- A completed worker turn could be omitted from the wait set, preventing queued prompts from advancing. Completed tasks now wake the loop too.
- Scheduling the next inbox read before checking shutdown could accept another public prompt during teardown. Shutdown now stops the read before scheduling it.
- Blocking stdin reads in asyncio's executor could hold Windows shutdown open. The public interface uses a bounded inbox and daemon reader, tested with stdin deliberately left open.
- Startup timers could kill an otherwise healthy persistent session. Readiness cancels the startup deadline; per-request and approval deadlines are separate.
- Reporting stopped before service cleanup could disagree with the final process exit code. CLI bootstrap emits the final stopped record after teardown.
- Approval expiry could be relabeled as an ordinary error. Timeout status now survives the error frame, and interrupt futures always resolve fail-closed.
- Prior-process, duplicate, unknown, and expired approval IDs are rejected. Fixed policy denials survive a human approve response.
- Startup checks could contaminate stdout or lack structured failure output. Dependency and onboarding failures now emit error/stopped JSON, with diagnostics on stderr.
- Invalid UTF-8, oversized frames, deeply nested JSON, reserved frames, and nonfinite JSON input are handled without accidentally executing prompts. Nonfinite tool/provider metadata becomes JSON null.
- New SessionState fields initially fell into the plugin fallback dictionary. Concrete-field registration now has regression coverage.
- The TUI refreshed an idle tab bar four times a second, including DOM walks and rebuilding labels. It now skips timer work with one pane, polls idle tabs once a second, keeps 4 Hz animation only while a session is active, and skips unchanged renders.
- The private tab worker silently discarded queued prompts on cancel/shutdown and had no queue cap. It now emits terminal cancellation for accepted queued requests and rejects excess prompts; the pipe emitter also reuses its writer instead of allocating one per frame.
- Both session interfaces bound queued prompts to sixteen and return a terminal status for rejected/cancelled work, preventing clients from waiting indefinitely on silently dropped requests.
- Duplicate adapter start calls could race while checking cwd. A starting guard prevents that; adapter validation failures clear pending request state.

## Verification

Final focused regression run: **188 passed** across persistent pipe, Node adapter,
one-shot headless, session worker, state registration, model roles, Trello, and
notification tests. Three existing dependency deprecation warnings remain.

The Python suite uses deterministic fake agent streams and actual Windows child-process pipes. The Node adapter tests use real fake-worker subprocesses, including Unicode/space-containing directories, restart, crash, and missing executables. No live model provider, relay, mobile app, or Telegram bot was exercised.

The subsequent scan follow-up changed tab refresh scheduling and private worker
queue completion after that 188-test run. Those follow-up changes passed Ruff F
checks and the existing four-tab benchmark; the Python regression suite was not
rerun after the follow-up edits.

Ruff F checks pass for the headless modules, worker, and new tests. Node syntax checks and git diff whitespace checks pass. Existing main.py dependency-probe unused-import warnings remain.

## Limits

- Pairing persistence, relay/mobile reconnection, authentication, and topic authorization belong to the Remote App; its source is not part of this implementation.
- Duplicate IDs are remembered only within the running process (256 IDs). Crashes cannot guarantee exactly-once tool execution. The adapter reports interrupted requests instead of replaying them.
- Workspace approval and automatic tool approval remain separate explicit choices. Image attachments are not yet accepted by the public prompt protocol.
- Deadlines are cooperative. The bridge must drain output and enforce its own process/tree deadline if a native call or output backpressure stalls Nova.
- Session saves are bounded and best effort. Public pipe mode attempts saving before done; one-shot -p keeps its existing result-before-save behavior.
