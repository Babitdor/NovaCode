# Saved session tabs

Validated locally on 2026-10-11, before the v1.0.32 release commit. No live model call was made during verification.

`/tabs resume` opens a saved-session picker across projects. `/tabs resume <id>` accepts an exact ID or unique prefix. A selected session opens in a separate tab using its recorded project folder, assistant and normal `--continue` startup. The current conversation remains in its original tab. An already-open session switches to its existing tab; concurrent launches reserve the saved identity.

History is shown in the existing paginated, inert transcript viewer: historical tool calls are displayed, never executed. The child restores model/context through the existing continuation path. Model availability and legacy-provider fallbacks retain that path's behavior. The ready frame updates the parent's actual model, thread identity and checklist. Tool approval mode comes from the current launch settings, never historical approval decisions.

Missing or unapproved folders fail clearly; saved IDs do not accept filesystem paths. Cancelling the picker opens nothing, and cancellation during subprocess startup removes the incomplete tab. Child turns now save before their completion frame, including cancellation/error paths, with a five-second save timeout. Shutdown saving remains best effort.

Verification on Windows, Python 3.13.5 and Textual 8.2.7:

- Expanded cross-session/session/model-restoration suite: **170 passed**, 91.69 seconds; three dependency deprecation warnings.
- Historical transcript, crash recovery, launch and project-picker regressions: **17 passed**, 9.54 seconds.
- Communication and resume modules: **100% line coverage** (489 statements), **99% branch-enabled coverage** overall. The resume resolver has 100% line and branch coverage.
- Strict mypy on the new resolver: passed. Full configured Ruff on the resolver, fatal/syntax checks on affected files, formatting of added code and `git diff --check`: passed.

Reproduce with a supported Nova development environment:

```powershell
python scripts/verify_cross_sessions.py
python -m pytest tests/test_session_transcript.py tests/test_session_crash_recovery.py tests/test_session_launch.py tests/test_project_session_picker.py -q -p no:cacheprovider
python -m mypy --follow-imports=silent novacode_cli/sessions/resume.py
ruff check novacode_cli/sessions/resume.py
```

This workspace used `.tmp/cross-session-env/Scripts/python.exe`. Machine-readable results are in `.tmp/cross-session-verification/regressions.json`, `tests.xml` and `coverage.json`. No paid-provider or Python 3.11 runtime validation was performed. Full-suite baseline timeouts and repository-wide lint limitations remain documented in `CROSS_SESSION_EVIDENCE.md`; no full-suite success is claimed.
