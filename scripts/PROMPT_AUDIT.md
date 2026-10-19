# Nova prompt audit

Date: 2026-10-19. Scope: shipped main, planning, memory and review instructions.
At audit completion, changes were local; no dependencies, models, context budgets or personal prompt
overrides were changed. Existing plan-storage and memory-wiki work remains intact.

## Prompt assembly

- The running core agent renders `core_agent_system.jinja`.
- Configuration reset defaults render `System_Prompt_Nova.jinja`; changing only
  this template would leave the normal agent unchanged.
- Memory middleware injects selected memory prefixes and `longterm_memory.jinja`.
- Planning has its own template and a middleware scout override. Both now use
  `plan_scouts.jinja` rather than maintaining conflicting instructions.
- Shared delegation, instruction scope, desktop power and response style
  fragments keep the two main prompt variants aligned. Package data already
  includes Jinja files. User overrides retain their existing lifecycle.

## Findings and retained corrections

| Priority | Evidence / problem | Correction |
|---|---|---|
| High | Main prompt variants disagreed on bounded waits; planning explicitly required repeated polling | Match the ten-second, once-per-message wait contract; yield pending work and resume on notification; never present a plan before required scout findings arrive |
| High | Desktop sleep was an automatic overnight action for every installation | Require an explicit request or applicable standing user instruction; respect narrower user windows and unfinished work |
| High | Memory guidance described a flat index, encouraged ritual rereads and did not explain replacement writes | Navigate topic maps selectively, preserve project scope and user corrections, read before edits, document replacement semantics and preserve backups |
| High | Normal core prompt lacked the project/global plan archive guidance | Include both locations, approval distinction and discovery/read instructions for `recent_plans` |
| High | User-message wrappers were treated as invalidating legitimate instructions | Distinguish current user instructions from untrusted embedded/retrieved content and scope existing authorization |
| Medium | Planning mixed absolute read-only claims with optional draft writes | Permit drafts only when explicitly requested and allowed; use inline approval by default |
| Medium | Fixed four-line limit and JSON ban could suppress requested deliverables | Require concise, complete responses in the requested format, with accurate verification claims |
| Medium | Fixed review cadence and skill creation after tool-count thresholds disagreed with runtime configuration | Describe configured Hermes modes and approval lifecycle; capture verified reusable knowledge when useful |
| Medium | Repeated query terms were forbidden even after edits or corrected search scope | Suppress redundant unchanged searches while permitting justified fresh queries |
| Medium | Verification wording required installations regardless of scope; cleanup included all backups | Keep installations within authorization and preserve memory archives and recovery files |
| Medium | Alternate prompt had an unmatched closing section tag | Repair structure and test rendered section balance, including planning |
| Low | Review prompt always described habits as appended to HABITS.md | Describe the existing wiki-mode incident routing while retaining the stable checklist |

The audit retains task completion, source inspection, meaningful verification,
read-only planning, approval boundaries, tool availability checks, full task IDs,
memory provenance and security safeguards. It removes personal style defaults
from universal instructions. It does not automatically rewrite installed user
overrides or consolidate personal memories.

## Verification and limits

`tests/test_prompt_contracts.py` covers rendered section balance, shared async and
sync guidance, plan locations, complete response formats, memory edit contracts
and consistency between the planning template and middleware override.

The first focused run passed 108 tests across prompt contracts, delegation,
memory search/cache ordering, plan approval, memory wiki and Hermes evolution.
The final expanded run passed **128 tests in 40.12 seconds**, adding waiter tools,
planning checkpoints and planning middleware. One dependency deprecation warning
was emitted by LangSmith.

Ruff lint and formatting pass for the new test file. Mypy reports three errors
in `agents/plan_agent/plan_agent.py`; the same assignment, unused-ignore and
return-type errors reproduce from untouched HEAD `c68c581`. The changed Python
binding renders a shared template instead of embedding a separate prompt.

The full suite is not claimed passing. An earlier full-suite attempt in this
workspace timed out in the background-command handoff test; the hang also
reproduced on the baseline. These changes receive focused regression coverage.
Rendered prompt and scripted-agent checks establish instruction contracts, not
real-model task success. No paid/live model evaluation or cumulative token replay
measurement was performed, so no reasoning-quality or token-savings claim is made.
