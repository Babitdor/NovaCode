# NovaCode v1.0.30

## Added

- Clicking a task in the subagents panel opens a full page laid out like the main transcript: the task as a prompt block, the agent's name over rendered Markdown, and each run of tool calls as one block with a theme-coloured bullet per call and its result on the same line. Results are matched to their own call, so parallel calls that finish out of order still read correctly.
- A Telegram topic that connects while a turn is running joins that turn, starting from the response so far, instead of waiting for the next prompt.

- Nova's own context compaction for the main agent and every subagent: prune old tool results, trim old tool-call arguments, then summarize with the `/compact` engine. Your messages are kept verbatim, a failed summary no longer fails the turn, and the summary call no longer streams into the transcript.
- Nova works within a 128K-token context budget by default. Set `context_budget_tokens` to change it (`0` uses the model's whole window). `/context`, auto-compact, and compaction all use the same figure.

- Learning reviews once when a task ends, with the real outcome. Lessons are stored per project and reach the shared pool only when a second project confirms them; a failed task shares nothing.
- Pitfalls are stored as symptom, cause, and fix, and recalled when failing output matches them by meaning, using a local embedding model (word overlap as fallback). Built-in environment lessons are recalled only at a failure.

- Ollama models run through Ollama's OpenAI-compatible endpoint. Thinking off maps to `reasoning_effort=none`, and a local model's context window is read from its Modelfile `num_ctx`, then `OLLAMA_CONTEXT_LENGTH`.
- `--help` shows the NovaCode name and version.
- The evaluation harness runs Terminal-Bench through Harbor on local Docker or Modal, in a hermetic home that never reads or writes `~/.nova`, with `--ak` knobs for `reasoning_effort`, `max_tokens`, `learning=off|on|frozen`, and `context_budget`.

## Fixed

- The loop guard's hard stop now ends the turn; previously a model could keep repeating a command through it. A repeat is flagged on the second identical result, near-identical calls with the same result count as one, and a hard stop left by an interrupted turn no longer ends the next one.
- A reply cut off by the output limit is no longer accepted as the end of a turn; the agent is nudged to continue, up to three times in a row.

- Sandbox file tools work on images without `python3`: read, list, and glob fall back to shell, and write/edit say what is missing. A populated directory is no longer reported as empty.
- Sandbox installers no longer hang on interactive prompts, and files written to `/tmp`, `/dev`, `/proc`, or `/sys` land where the shell sees them. Sandbox provider lifecycle and timeout handling are repaired.

- `rm -rf` below the top level is allowed again (for example `rm -rf /tmp/build`). Root, home, and top-level directories stay blocked in any argument position, quoted or not, including with the long `--recursive` / `--force` options.
- The dangerous-command pattern for `halt` / `shutdown` no longer matches those words inside other commands.
- A test run's failure summary is treated as a failure.

- `/context` rows no longer stay at 0 when one tool has no JSON schema, and the embedding cache no longer raises `KeyError` after it fills.
- The status bar updates CTX on every model call, and the cache hit rate can no longer exceed 100%.
- The TUI stays responsive on long, tool-heavy turns: large tool arguments are compared in milliseconds rather than seconds, saved tool results are checked on disk once rather than on every model call, and compaction's planning pass runs off the UI loop.
- Compaction recovers from a too-long summary request, keeps injected notes out of your quoted messages, and budgets a subagent against its own model.
- Improved async concurrency and runtime efficiency.

## Limits

- The 128K default budget's effect on task success has not been measured. Set `context_budget_tokens` to `0` to return to the whole window.
- In the subagent page the tool block is always expanded. A remote (async) agent's page updates step by step rather than token by token.
- Recall by meaning needs the local embedding model; without it, matching falls back to word overlap.
