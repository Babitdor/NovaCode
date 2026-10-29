# Skill selection, 2026-10-29

Nova's existing refreshing skills middleware now owns one shared policy for main
agents, continuations and subagents. Selection considers task intent, deliverable,
scope, exclusions and phase. It prefers the smallest useful set, searches unclear
matches, loads instructions before applying them, reuses bodies still in context,
and reloads after compaction. Suggestions alone do not activate instructions.
Skills cannot extend user authorization or override higher-priority instructions.

Removed the continuation template's conflicting "ambiguous → skip" rule.
Automatic pinning now requires a clear command or invocation prefix; negations,
mentions and fenced examples no longer automatically inject skill bodies.
Ambiguous natural-language requests remain available for model-driven selection.
Explicit pins from the skill picker retain their existing behavior.

## Verification

The new policy/activation regressions initially failed (11 failures, 3 passes).
After implementation, 249 focused offline tests passed across skill discovery,
retrieval, resources, controls, actual graph tool plumbing, prompt contracts,
deepagents compatibility, Caveman and credential/auth refresh.

The opt-in live probe used the saved CommandCode DeepSeek V4 Flash model without
fallback, temperature 0 and a 4,096-token completion limit. All eight synthetic
cases passed exact loaded-skill-set checks; applicable cases also included the
marker from the loaded body in their final answer:

- Website building versus read-only website review.
- Textual terminal work versus website work.
- Explicit named skill use.
- Negation, trivial arithmetic and discussion without activation.
- Two complementary workflows when both deliverables were requested.

The probe uses actual RefreshingSkillsMiddleware and graph tool execution against
a temporary synthetic library. Only skill tools are exposed. BM25 retrieval avoids
embedding downloads; it does not measure the production dense ranking or a large
real library. Eight cases establish regression evidence, not equivalence to Codex
or guaranteed decisions on arbitrary tasks. No dependency or model router added.

Reproduce (makes live provider calls):

```powershell
.venv/Scripts/python.exe -m scripts.benchmark_skill_usage
```

Raw synthetic trajectories are local and ignored at
`reports/skill-usage-deepseek.json`. Offline policy tests:

```powershell
.venv/Scripts/python.exe -m pytest -q tests/test_skill_usage_policy.py
```

New tests/probes and the modified runtime helper pass Ruff. Existing middleware
lint/format debt and the unrelated full-suite baseline issue recorded in
`core-runtime-fixes-evidence.md` remain outside this change. No full-suite pass is
claimed. Auth refresh and token measurements are documented in
`docs/caveman-mode.md`.
