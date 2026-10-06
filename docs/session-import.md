# External session import

Use `/sessions` to choose Nova saved sessions or the external Claude/Codex
browser. Search the external list, select **View** to inspect it, or **Import**
to load compact context into the current Nova conversation. Viewing changes
neither the source transcript nor the Nova graph.

```
/import codex --last
/import claude <session-id>
/import claude "C:\exports\conversation.md"
/import nova <session-id>
/context imported
/context imported full
/context imported compact
/context imported relevant authentication timeout
/compare claude:<session-id> codex:<session-id>
```

CLI startup supports `nova --import claude --latest` and
`nova --import codex --import-session <id-or-file> --import-mode compact`.
Import is separate from resuming the source agent. Nova uses its own current
model, tools and workspace. Existing Nova conversation messages are retained
when importing in the TUI. A new import replaces the previous imported reference.

Claude discovery reads `~/.claude/projects` (or `CLAUDE_CONFIG_DIR/projects`).
Codex discovery reads `~/.codex/sessions` (or `CODEX_HOME/sessions`). Recent
discovery is bounded to 200 files per provider; older sessions can be imported
by explicit path. Source files over 50 MiB are rejected. Incomplete final live
JSONL records are skipped; malformed earlier records are reported. Image and
private reasoning blocks are excluded. Unknown text export layouts are kept
as a single historical document instead of inventing message roles.

Claude's supported `/export` path and local resumable sessions are described
in the [Claude Code FAQ](https://support.claude.com/en/articles/14554922-claude-code-user-faq).
OpenAI documents the distinction between saved chat history and the current
filesystem in [Projects and chats](https://learn.chatgpt.com/docs/projects).
Native JSONL schemas can change; explicit supported text exports provide an
additional Claude ingestion route.

Imported Context reports provider, project, messages, tool records and estimated
raw/loaded sizes. These sizes use a UTF-8 heuristic, not the provider tokenizer.
The import budget leaves space for Nova instructions, ongoing history and a
response. Full mode refuses oversized histories. Compact mode extracts evidence
snippets for decisions, attempts, unresolved problems, files, commands and test
results; relevant mode selects messages by query terms and recency. These are
historical excerpts, not assertions that earlier changes still exist.

The complete normalized text transcript is atomically saved as
`imported-context.json` alongside the Nova session. Mode changes replace a single
reference message and preserve the raw transcript. Resuming Nova retains that
reference and allows further mode changes; `/clear` starts a new session with
no imported context. Historical tool calls are plain text, never live calls.
Comparison sends a bounded ordinary request to Nova to compare both histories.

## Adapter plugins

The `SessionAdapter` protocol implements `provider`, `discover()` and
`load(session_id)`. Built-ins live in `session/adapters/claude_code.py`,
`codex.py` and `harness_native.py`. Installed Python packages can register
factories under the `novacode.session_adapters` entry-point group:

```toml
[project.entry-points."novacode.session_adapters"]
my_agent = "my_package.adapter:MyAgentAdapter"
```

Each command builds a fresh registry. A broken plugin is isolated and cannot
replace a built-in provider. Plugins are executable installed code; source
transcripts cannot register or execute adapters. Live watching and automatic
adapter generation are future extensions rather than parts of this import path.

Validation: run `python scripts/verify_ui_recovery.py --test tests/test_session_import.py`.
Tests exercise native records/exports, role normalization, tool preservation,
deduplication, incomplete tails, malformed records, latest/prefix selection,
Windows paths, budgets, durable mode switching, and a real Textual read-only
browser preview. No source-agent API call or credentials are required.

The combined UI, recovery, subagent and import suite passed 136 tests. The six
adapter/context/browser modules pass limited mypy checking and new modules/tests
pass Ruff. Test isolation uses a per-test memory store for mock-stream leases,
disables native audio warmup/update checks, and preloads normal agent bootstrap
before timed tests. The full repository suite was not run.
