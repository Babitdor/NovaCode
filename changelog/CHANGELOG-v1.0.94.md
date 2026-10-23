# NovaCode 1.0.94

## Caveman skill alignment and token accounting

- Align Say less with the installed Caveman skill: default Full intensity, exact technical content, normal code/commit/PR prose, no style announcements or tool narration, and clarity overrides for warnings, irreversible actions, ambiguous sequences and clarification requests.
- Replace Nova's exact bundled response-style block in the model request rather than carrying both default and Caveman directives. Preserve custom style blocks and other instructions.
- Add Lite, Full, Ultra and the three Wenyan intensities to `/caveman` and Options. Persist intensity independently; save the Options draft together and discard it on Cancel.
- Give Nova-managed subagents the selected prose style without overriding their model roles or adding routing/compression utilities to local/background subagents.
- Expand `/caveman audit` with provider-reported output, total and reasoning tokens, plus usage observation counts. Keep estimates separate from provider usage.
- Add an opt-in paired-inference script using saved credentials and the packaged core/security prompts. Shorter visible output does not guarantee lower total usage or cost on reasoning models.
- Cover all levels, persistence, UI Save/Cancel, small terminals and subagent integration. Give the provider-cancellation regression enough setup time to reach its simulated transport before timing out.

No dependencies added. Usage and audit limits: `docs/caveman-mode.md`.
