# Coding reliability follow-up: evidence

Specifications: [failed-check-gate-spec-2026-10-14.md](failed-check-gate-spec-2026-10-14.md)
and [numeric-requirements-spec-2026-10-14.md](numeric-requirements-spec-2026-10-14.md).
Spec approval was not obtained: this was an autonomous run after `Proceed`.
No dependency, commit, release, or push was made. The worktree had extensive
unrelated changes before this run; preserve them. Base commit:
`f865a2a47a5162722552a78bd762eb8eb0b76768`.

## Changes and acceptance evidence

| Behavior | Evidence |
| --- | --- |
| Failed recognized check after an edit causes one opt-in reminder | `test_failed_check_after_edit_does_not_satisfy_verification`, `test_shell_failure_wrapper_is_treated_as_failed_check`, `test_failed_check_recovery_runs_in_an_agent_graph` |
| Unpaired check is not verification; later successful check clears failure | `test_unpaired_check_does_not_satisfy_verification`, `test_successful_check_after_failed_check_clears_reminder` |
| Gate stays opt-in and bounded | Existing `test_edit_reminder_is_off_by_default`, `test_edit_without_later_check_gets_one_verification_reminder`, plus the new failed-check bounded assertion |
| Numeric definitions and ambiguous classification guidance reaches the model | `test_numeric_requirements_guidance_reaches_a_model_in_an_agent_graph` |
| Final task result | Matched Terminal Bench `financial-document-processor`: 0 -> 1, seven verifier tests passed in the final trial |

The failed-check and unpaired-check tests each failed RED before the middleware
change. The numeric graph test failed RED before the numeric prompt line, then
failed again for the newly specified classification line before that line was
added. The final focused GREEN run:

```text
.venv/Scripts/python.exe -m pytest tests/test_task_discipline.py tests/test_deepagents_compat.py tests/test_agent_stream.py tests/test_core_prompt_subagents.py -q --timeout=300
87 passed in 15.29s
```

Ruff 0.15.17 checked `task_discipline.py` with `INP001` ignored for the existing
namespace package: pass. Ruff formatting of the production module and focused
test file: pass. `git diff --check` on touched tracked files found no whitespace
errors. The test module still has ten pre-existing Ruff findings on unchanged
lines. Mypy 2.1.0 found five pre-existing errors in `task_discipline.py` on
unchanged lines; no new type error was reported. Pytest version: 9.1.0.

## Live evaluation

All completed financial-document runs used `ollama/deepseek-v4.1-flash:cloud`,
`reasoning_effort=off`, the same Terminal Bench 2 task revision, and Modal.
`scripts/compare_jobs.py` confirmed the baseline/final profiles matched.

| Job | Guidance | Reward | Failure/result | Input / output tokens |
| --- | --- | ---: | --- | ---: |
| `jobs/tb2/2026-10-14__13-16-36` | Existing | 0 | Reported pre-tax amount as `total_amount` | 653,817 / 5,755 |
| `jobs/tb2/2026-10-14__13-21-11` | Numeric definition | 0 | Tax values corrected; an order/shipping document was misclassified as an invoice | 327,517 / 3,947 |
| `jobs/tb2/2026-10-14__13-26-31` | Numeric and classification | 1 | 7/7 verifier tests passed | 1,893,768 / 12,380 |

The final run was longer (4m56s versus 3m28s baseline) and used substantially
more tokens, most of them cached. One task with one attempt per variant is too
small to attribute the gain causally or update NovaCode's overall score. The
extra reasoning/validation cost is a material tradeoff to measure on a larger
matched panel before treating these prompt lines as proven.

An additional current `chess-best-move` replay scored 0 after misreading the
board; no coding-harness change was made for that vision failure. The current
`filter-js-from-html` replay scored 0, but its clean-input verifier normalizes
the original with BeautifulSoup and compares that to the raw filtered output.
That comparison can reject a byte-preserving output required by the task. A
candidate filter replay was cancelled after this verifier defect was found; no
score is claimed for it. The filter run also failed XSS tests, so the invalid
clean-input check does not establish that the overall solution was correct.

## Limits and skipped layers

The opt-in gate recognizes common check commands and structured/nonzero shell
outcomes. It cannot establish that a successful check was relevant, understand
arbitrary custom scripts, or catch shell edits. The prompt test proves delivery
to a scripted model call, not model compliance. The full repository suite was
kept separate because of the user's time/credit limit and the recent full run
reported in the earlier evidence file (4,006 passed, two load-sensitive
failures that passed in isolation). Changed-line coverage, mutation testing,
randomized suite order, and a one-command gauntlet were not run; confidence is
limited to the RED tests, focused regression modules, static checks, and live
task verifier. There was no dependency change, so supply-chain audit did not
apply.

SHA-256 of final source/test files:

```text
task_discipline.py          56273A07CB734E4E159372F9A9E0E918AF9F45AE370136C6B4B90AACD02E45E5
core_agent_system.jinja     A3E173D00D1751BC58B9E160014F0BC7A4EC9C6ECF859790C130A9C1214C4DD3
test_task_discipline.py     22A76CBE077C117D706C199E3F3E5D2B6F6EB4D353EB131366EEBDB7C78296E3
```
