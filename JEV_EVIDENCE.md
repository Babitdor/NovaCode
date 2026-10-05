# Jev, Decisions tab, and token meters: verification

Spec: [JEV_SPEC.md](JEV_SPEC.md). Implemented autonomously under the user's
instructions; separate spec approval was not obtained. No dependencies added.

Verified with Python 3.13.5, pytest 9.0.2, and coverage 7.15.4:

- `python scripts/verify_jev.py`: **159 passed** across authentication,
  credentials, endpoint contracts, verdict pruning, model selectors and the
  Decisions tab. Textual pilot tests exercise actual mounted widgets.
- Focused agent-stream, compaction-notice, fallback-usage, context-history and
  rendered-meter regressions: **52 passed**, 81 unrelated TUI tests deselected.
  Five added counter assertions failed before implementation and passed after.
- `python scripts/verify_jev.py --mutations`: **4/4 manual mutants killed**:
  wrong endpoint receiving Jev credentials, missing-key transport allowed,
  invalid probabilities accepted, and cache sharing across models. Fresh
  subprocesses print the compiled mutant hash and verify execution origin.
  These results validate those four chosen defects, not every possible defect.
- `python scripts/verify_jev.py --coverage`: **32 tests passed; 32/32 executable
  lines covered** in configuration persistence, credential-routing factory and
  cache-path helpers. The gate exits nonzero on missing helper coverage. This
  is deliberately narrower than coverage of all changed lines; full changed-line
  and branch coverage were not established.
- Mypy with `--follow-imports=silent --no-incremental`: no issues in
  `decision_settings.py` and `context/history.py`.
- Ruff passes for new panel/helper/tests/verifier and authentication screen;
  `git diff --check` passes. Existing large modules retain baseline lint issues.
- `python -m novacode_cli --help` runs successfully. No paid live Jev request
  was made; the request shape is verified with an HTTP mock against TypeSafe's
  published [OpenAPI contract](https://api.typesafe.ai/openapi.json).

Acceptance mapping:

| Behavior | Evidence |
| --- | --- |
| Credentials stored independently of decision settings | `test_auth_screens.py`, `test_provider_auth.py`, `test_credentials.py` |
| Exact official endpoint receives Jev credentials; custom endpoints use separate key | `test_jev_auth.py` endpoint matrix, wrong-host mutation |
| Missing credentials block transport; response bodies do not echo secrets | `test_jev_auth.py` transport/error tests, missing-key mutation |
| Probabilities finite and within [0,1] | parameterized parser tests and Hypothesis invariant, invalid-probability mutation |
| Separate cache for each endpoint/model | model-switch cache test, shared-cache mutation |
| Toggle, presets, arbitrary model/endpoint, Cancel, validation errors, nested auth | `test_decision_settings.py` |
| Saving rebuilds with the existing chat model | enabled/disabled live-rebuild tests in `test_decision_settings.py` |
| Existing model/voice selection and offloading behavior | selector/role suites and existing verdict suites in verifier |
| SESSION sums distinct main-model calls; CTX uses latest call | `test_usage_sums_calls_but_context_uses_latest_call`, tracker tests |
| Compaction cannot restore an old API total; subsequent summaries detected | `test_usage_precedes_compaction_reset`, `test_a_later_summarization_event_is_detected` |
| Context excludes archive after compaction, survives resume, uses new model window | context-history tests, mounted TUI counter/resume tests, tracker window test |
| SESSION budget label and compaction preservation | `test_tui_token_usage_and_compacted_context` |

Limits: the project virtualenv cannot start because its base Python was removed.
Global DeepAgents 0.6.7 lacks APIs required by this repository (>=0.7,<0.8),
blocking full-suite collection with nine import errors. A broader TUI run also
hit an installed TorchAudio DLL error. Meter tests now disable unrelated eager
voice warmup to avoid loading or downloading voice models. These environment
problems were not fixed as part of this feature. Full-suite success is not claimed.

The SESSION meter records main-model usage supplied by providers on completed
turns, not provider quotas, costs, independent subagent/auxiliary usage, or
historical totals from restored sessions. CTX uses the latest reported input
tokens, then estimates effective history after compaction/resume; it is not an
exact measurement of the next prompt including every middleware addition.
No new dependency audit was needed; no credentials were added to source or config.
