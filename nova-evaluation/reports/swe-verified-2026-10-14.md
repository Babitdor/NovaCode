# SWE-bench Verified pilot, 2026-10-14

## Setup

- Dataset: Harbor Hub `swe-bench/swe-bench-verified` (500 human-validated issues),
  resolved to `sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341`.
- Agent: NovaCode with `ollama/deepseek-v4.1-flash:cloud`, default reasoning setting, learning off.
- Environment: Modal, two concurrent NovaCode trials.
- Harbor dry run: three tasks selected and NovaCode options validated without downloading tasks or calling the model.

## Results

| Issue | NovaCode reward | Reference patch reward | Model job |
| --- | ---: | ---: | --- |
| `matplotlib__matplotlib-14623` | 1 | 1 | `jobs/swe-verified/2026-10-14__08-01-51` |
| `django__django-16938` | 1 | not run | `jobs/swe-verified/2026-10-14__08-01-51` |
| `sympy__sympy-16886` | 1 | not run | `jobs/swe-verified/2026-10-14__08-01-51` |
| `astropy__astropy-12907` | 1 | 1 | `jobs/swe-verified/2026-10-14__08-12-31` |
| `scikit-learn__scikit-learn-14894` | 1 | 1 | `jobs/swe-verified/2026-10-14__08-12-31` |

Reference jobs: `jobs/swe-verified-oracle/2026-10-14__07-59-48` and
`jobs/swe-verified-oracle/2026-10-14__08-11-10`.

NovaCode resolved **5/5**, with no execution errors, graph cutoffs, loop stops,
or truncated final responses. Harbor reported 6,478,044 input tokens
(6,095,744 cached), 382,300 uncached input tokens, and 75,187 output tokens
across the five trials. It did not report a dollar cost.

## What the pilot established

The existing NovaCode Harbor wrapper can run and grade Verified tasks on Modal.
The first three tasks came from `-l 3`, which takes the first tasks in Harbor's
dataset order. The other two were selected explicitly to include different
repositories and bug types. These five passes do not estimate full-set accuracy
or show a before/after improvement in the coding agent. No failing trace in this
pilot supports an agent behavior change.

The harness now has a `swe-verified` launcher alias pinned to Harbor revision 2,
documented dry-run and reference-patch checks, and exact task filters. Harbor
task names require the `swe-bench/` prefix; an unprefixed filter matched zero
tasks in the dry run.

## Reproduce

From `nova-evaluation/`:

```bash
uv run python scripts/run.py swe-verified -m ollama/deepseek-v4.1-flash:cloud -e modal -l 3 -n 2
uv run python scripts/run.py swe-verified -m ollama/deepseek-v4.1-flash:cloud -e modal -i swe-bench/astropy__astropy-12907 -i swe-bench/scikit-learn__scikit-learn-14894 -l 2 -n 2
uv run python scripts/analyze.py jobs/swe-verified/<timestamp> --summary-only
```

The `jobs/` artifacts are local and ignored by Git; keep the same task IDs when
comparing a future harness change, and use separate runs for any broader sample.
