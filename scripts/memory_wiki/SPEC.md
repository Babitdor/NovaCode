# Memory wiki maintenance specification

This autonomous implementation has no separate human spec approval. The user authorized fixing memory inconsistencies, creating a linked memory wiki, and maintaining/consolidating it through `/dream`.

## Acceptance criteria

- `/dream` recognizes agent.md, HABITS.md, and topic memory, including habits-only stores; it always targets `/memories/memories/INDEX.md`, never legacy MEMORY.md.
- Maintenance produces a home index under 2,000 characters, domain maps covering every current topic, and topic navigation linking home, a map, and related pages where meaningful.
- Existing topic bodies and user preferences survive structural maintenance. Existing user-authored pages colliding with managed names fail safely. Missing links are reported, not silently deleted or invented.
- A second identical pass makes no writes or additional backup. Root instructions and project-scoped memories are not mixed with unrelated projects.
- Every overwritten source has an exact recoverable backup. Writes use atomic replacement and check for changes since the snapshot. Symlinked/outside paths are rejected. Partial/cancelled passes retain original backups and are repairable on rerun.
- The consolidation prompt requires reading full source, preserving distinct facts, provenance and corrections, keeping history separate, repairing inbound links before retiring pages, and checking the wiki afterward.
- An agent-facing maintenance tool can rebuild/validate the active agent's wiki; planning mode blocks its write operation.
- Future habit reviews retain the stable checklist by filing incident lessons in topic memory once wiki mode is enabled. Overflow compaction preserves full original text in a recoverable archive.
- Fix the audited personal rules with exact backups: one midnight–06:00 sleep rule; memory-write warning early; correct plan locations; named habits checklist; scoped project history; current tool-discovery and budget guidance.

## Failure model and setup

Protect against overwritten personal content, stale concurrent writes, path escape, partial writes, false relatedness, unbounded injected indexes, broken links, repeated changes and loss during compaction. Use temporary profiles and synthetic fixtures, standard library plus installed test tools. No new dependencies, paid calls, publishing, commits, or unrelated cleanup. Preserve the existing uncommitted work. Verify using focused regressions, types/lint/format, mutation checks and the full suite; report unavailable layers and baseline failures explicitly. Filesystem checks cannot eliminate the final race with non-cooperating external writers; backups and atomic replacement limit damage but do not provide a multi-file transaction.
