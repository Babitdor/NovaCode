# Browser Mode

`/browser-use` now changes Nova into an internet research mode, using the current
Nova model, agent, conversation and checkpoint thread. It no longer launches a
separate browser-use/Ollama agent or requires the optional Python `browser` extra.

```text
/browser-use                 toggle on/off
/browser-use on              enable without submitting a task
/browser-use Research Rust browser engines
/browser-use status          show current state (--status also works)
/browser-use off             return to normal Nova
```

The composer also has a `browser:on/off` button. Browser Mode changes its border,
badge, colors and pulse. Toggling preserves an unfinished draft. Mode/status
notifications stay out of the transcript. Plan Mode and Browser Mode are mutually
exclusive; changing modes is refused while the TUI has an active request.
`/clear` exits Browser Mode as it starts a new conversation.

## Browser connection

Install [Obscura](https://github.com/h4ckf0r0day/obscura/releases) and register its
native MCP server with the name `obscura`, for example on Windows:

```powershell
nova mcp add obscura --transport stdio --command "C:\Tools\Obscura\obscura.exe" --args mcp
```

Run `/reload-plugins` in an already running Nova to load the connection. The regular
Obscura archive includes rendering for screenshots. Nova does not install a browser
or fall back to Chrome/Chromium. If Obscura is unavailable, search and URL fetching
remain usable, but tasks requiring an actual browser cannot be claimed as complete.
Obscura's own private-network opt-in is needed to browse local development servers;
Browser Mode does not change that setting.

## Capabilities and continuity

Available capabilities include reviewed `obscura_browser_*` actions, web/documentation
search, URL fetching, tool/skill discovery, questions, reasoning and task tracking.
The mode excludes shell execution, repository/file tools, desktop control, other
browser providers, arbitrary MCP dispatch, Code Mode scripts, and delegation.
Both the model's visible schemas and execution are restricted. Previously loaded
schemas and guessed tool names cannot bypass the execution guard. Normal policy,
approval and prompt-injection guards still apply to allowed tools.

The research prompt asks for primary sources, citations, conflicting evidence,
uncertainty, observed browser actions and unfinished work. Findings and browser
tool observations remain in the ordinary conversation/checkpoint history. After
switching off, ask Nova to implement something using those findings. There is no
separate report to import, graph to replace, or model to switch. History remains
subject to Nova's normal compaction and retention behavior.

Legacy `/browser-use --model ...` and `--no-vision` settings are retired: use Nova's
normal model selection, and the tools exposed by the connected Obscura build.

## Verification

Offline checks cover blocked direct calls (including dispatcher/delegation escape
paths), model schema filtering, restricted discovery, concurrent session isolation
through the shared loop, normal-mode restoration, and two turns sharing a real
LangGraph checkpoint with browser observations retained. Textual tests cover wide
and narrow layouts, button clicks, draft/transcript preservation, task submission,
busy mode changes, and direct shell shortcut rejection.

The final focused run passed 205 tests with one platform skip. It includes the
browser tests plus tool search, session fields, Plan Mode lifecycle/guards,
deepagents compatibility, interface styles and Lite Mode. The new source modules
pass Ruff and targeted mypy. Three temporary negative controls (removing execution,
schema and discovery restrictions) were caught by the tests. The tests also prove
that allowed browser actions still pause for approval and respect rejection and
hard policy denials.

A live smoke check used the installed Obscura 0.2.5 against a temporary loopback
fixture: navigation, snapshot, form fill, click, JavaScript result (`Hello Nova`),
and a nonempty PNG screenshot all succeeded. Private-network access was enabled
only for that test subprocess. No saved MCP/model configuration was changed.
The local raw smoke results are ignored at
`reports/browser-mode-obscura-smoke.json`.

No new dependencies. Broad real-site compatibility and live-model research quality
are not established by these checks.
