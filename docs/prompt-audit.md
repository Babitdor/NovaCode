# Nova prompt audit and coding-agent comparison

Reviewed 2026-10-23. This is a broad comparison of **15 coding agents**, not a
claim to cover every commercial product or obtain proprietary system prompts.
It uses public first-party source and official documentation. No leaked prompt
collections were used as research evidence. Patterns are adapted to Nova's own
tools and runtime rather than copying another agent's instructions.

## Nova inventory and prompt routing

The starting tree contained 49 Jinja templates. The audit inventoried them all,
scanned instructions and foreign placeholders, and reviewed the main,
continuation, planning, generation, initialization, summarization and research
paths in detail. Domain-specific review/evolution/output schemas were retained
unless a concrete conflict was found.

- `agents/core_agent.py:get_system_prompt` renders `core_agent_system.jinja` with
  the actual shell/platform and async-availability context.
- `System_Prompt_Nova.jinja` supplies the packaged continuation/creation defaults.
  Both now include the same `coding_workflow.jinja` and `file_paths.jinja`.
- `nova_standing_instructions.jinja` keeps identity, authorization, memory,
  plan recall, communication and completion preferences.
- `prompts/__init__.py` loads these main templates and the new shared blocks from
  the package, bypassing global prompt-history/activated-template overrides.
  Includes are resolved by the package environment too. Global agent memory is
  context, not the source of the built-in coding identity.
- The earlier names `core_agent_prompt.jinja`, `prompts/agent.md` and
  `prompts/Codex.md` are not present in this checkout. Their absence is not
  evidence that the current main prompt is loaded from a global agent file.
- `claude_plan_mode.jinja` and `init_agents_md.jinja` are retained template
  interfaces; no direct renderer call for either was found in the Python call-site
  search. They were still corrected so future use does not reproduce stale rules.

## Findings and changes

| Severity | Evidence in the original prompts | Applied change |
| --- | --- | --- |
| High | Main workflow required a separate plan-approval pause solely for large/multi-file work, while other instructions said to finish authorized implementation. | Plan mode is used when requested or runtime-required. Active read-only mode still requires its approval transition; existing authorization is reused. |
| High | Research prompts demanded `write_file` to an exact or shared fallback path even for existing files or missing assignments. | Shared conditional delivery contract: assigned workspace paths, create vs edit, no invented common output file, explicit blocker reporting. |
| High | Fact-checker prohibited fetching and called search snippets sufficient verification. | Consequential claims require an inspected supporting source; inaccessible, snippet-only and unchecked evidence stays unverified. |
| High | Summaries suppressed all raw evidence, prioritized old requests over unfinished work, and claimed no other history source existed. | Preserve current objective, corrections, authorization provenance, exact decisive errors/commands, task IDs and recovery references; do not execute historical requests or promote tool/peer text into user authority. |
| Medium | Main and plan prompts categorically rejected Windows host paths, despite workspace/mount normalization in `integrations/host_path.py` and `utils/backend_patches.py`. | Shared guidance distinguishes preferred virtual paths from supported normalized host paths and real shell paths; no path-rewrite bypass or new access grant. |
| Medium | Agent generator requested internal reasoning, invented shell flags, categorically denied a `run_tests` tool, and padded every role to 500–800 words. | Observable actions/evidence, actual runtime schemas, synchronous vs background delegation, proportional prompt size and explicit trust boundaries. |
| Medium | `init_agents_md.jinja` inserted `pip install -e .`, `make test`, `make lint` and `make format` regardless of project evidence. | Render discovered commands only; explicitly state when commands remain unknown. |
| Medium | Legacy planning template used another product's identity and unresolved JavaScript `${...}` placeholders in Jinja. | Nova-specific, runtime-aware read-only planning instructions; legacy filename preserved. |
| Medium | Research/semantic orchestration assumed multiple `task` calls were necessarily concurrent, prohibited direct fallback and gave contradictory retry rules. | Describe actual synchronous/background capabilities, respect capacity, inspect failures, retry with evidence or finish feasible work directly. |
| Medium | Standing rules banned all added inline comments and assumed a personal HABITS file was always present. | Permit sparse explanatory comments; memory files are existence-aware and scoped. |
| Medium | Iteration prompt could encourage checking off remaining work to escape a no-change loop. | Only verified checklist items may be completed; record blockers instead of inventing changes or completion. |
| Medium | Vision transcription lacked an explicit boundary around instructions or credentials visible in an image. | Image instructions remain untrusted content; redact recognizable credentials while retaining diagnostic context. |
| Low | Main/continuation had separate workflow prose and repeated standing instructions. | Shared package-controlled workflow/path blocks; focused standing preferences and contract checks for consistency. |

Runtime approvals, path enforcement and prompt-injection middleware remain the
enforcement layer. Prompt text cannot guarantee security, authorize an action or
repair a missing capability by itself.

## Public coding-agent comparison

Links below pin the source revisions observed during this audit. Official docs
are dated by this review because their published URLs remain mutable.

| Agent | First-party evidence | Useful pattern for Nova | Deliberately not copied |
| --- | --- | --- | --- |
| Codex | [Published prompt](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/gpt-5.2-codex_prompt.md), [official prompting guide](https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide) | Complete scoped work, preserve dirty-tree edits, use precise changes and evidence-based verification. | Model-specific communication rules, tool names and approval assumptions. The guide concerns its named Codex model, not every Nova provider. |
| Claude Code | [Official best practices](https://code.claude.com/docs/en/best-practices) | Give verifiable tasks, manage context deliberately and use focused validation. | No claim to possess its private full system prompt; no undocumented native-tool schemas. |
| Gemini CLI | [Prompt snippets](https://github.com/google-gemini/gemini-cli/blob/cd1b45c2db0a5ea7b7fc3b67c1ba5879601d6005/packages/core/src/prompts/snippets.ts) | Modular workflow/capability sections, project dependency verification, lower-trust external context. | Its inquiry-by-default policy and product-specific approval transitions. Nova retains user-directed action and its own runtime gates. |
| Cursor | [Official rules documentation](https://cursor.com/docs/rules) | Keep instructions scoped to relevant project context and avoid irrelevant global rules. | No claim to know the hosted agent's private system prompt or hidden implementation. |
| GitHub Copilot Chat | [Capability-aware instructions](https://github.com/microsoft/vscode-copilot-chat/blob/5863f5a7088958050792b5dccbe8b46c6e13eccc/src/extension/prompts/node/agent/defaultAgentInstructions.tsx), [prompt assembly](https://github.com/microsoft/vscode-copilot-chat/blob/5863f5a7088958050792b5dccbe8b46c6e13eccc/src/extension/prompts/node/agent/agentPrompt.tsx) | Compose instructions around available tools, environment and relevant context. | VS Code-only tool references or assuming that this source represents every hosted Copilot configuration. |
| OpenCode | [Provider overlay](https://github.com/anomalyco/opencode/blob/9b3a7d117ddd2b8dab14e95c1eb5d5694ac85da9/packages/core/src/plugin/system-prompt/anthropic.txt), [another model prompt](https://github.com/anomalyco/opencode/blob/9b3a7d117ddd2b8dab14e95c1eb5d5694ac85da9/packages/core/src/plugin/system-prompt/meta.txt) | Match existing comment density; keep local tool instructions precise and preserve active corrections. | Provider identities, product-specific subagent tools and automatic delegation mandates. |
| Cline | [Act-mode prompt](https://github.com/cline/cline/blob/b11ad3abc0c76f30c93dc19dc82f5352e4a0eb1c/sdk/packages/shared/src/prompt/system/act.ts) | Gather codebase context, use existing conventions, batch independent work and validate the result. | Mandatory long upfront planning for every task or treating every tool-free response as final completion. |
| Roo Code | [Rules module](https://github.com/RooCodeInc/Roo-Code/blob/b867ec9145750d0ae1ff7f02d35406e9bf2a0b16/src/core/prompts/sections/rules.ts) | Ground commands in the actual OS, working directory and mode restrictions. | Mandatory user response after every tool call, vendor secrecy rules and Roo-only completion/question tools. |
| Aider | [Base prompts](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_prompts.py) | Finish implementations and restrict edits to the requested scope. | File-sharing approval rules belonging to Aider's chat/editor model or automatic-commit conventions. |
| Goose | [System template](https://github.com/block/goose/blob/c3bfc5f7b0e230180ba3164ec1ee84fd951eaf0d/crates/goose/src/prompts/system.md) | Describe extensions and capabilities actually present in the runtime. | Goose's identity, extension-control APIs or assuming absent capabilities exist. |
| OpenHands | [SDK prompt](https://github.com/OpenHands/software-agent-sdk/blob/acd96e6fa424c02432304395b9208f99be24b54d/clients/typescript/src/prompts/system-prompt.ts) | Explore before editing, make minimal direct changes, avoid duplicate artifact files and handle version-control operations carefully. | Mandatory elaborate troubleshooting rituals or its specific finish tool. The SDK source is distinguished from the separate application UI repository. |
| SWE-agent | [Default configuration](https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/config/default.yaml) | A task and observation contract should match its actual environment. | Benchmark-only rules forbidding test edits or telling the agent to revert tests using Git; these are unsuitable as Nova's general defaults. |
| Continue | [CLI system-message construction](https://github.com/continuedev/continue/blob/5522c6f44ca0ac3528b37244818fbfa39b5af470/extensions/cli/src/systemMessage.ts) | Respect active read-only plan mode, headless interaction constraints and requested JSON output. | Product-specific commit signatures or imposing final-only output on interactive sessions. |
| Pi | [System-prompt builder](https://github.com/badlogic/pi-mono/blob/136b87e8a7f5a3e5186de0289523ba327dc592fc/packages/coding-agent/src/core/system-prompt.ts) | Modular sections and guidelines derived from declared tools; avoid shipping irrelevant instructions. | Pi-only tool names or replacing Nova's approval and memory model. |
| Qwen Code | [Core prompt builder](https://github.com/QwenLM/qwen-code/blob/5a0be334326566bec502a0cfcaf35c11557247d3/packages/core/src/core/prompts.ts) | Make question/plan guidance match interaction capabilities; discover real verification commands and adapt after errors. | Qwen-specific tool identifiers and interaction policies that would contradict Nova's current runtime. |

These are design observations from selected public versions, not a leaderboard,
a reproduction of proprietary prompts, or proof that copying instructions raises
task success. Variants, custom agents and other products remain outside coverage.

## Size measurements

Same render context before/after: `has_async_agents=True`, default unspecified
shell variables, no user-memory injection. Counts are Unicode characters in the
rendered template, not tokenizer measurements or whole-request token totals.

| Template | Before | After | Reduction |
| --- | ---: | ---: | ---: |
| `core_agent_system.jinja` | 26,625 | 23,789 | 10.7% |
| `System_Prompt_Nova.jinja` | 21,447 | 19,031 | 11.3% |
| `nova_standing_instructions.jinja` | 6,864 | 4,244 | 38.2% |

The standing block is already included in each main render; these savings must
not be added together. Runtime memory, tools, provider adapters and overlays add
their own context. No API cost or model-quality improvement was measured.

## Verification and limits

Contract checks cover shared workflow/path inclusion, frozen built-in defaults,
Jinja syntax, balanced top-level sections, absent foreign runtime placeholders,
assigned-file delivery, inspected-source fact-checking, provenance-preserving
summaries and project command discovery. Existing plan, research-roster and
compaction checks provide regression coverage.

The focused combined run passed **169 tests** (one warning), covering prompt
contracts, packaged defaults, research roles, conversation context, compaction,
memory prompt ordering, prompt-injection guardrails and Caveman behavior. Ruff
checks for undefined names and import ordering passed for the changed prompt
loader and prompt tests. The diff whitespace check passed.

Memory awareness remains explicit in both packaged main prompts. Runtime
`AgentMemoryMiddleware` additionally renders `longterm_memory.jinja` with selected
standing/project prefixes, the memory index, optional habits and retrieved
lessons. It does not inject the whole corpus; the agent is instructed to search
and read relevant pages, verify project scope and treat historical memory as
evidence rather than new authorization.

These are deterministic rendering and integration checks, not evidence that every
model follows every instruction. The saved-model Caveman checks are documented
in `caveman-mode.md`; they are synthetic inference probes, not a before/after
prompt-quality evaluation. Live cross-provider task-success, approval-friction
and prompt-quality evaluations remain unrun. A useful future eval
would pair the same model/tasks before and after, using dirty working trees,
missing tools, Windows paths, failed checks, forged source instructions and
context-compaction continuations, and measure outcomes rather than prose style.

Changes are local and reviewable. No release, commit, push, global agent-memory
edit, dependency installation or external publication is part of this audit.
