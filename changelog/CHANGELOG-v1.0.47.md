# NovaCode v1.0.47

## Improved

- Explicitly approved plans are saved in the project's `.nova/plans` folder and
  mirrored to the global archive. Automatically approved plans stay global.
  `recent_plans` lets the agent find and read both stores for the current project.
- `/dream` maintains a linked memory wiki with compact indexes, topic maps,
  related pages, backups and broken-link checks. It recognizes habits-only and
  project-scoped notes, preserves stable habit checklists and defers writes in
  plan mode. Memory overflow retains recoverable originals.
- Main, planning, memory and review prompts now agree on delegation, bounded
  waiting, instruction scope, plan locations and wiki navigation. Responses can
  follow the requested format without a fixed four-line limit. Desktop sleep
  requires a user request or applicable standing instruction.

## Validation and limits

- Release checks passed: 135 memory/wiki tests and 142 prompt, planning, approval,
  async-task and agent integration tests. The memory engine/tool retained 99%
  coverage; five focused runtime files passed mypy. Detailed evidence is in `scripts/PROMPT_AUDIT.md` and
  `scripts/memory_wiki/EVIDENCE.md`.
- The memory-wiki engine/tool previously achieved 99% branch-aware coverage;
  three isolated manual mutants were rejected. These checks establish structural
  contracts, not live-model reasoning or consolidation quality.
- The full suite is not claimed passing: a background handoff timeout was also
  reproduced on the baseline. Existing typing errors remain documented.
- No new dependencies, model changes or paid provider calls. Personal memories,
  credentials and prompt overrides are not included in this release.
