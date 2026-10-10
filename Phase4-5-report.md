# Phases 4–5: safe computation reuse and request diagnostics

All changes remain local. `Optimization.md` is preserved. No paid model requests
were dispatched and no coding-quality or paid-latency improvement is claimed.

## Implemented

- A synchronized process-local JSON cache returns defensive copies. Each
  namespace has a 256-entry / 8 MiB serialized-payload limit; all namespaces
  share 32 MiB. Oversized or lossy JSON values bypass reuse. Shutdown clears it.
- Named subagents reuse only parsed definitions keyed by current content. Every
  build discovers files and binds current tools, model roles and permissions.
- Skill listings validate current source bytes, including equal-size edits with
  restored timestamps. New parsed listings stay in memory; legacy disk listings
  are ignored. Project skill roots are discovered on each call.
- Baseline token counts persist only a versioned digest and nonnegative integer.
  Keys include the complete prompt, provider/model configuration, tokenizer,
  relevant SDK versions and native counting method. Legacy, custom and ambiguous
  identities bypass reuse; fallback counts are not persisted. Replacement is
  atomic with unique temporary files.
- Opt-in `NOVA_LOCAL_METRICS=1` stderr diagnostics include cache outcomes,
  retained payload, validation/computation time, request purpose, correlation,
  tokens and latency. They contain no prompts, source, tool payloads or credentials.
  SDK runs and observable HTTP attempts are distinct; unobservable attempts and
  unverified cost estimates are null, with explicit unknown pricing.
- Summary generation checks the checkpoint/messages again before committing.
  Superseded, failed and cancelled summaries preserve original history. This is
  a validation guard; it is not a transactional compare-and-swap across writers.
- Router, summary and classifier calls are labeled at actual dispatch sites.
  No auxiliary request was proven redundant, so none was removed. No response
  cache was added. Prompt order, schemas, approvals and pipe contracts remain.
- Relevant-section selection exists only in the offline benchmark's opt-in
  `--context-sections` experiment, with path/line provenance and expandable reads.
  Production context selection and explicit full reads retain their behavior.

## Offline measurements

`phase45-measurements.json` records ten repetitions for enabled/disabled caching,
CPU time, event-loop delay, median/p95 latency, cache counters/payload and profiles.
The disabled candidate establishes the existing local-optimization baseline.
Values below are milliseconds; these are computation workloads, not coding tasks.

| Workload | Baseline median/p95 | Candidate median/p95 | Median change |
|---|---:|---:|---:|
| Unchanged agent parsing | 231.76 / 469.47 | 6.45 / 7.58 | 97.2% faster |
| Changed agent parsing | 107.21 / 118.11 | 125.07 / 143.43 | 16.7% slower |
| Unchanged skill listings | 218.04 / 236.80 | 37.99 / 41.11 | 82.6% faster |
| Changed skill listings | 217.73 / 250.05 | 238.89 / 245.74 | 9.7% slower |
| Tool schemas, uncached control | 5.94 / 6.75 | 5.79 / 7.24 | 2.4% |
| Token counting, uncached control | 2.15 / 2.71 | 2.10 / 2.42 | 2.3% |
| Serialization, uncached control | 5.03 / 5.92 | 4.99 / 5.70 | 0.7% |
| Repository discovery, uncached control | 56.56 / 67.60 | 53.26 / 58.28 | 5.8% |

Only repeated parsing clears the 20% threshold. Other controls do not justify
additional caches. Source validation costs time on changed inputs. Serialized
payload retained in the unchanged workloads was 8,460 and 13,730 bytes respectively;
this is not a measurement of total Python heap/RSS. The existing semantic index
remains separately managed and continues content/ignore-rule validation.

## Verification and limits

The affected cache/skills/subagent/compaction/optimization selection passed **135
tests** (`phase45-tests.xml`). Tests exercise concurrent access, eviction, failed
reads, model/tokenizer/version changes, file changes and restored timestamps,
Unicode paths, workspace changes, fresh tool/model binding, cancellation,
superseded summaries and model-boundary schema/message equivalence.

`phase45-static.json` compares affected files against HEAD: **zero new lint or
strict type findings**, with existing findings classified separately. New modules
also receive direct lint/type checks. The installed usable runtime is Python
3.13; the configured 3.11 interpreter points to a missing executable. Compatibility
is checked syntactically, but a 3.11 runtime pass is not claimed.

A full suite was attempted in an isolated home, with credentials removed. It
terminated around 81% at the timeout in the existing verdict-file pruning test
(`test_old_cycles_are_pruned_to_bound_the_store`). That file-system path was not
changed by this work. Other failures appeared before termination without a final
summary; they remain unclassified. Full-suite success is not claimed. Raw logs
are under `.tmp/phase45-full-final.log` and `.tmp/phase45-verified.log`. The verifier
disables randomized ordering because an existing compaction test leaves a manually
created monkeypatch active. Earlier system-environment runs also had incompatible
Deep Agents/Semble versions; these are distinct from the isolated passing run.

The configured primary model was OpenCode Zen / DeepSeek V4.1 Flash. Official
[Zen documentation](https://opencode.ai/docs/zen/#pricing) lists USD per million
tokens of 0.30 input, 1.20 output and 0.006 cached reads. Cached-write pricing and
explicit cache-control semantics were not verified. No provider-specific cache
change was made. Auxiliary models/retries lack an enforceable combined spending
bound, so the original USD 10 allowance was not dispatched. The paired live coding
tasks remain unevaluated; behavioral experiments remain disabled.

## Reproduction

Use an environment matching `uv.lock` with Python 3.11+:

```powershell
python scripts/benchmark_phase45.py --repetitions 10 --output phase45-measurements.json
python scripts/verify_phase45.py --tests tests/test_phase45.py tests/test_subagent_model_roles.py tests/test_subagent_registration.py tests/test_refreshing_skills.py tests/test_compaction.py tests/test_compaction_tail.py tests/test_compaction_derived_label.py tests/test_compaction_replay_leak.py tests/test_skill_libraries_and_resolvers.py tests/test_skills_runtime.py tests/test_skills_upstream.py tests/test_optimization.py
python scripts/verify_phase45.py --full
python scripts/check_phase45_static.py
```

In this workspace runtime checks used `.tmp/optimization-env/Scripts/python.exe`;
static checks used the installed `python -m ruff` and `python -m mypy`.
