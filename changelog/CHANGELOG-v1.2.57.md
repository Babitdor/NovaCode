# NovaCode 1.2.57

## Tool audit: fewer, better-documented tools

- Removed dead and redundant tools. Deleted outright: `http_request` (merged into
  `fetch_url` long ago), the Twitter scraper pair (registered nowhere), the unregistered
  `create_memory_structure` scaffolding tool, and the `oracle` agent tool (unadvertised,
  untested, and redundant with Oracle mode and Council). `docs_search` is gone as a
  separate tool — it was `duckduckgo_search` plus a static `site:` filter table; the
  model gets the same results with one `site:` query, and the plan/research prompts now
  say so. 105/105 port-served command surface is unchanged.
- Kept after verification: `shell`/`bash`/`execute` are NOT redundant — `shell` is the
  native shell, `bash` is the real bash binary (model-reflex compatibility), `execute`
  is deepagents' backend tool with an explicit dialect parameter. The `shell`
  description on Linux/macOS now states it runs `/bin/sh` (dash on Debian/Ubuntu) and
  that `bash` is the tool for bash-specific syntax — they stopped reading as aliases.
- A new contract test (`tests/test_package_imports.py`) walks the AST of every module —
  deferred, function-level imports included — and fails when a first-party import no
  longer resolves. It caught two real breakages on its first run: a stale import of the
  removed `_DOCS_SITES` table (now moved into `prime_factory/research.py`, its only
  consumer) and a remote `evolution` command importing a module that no longer exists
  behind an always-firing `except ImportError` fallback.

## /export asks what to export

- `/export` now opens a dialog: Markdown or JSON, and whether to include thinking,
  tools, both, or neither. Cancelling writes nothing; a destination argument still
  wins and gets the chosen format's extension. The builders render the conversation
  along the current path (the HTML tree export stays the whole-branch view), redact
  secrets, recover tool-result names from their calls, and cap oversized tool output.
  Over the remote port the same questions arrive as a form screen; headless runs keep
  the HTML default.

## Knowledge cache fixes

- The `/cache` screen's "Cache generation" row actually displayed the epoch — the count
  of clears, not generations. It is now labelled "Clears (epoch)".
- A dead feedback loop was starving the cache: a lookup miss returned generic guidance
  that never said `paths` is what builds a reusable overview, so `generation_calls`
  stayed 0 forever. Misses now return explicit guidance to re-call with 2-8 key files.

## Memory and verification hardening (follow-ups)

- Associative pre-turn recall no longer mistakes gate nudges ("Internal context: …")
  for the user's query — it recalls the last real user message — and the recall cache
  invalidates when memory is written mid-turn.
- The post-edit verification detector recognises more shell forms: custom check
  scripts by name (`scratch_test.py`, `check_env.sh`, `verify_install.py`), file-writing
  commands (redirection, `sed -i.bak`, `tee`, `cp`/`mv`, `patch`, `git apply`,
  PowerShell write cmdlets), while ignoring `/dev/null`, stream dups and comparison
  operators.

## TUI composer footer

- The live-chat composer gained a compact two-line footer in the coding-CLI style:
  model · context used/window · percentages · workspace · branch · session · version,
  then the permission mode with a `shift+tab` cycle (normal → accept edits → plan
  mode → normal) and `? for shortcuts`. The labelled info-bar columns remain on the
  home layouts; tiny/short terminals keep the lean chrome.
