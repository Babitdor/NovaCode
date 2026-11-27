# v1.2.89 — Memory wiki accumulation fix + deepagents 0.7.42

**Released 2026-11-27** · PyPI `novacode-cli==1.2.89` · tag `v1.2.89`

## Memory: project pages can no longer strand outside the wiki

Investigating why `/dream` stopped accumulating the wiki revealed the root
cause: pages written flat as `memories/projects/<topic>.md` were invisible to
every consumer. The wiki builder, `/dream`'s topic scan and `memory_search` all
read the scoped layout `projects/<project-key>/<topic>.md`, and the dream
agent's backend refused to even read the flat pages — so real project memory
sat stranded while `wiki-projects.md` stayed empty.

- **Wiki accumulates legacy flat pages.** `maintain_wiki`, `validate_wiki` and
  `/dream` now include `projects/<topic>.md`, so stranded pages get INDEX
  entries, topic-map links, navigation and backlinks, and the dreamer can
  finally consolidate them.
- **Dream backend reads flat pages.** The dream agent's scope guard now allows
  single-part (flat) pages under `projects/` while other projects' scoped
  directories stay hidden — their names included.
- **The writer refuses flat pages.** `write_memory` and the main agent's
  `/memories/` backend (`AgentMemoryBackend`) refuse writes and edits that
  would create a flat `projects/<topic>.md` page, and name the correct scoped
  target instead. Reads and deletes of flat legacy pages stay open.
- New tests cover the wiki accumulation, the dream scope boundary, and both
  write guards.

## Dependencies

- `deepagents` pinned to **0.7.42** (from `>=0.7.0,<0.8` resolving 0.7.10).
  Nova's three monkey-patch contracts (`validate_path`,
  `perform_string_replacement`, `create_sub_agent`) verified unchanged via
  `tests/test_deepagents_compat.py`.
