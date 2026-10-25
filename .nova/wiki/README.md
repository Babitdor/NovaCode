# Agent Wiki Configuration

This wiki is the agent's persistent knowledge base. The `wiki/` tree is an
**Obsidian vault** — open `.nova/wiki/wiki/` in Obsidian to browse the graph.

## Flow

    Web  →  Web Clipper → Clippings/  →  Agent (/ingest)  →  Obsidian wiki (wiki/)

- `Clippings/` — **Obsidian Web Clipper inbox** (the clipper's default folder).
  Point the clipper here; whatever you save lands in this folder and `/ingest`
  picks it up.
- `raw/` — legacy / categorized sources (`articles/`, `docs/`, …); also scanned.
- `wiki/` — synthesized, interlinked Obsidian pages
  - `technologies/`, `frameworks/`, `patterns/`, `projects/`, `comparisons/`
- `index.md` — machine-parsed topic index (markdown links)
- `log.md` — ingestion log
- `README.md` — this file

## Page conventions (Obsidian-native)

Every synthesized page has:
- **YAML frontmatter**: `title`, `aliases`, `tags`, `source`, `created`
- **`[[wikilinks]]`** to related pages (by filename-without-extension) — these
  build the Obsidian graph and backlinks
- Headings, a short summary, and scannable bullets

## Commands

- `/ingest [<path>]` — ingest a raw source into synthesized pages (no arg lists
  available raw sources; a bare filename is found anywhere under `raw/`)
- `/ask <question>` — answer with relevant wiki pages prepended as context
- `/file <topic>` — file recent conversation knowledge as a wiki page
