# NovaCode 1.2.25

## Code Mode discovery and recovery

- Add `tools.search({query, limit})` over the current invocation's callable native
  and MCP tools, using existing ranking and discovery restrictions. Results name
  exact invocation targets; existing list, describe and call APIs remain available.
- Describe verified core filesystem and shell results, explicitly identify unknown
  contracts, and preserve MCP servers' declared `outputSchema`. Tool return values
  are unchanged; command output must not be assumed to have a `.stdout` property.
- Save complete oversized selected results as UTF-8 JSON in the existing `/cleared/`
  store before truncating the preview. Atomic writes, existing workspace boundaries
  and seven-day retention apply. Failed saves are reported without a recovery path.
- Prefer focused discovery, temporary async-function locals and compact selected
  results. Parallel execution is for independent operations. Inspect recorded
  outcomes before retrying; completed side effects are never automatically replayed.

## One advertised command tool

- Advertise `execute` with an explicit `shell: "native"` or `shell: "bash"` choice.
  Native uses PowerShell on Windows and the platform shell elsewhere. Explicit
  selections never automatically retry a different shell dialect.
- Keep `shell`, `bash` and selector-free `execute` calls compatible. Old Windows
  `execute` calls retain their platform-default `cmd.exe` semantics. Subagents
  without Nova's shell middleware retain their backend executor.
- Preserve permissions for both `execute` and the selected shell alias; the stricter
  decision wins. Approval details show the shell, timeout and execution options.
  Existing permissions are not migrated or relaxed.
- Reuse existing interactive, background and cancellation paths. Local timeout
  overrides can shorten the configured foreground deadline without changing
  concurrent calls. Sandbox commands stay on their backend and retain backend
  timeout validation. Unsupported backend timeout/interactive combinations fail
  before execution.

## Compact tool display

- Replace Code Mode outlines with borderless status rows, theme-aware status dots,
  three-line selected-result previews and an omitted-line count.
- Keep exact JavaScript and full output behind keyboard-accessible disclosures.
  Failures and recovery paths stay visible without opening the raw envelope.
- Parse Code Mode envelopes before the generic 6,000-character error-preview cap,
  preserving recorded tool names, complete selected output and offload paths.

## Response modes and benchmark evidence

- Preserve exact code-only and JSON-only output formats in Ponytail mode rather
  than adding an optional prose footer.
- Include `eval` and `MCPCode` in existing execution-block capture and output
  compression. Recovering a recorded script does not execute it and still requires
  current authorization before any later execution.
- Extend offline Caveman/Ponytail benchmark checks and preserve raw Code Mode
  measurements. In 36 final trials, improved Code Mode passed 12/12 tasks, versus
  10/12 each for original Code Mode and direct tools. Token changes were mixed;
  two repeats per task provide directional evidence, not a universal cost guarantee.
  The report separately accounts for 24 excluded preliminary trials and their usage.

## Validation

Focused execution, permissions, cancellation, timeout, sandbox, discovery and Code
Mode regressions passed, including 21 execution-surface checks and 257 surrounding
checks. Tool display changes were checked at wide and narrow terminal sizes, with
keyboard access and theme changes. New regressions are registered with platform
CI. No new runtime dependency is required.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.25
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Retain optional extras used by your installation:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.25"
```
