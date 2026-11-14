# NovaCode 1.2.22

## Code Mode tool display

- Replace truncated `eval(code=...)` rows with compact Code Mode disclosures.
  Completed runs show the recorded operation count and tool names; repeated
  operations are counted together. `MCPCode` uses the same presentation.
- Keep the exact JavaScript and formatted full output in separate,
  keyboard-accessible disclosures. Failed runs show a short error and recovery
  guidance without expanding a dense JSON dump into the transcript.
- Surface failed nested calls even when the script returns normally, and label
  omitted outcome records and truncated selected output. Execution, approval
  policy and the runtime's result envelope are unchanged.

## Remote plan approval and implementation

- Offer plan approval in both the originating Telegram/Discord chat and the
  terminal. The first decision settles the request and retracts the other
  surface; late remote replies cannot approve an already settled plan.
- Use the same approval flow for native plan mode and external planner handoffs.
  Remote approval remains bound to the requesting account, conversation and
  reviewed plan, and retains manual tool approval.
- Preserve the durable session identity and live bridge connections when an
  approved plan starts implementation. The model still receives a fresh thread
  containing the approved plan, without carrying over planning context.
- Read the implementation thread when sending the remote completion reply,
  rather than reading the old planning thread. Subsequent messages continue
  through the existing bridge queue and session topic/thread.

## Long-running graph turns

- Raise the default per-turn LangGraph step limit from 1,000 to 9,999 so longer
  Code Mode tasks are not stopped by the former fixed ceiling.
- Add the startup environment override `NOVA_RECURSION_LIMIT`. Invalid values
  fall back to the default; nonpositive values are clamped to one. Restart Nova
  after changing it. This controls graph steps, not JavaScript recursion.
- Keep the existing repeated-tool and policy-rejection loop guards. A higher
  ceiling does not repair a repeating task or roll back completed side effects.

## Validation

The related remote approval, routing, planning, external-planner and Code Mode
selection passed 133 tests. The final focused selection, including saved remote
session reconnection, passed 70 tests. New component/test lint and whitespace
checks passed, and desktop, narrow-terminal and expanded TUI states were rendered
headlessly. Live Telegram/Discord services were not exercised. No new runtime
dependency is required.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.22
uv tool upgrade novacode-cli
```

Optional extras (retain all extras needed by your installation):

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.22"
```
