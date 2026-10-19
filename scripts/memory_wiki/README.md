# Linked agent memory and `/dream`

Nova's memory wiki lives inside the active agent's existing directory:

```text
~/.nova/agents/<agent-id>/
  agent.md                 Stable user instructions
  HABITS.md                Stable checklist
  memories/
    INDEX.md               Compact wiki home
    wiki-*.md              Generated topic maps
    <topic>.md             Facts, evidence, decisions and related links
    projects/<key>/*.md    Scoped project lessons
    .history/*.txt         Full text retained before overflow compaction
  history/wiki-backups/    Exact pre-maintenance sources and hash manifests
```

A first installation starts with the normal empty agent-memory template and memory folder. It does not include the developer's personal wiki or preferences. Review-generated lessons accumulate as topic files; the first `/dream` builds the linked wiki around them. Wiki structure is initialized on demand, not at installation.

Ask the agent to recall a subject. It starts with `memory_search` or the wiki
home, reads relevant pages, follows related links, and verifies scope and dates.
It should not load every memory page into context.

Run `/dream` to organize and consolidate memory. It backs up the source and
rebuilds the wiki's navigation before asking the configured agent to consolidate
overlapping notes. The prompt requires preservation of unique facts, user
corrections, provenance and unresolved work. The agent then calls
`memory_wiki(action="maintain")` to refresh maps and links, and
`memory_wiki(action="check")` to report unresolved local links. These tools can be
discovered with `search_tools` and are bound to the active agent directory.

The structural pass does not infer which conflicting statement is true, merge
unrelated projects, delete unique facts, or claim improved model recall.
Filename-based related links are suggestions; project scope still needs checking.
Semantic consolidation runs through the normal configured agent and permissions.
Memory maintenance is blocked in plan mode and in Cowork's workspace-only broker.

The wiki home stays below the default 2,000-character index injection ceiling.
Root instructions should fit their configured block ceiling (default 6,000).
Wiki checklists carry `<!-- nova-memory-wiki:checklist -->`; background habit
reviews then go to `habits-lessons.md` rather than displacing the checklist.

Backups are intentionally retained. No automatic retention/deletion policy is
introduced. Each structural file replacement is atomic and checks the captured
source, but the full pass is not a filesystem transaction. A non-cooperating
writer can race between the final check and replacement. On a detected change,
stop and reconcile; on interruption, retain backups and rerun maintenance.
Backups can restore bytes exactly, but restoration should reconcile later edits.

Verification, using the existing environment:

```powershell
.venv/Scripts/python.exe scripts/verify_memory_wiki.py
.venv/Scripts/python.exe scripts/verify_memory_wiki.py --coverage
.venv/Scripts/python.exe scripts/verify_memory_wiki.py --mutations
.venv/Scripts/python.exe scripts/verify_memory_wiki.py --full
```

Mutation checks load throwaway module copies in separate processes; the shared
source tree is never modified. Tests use temporary user profiles; full-suite optional
integration/model dependencies may still be unavailable offline. No dependencies
are installed by the runner.

See [acceptance criteria](SPEC.md) and [verification evidence](EVIDENCE.md).
