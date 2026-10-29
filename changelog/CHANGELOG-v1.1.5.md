# NovaCode 1.1.5

## Lite Mode

- Move built-in compression controls to `/lite-mode`, leaving `/caveman` and
  `/ponytail` available for skills.
- Show only the selected style's settings, explain strengths and options, and
  keep Apply and Cancel visible on small terminals.
- Remember the chosen style when disabled and preserve saved preferences and
  unfinished composer drafts.

## Passing and continuation

- Rename the automatic project handoff mode to `/passing`, leaving `/handoff`
  available for the skill and retaining existing saved records.
- Stop continuation if saving the current conversation fails. Allow interrupted
  handoff deletion to be retried without resurrecting deleted records.
- Distinguish context snapshots from newly written handoff documents in status
  messages and shared events.

## Skill discovery

- Isolate retrieval caches by source and metadata, and track successful skill
  loads in usage rankings.
- Offer relevant candidates for small libraries and without embeddings. Preserve
  applicability and exclusion rules in discovery summaries.
- Use task context for short follow-ups, retain trailing requirements in long
  requests, and allow unloaded skills to be suggested again.

## Tool discovery

- Resolve tool inventories from the owning invocation, including activated skill
  tools, and keep concurrent searches separate.
- Search parameter names and descriptions, filter unrelated semantic results,
  and distinguish tools from same-named subagents.
- Bind discovered schemas using successful, correlated structured records. Older
  text-only discovery reports remain readable and require rediscovery.
- Bound inventory notes and remove full hidden subagent descriptions. Preserve
  normal approval, execution-policy, and plan-mode checks.

## Maintenance

- Standardize plugin management on the singular `/plugin` command.
- Handle update output on legacy Windows terminal encodings.
- Add offline selection, concurrency, checkpoint, UI, and policy regressions.
