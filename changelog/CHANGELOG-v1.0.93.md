# NovaCode 1.0.93

## Commit and pull request attribution

- Enable commit and PR attribution by default, with independent `attribution.commits` and `attribution.pullRequests` opt-out switches in `~/.nova/Nova.config.json`.
- Give the main agent and Nova-managed subagents live attribution instructions, and normalize supported structured Git/GitHub creation-tool messages.
- Add a standard NovaCode co-author trailer while preserving human authors and existing trailers. Attribution is commit-level, using an unlinked product identity.
- Display the supplied NovaCode logo in PR attribution footers. Git commit trailers remain plain text; arbitrary shell commands follow agent instructions rather than being rewritten.
- Test persisted opt-outs, schema handling, duplicate prevention, real isolated Git commits, synchronous/asynchronous agent execution, and subagent integration. No new dependencies.

Configuration and scope: `docs/attribution.md`.
