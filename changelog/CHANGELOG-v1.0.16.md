# NovaCode 1.0.16 — Progressive skill loading

- Pin enabled skills using `$skill-name`, `/skill-name`, or the new **Pin to prompt** control in `/skills`. Load each skill as a structured instruction snapshot, without preloading its supporting files. Earlier snapshots remain unchanged after edits.
- Add `skills_load` and `skills_read_resource` tools for explicit activation and bounded, on-demand reference reads. Relative resource paths resolve against the skill directory and reject traversal.
- Activate optional tool schemas declared in `metadata.include_tools`. Dedicated skill tools are gated until their skill is loaded; removing activation messages during compaction removes that access. Existing independently registered tools remain searchable.
- Add a **Reload** control and per-run `pinned_skills` / `skills_metadata=None` support. Keep the full skill library outside session checkpoints and run async discovery and pin reads off the UI event loop.
- Use consistent user → shared → Claude → plugin → project precedence across discovery, invocation, previews, and the agent. Preserve nested YAML metadata and multiline frontmatter during skill normalization.
- Keep `/skills` action buttons usable on narrow terminals and show compact pinned-skill labels when restoring sessions.
