# NovaCode v1.0.33

## Improved

- The shared coding agent asks for an independent check before generalizing from one example and gives more useful guidance when a shell check repeats without progress.
- Context compaction budgets encoded images by request size and treats provider HTTP 413 responses as a signal to compact. Oversized inline tool images can be omitted on the fallback retry.
- Terminal Bench runs use the effective Harbor time limit in the prompt, report failures and token use locally, and allow more graph steps before the task time limit.

## Validation and limits

- The full Terminal Bench 2 run passed 49 of 89 tasks before these fixes. A graph-cutoff replay passed, but took a different path below the old graph limit, so causation is unproven.
- An image-heavy video replay reached grading without the earlier HTTP 413, but still failed the verifier's second video. The prompt change has not established better task accuracy.
- Focused context, provider-error, loop-guard, analyzer, and evaluation-wrapper checks passed. The full unit suite and full Terminal Bench run were not repeated for this release.
