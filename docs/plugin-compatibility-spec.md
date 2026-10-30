# Plugin compatibility contract

Reference: https://code.claude.com/docs/en/plugins/overview and its manifest,
marketplace, loading and installation references, consulted 2026-10-28.

This is an autonomous implementation. Spec approval was not obtained; acceptance
criteria remain reviewable here. Nova uses its own `.nova` settings and storage;
it does not modify or automatically trust another host's installed plugins.

## Acceptance criteria

1. A standard directory plugin, optionally declaring `.claude-plugin/plugin.json`,
   installs as a unit. Invalid JSON, component paths escaping the root, symlinks
   escaping the bundle and unsafe storage names fail without replacing an existing
   installation. Concurrent operations do not share staging directories or lose
   registry updates. Installation never executes plugin code.
2. Marketplaces use `.claude-plugin/marketplace.json`; plugin identity includes
   `name@marketplace`. Local relative sources stay inside the marketplace. GitHub
   and Git sources retain their ref and subdirectory. Unknown source types report
   an error instead of being interpreted as another source type.
3. Install, uninstall, enable, disable, update, validate, list and marketplace
   commands are available through `nova plugin` and `/plugin` (`/plugins` remains
   an alias). User scope is default; project and local scopes record settings in
   `.nova/settings.json` and `.nova/settings.local.json`. Local overrides project,
   which overrides user. A settings entry alone never downloads code.
4. Immutable cached snapshots retain source/version metadata. Updates leave the
   prior snapshot intact; failed fetches/validation preserve the usable install.
   Existing `installed.json` installations remain discoverable without deleting
   them or treating uninstalled files as installed.
5. Commands, skills and agents use `plugin:component` names. Standard directories
   and manifest component paths are honored, including inline commands, MCP and
   hooks. `${CLAUDE_PLUGIN_ROOT}` resolves to the installed bundle. Full skill
   content stays on demand; descriptions are discoverable by the agent.
6. MCP servers and hooks are derived from enabled plugins at runtime, not copied
   into owner configuration. Disable/uninstall removes all components on reload.
   Legacy merged hooks/MCP entries are suppressed when their owning legacy plugin
   is disabled, without deleting unrelated owner entries.
7. Command hooks receive Claude-shaped event JSON, preserve matchers/timeouts,
   return additional context, and can deny tool execution. They cannot approve a
   denied operation or broaden Nova permissions. Hook subprocesses have bounded
   lifetime/output; native Nova observer hooks retain their existing behavior.
8. The TUI has Installed, Discover and Marketplaces views. Operations run off the
   UI loop; status/errors use temporary notifications. Reload refreshes skills,
   commands, agents, hooks and MCP without stale autocomplete registrations.
9. Claude host APIs (JavaScript mods, LSP integration, channels, workflows, desktop
   bundles and organization/account sync) are not emulated. Declared unsupported
   executable components produce explicit compatibility diagnostics. No claim
   of complete Claude runtime compatibility is made.

## Setup and verification

Use existing Python, pytest, Hypothesis, Ruff, mypy and coverage tooling. No new
runtime dependency, global install, release or push is planned. Preserve unrelated
workspace changes. Add compatibility tests in `tests/test_plugin_compatibility.py`,
an opt-in/local verification entry point in `scripts/verify_plugin_compatibility.py`,
and evidence in `docs/plugin-compatibility-evidence.md`. Existing entry-point
middleware plugins remain supported through `/middleware`.

Verify installation rollback, scope precedence, namespaces, actual local plugin
execution, missing executables, matcher filtering, hook denials, config ownership,
legacy migration, concurrent writes and TUI navigation. Record red tests, focused
regressions, lint/types, coverage, manual negative controls and unverified layers
in the evidence document. Network tests use fake Git fetches; real execution uses
a local synthetic bundle with harmless commands.

## Final owner steering and regression additions

The owner subsequently authorized pushing code/tests to main, excluding docs,
PRIME.md and FEATURES.md, then updating FEATURES.md locally after the push.
No version bump or dependency change is included in this plugin task.
Snapshot identities must preserve filename/content boundaries, permissions and
link targets; a successful shell command must print its result, and failures
must print diagnostic text to stderr. The shared TUI service stays print-free.
