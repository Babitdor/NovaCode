# NovaCode 1.2.17

## Update and terminal reliability

- Show immediate feedback while preparing an update. Reject duplicate requests,
  reset stale install buttons after failed checks, and start the update worker
  after the dialog closes.
- Keep Nova open if session saving fails or a running turn has not stopped.
  Preserve installation-lock checks for other terminal windows and the existing
  same-terminal update/resume handoff.
- Capture startup output in the worker thread that produces it, preventing
  Rich output from painting over the TUI during model switches, MCP/plugin
  reloads, initialization, resume, and auxiliary model setup.
- Press Left Arrow in an empty composer to open Command Center. With text in
  the composer, Left Arrow continues to move the cursor.

## Images and compression

- Determine image support from each explicit subagent model, and provide the
  VISION tool to text-only subagents. Recognize standard URL image blocks,
  preserve message metadata when stripping images, and extract only caption
  text from structured vision responses.
- Retry vision initialization after a short cooldown instead of disabling it
  for the rest of the session after one failure.
- Harden Ponytail and Caveman activation/deactivation, command-reference
  identity, argument copying, fenced-code preservation and multimodal routing.
  Compression modes retain full verification and correctness requirements.

## Coding guidance

- Improve shared coding, planning, delegation, agent-generation, refinement
  and verification prompts: state consequential assumptions, keep changes
  focused, justify added complexity and verify observable outcomes.
- Adapt guidance from
  [Karpathy-inspired coding guidelines](https://github.com/multica-ai/andrej-karpathy-skills)
  to Nova's tools and authorization model. The verifier no longer accepts a
  plausible completion claim in place of required evidence.

## Validation and limits

Regression coverage includes real Textual button/key events, compiled subagent
provider payloads with a mocked HTTP transport, update/save failures, duplicate
requests, prompt rendering and verifier contracts. Live-provider image accuracy
and improvements in coding quality have not been measured. No new runtime
dependency is required.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.17
uv tool upgrade novacode-cli
```

Optional extras remain available, for example:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.17"
```

Exit Nova sessions sharing the installation in other terminal windows before
updating. The terminal windows themselves can stay open. In Nova, use `/update`;
if running an older version with an unresponsive button, exit Nova and run
`nova update` in the same terminal, then resume with `nova --continue` if needed.
