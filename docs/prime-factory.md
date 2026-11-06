# Prime software factory

Enable `/prime on`, then give Nova an explicit development goal, for example
“Fix the duplicate notification.” Questions and explanations use normal chat.

Prime runs requirements, documentation research, architecture and test planning
before asking you to approve implementation. The review includes acceptance
commands, file ownership, task dependencies, model routes, setup commands,
isolation and budgets. Existing uncommitted edits are disclosed and excluded
from the factory branch. Incorporating them requires a reviewed commit first.

Implementation uses dedicated integration and task worktrees. A configured
container profile selects the existing pinned-image container worker transport;
Prime never pulls an image or silently changes provider. Independent tasks can
run concurrently, with at most three specialists. Acceptance test files are
frozen after the test engineer creates them. Test-first checks require an
observed failure before implementation and a pass against the integrated result.
Each failed check gets at most three repair rounds. Missing required checks,
changed acceptance definitions and unresolved independent review findings block
delivery.

Prime asks for a second approval showing the complete candidate diff, destination,
research and check results before pushing a branch and opening a GitHub PR.
Changing the candidate expires that approval. Merge and deployment require
separate instructions. Tool auto approval does not approve either factory gate.
Without an interactive approval handler, a run suspends with its continuation ID.

Commands:

| Command | Effect |
| --- | --- |
| `/prime status`, `/prime report` | Inspect the current run |
| `/prime tasks`, `/prime checks` | Browse saved work, including legacy Prime records |
| `/prime agents`, `/prime sources` | Inspect specialist routes, usage or documentation |
| `/prime pause` | Stop work and preserve branches |
| `/prime cancel` | Cancel the run and preserve unfinished branches |
| `/prime off` | Pause active work and disable new runs |
| `/prime resume <id>` | Prepare an explicit continuation; send Continue to run it |
| `/prime budget <tokens> <active-minutes>` | Change the run budget and require plan review again |
| `/prime waive <check-or-finding-id> <reason>` | Record a specific human exception for the current candidate |

Waivers retain failed or unavailable execution evidence and expire when the
candidate changes. Delivery review and the PR body disclose them as unverified
exceptions; they never become passing checks or compliance claims.

Defaults are 500,000 shared input/output tokens and 60 active minutes. Capacity
is reserved before provider calls, including compaction. Unknown usage is charged
conservatively; it is labelled separately from provider-measured usage. Time while
all work waits for human input is excluded. Price estimates are not known costs.

Role routes use separate `factory_model_<role>` configuration entries; unset
roles inherit the bound session model. Roles are `requirements`, `research`,
`architecture`, `developer`, `test_engineer`, `verification` and `security`.
Use `NovaConfig.set_factory_model(role, provider, model, base_url=None)` to persist
an exact route. Unavailable configured routes suspend work without fallback.

Runs and knowledge are versioned local SQLite records under the repository's Git
common directory, in `prime_factory`. Linked worktrees share run storage and keep
separate file-index fingerprints. Python AST indexing is bounded; unsupported
languages are labelled rather than represented as fully analysed. Sources record
successful retrieval, version, digest, excerpts and supported decisions. Search
snippets cannot pass the research gate. Reused sources are fetched again.
Knowledge and retrieved documents are untrusted context and never permissions.

Exit or cancellation checkpoints the run. Explicit resume reconciles owned
workers, worktrees, research, approvals and evidence before new dispatch. Storage
failure blocks further mutations. Legacy reports remain inspectable; loading one
starts a fresh factory plan without legacy approvals or passing status.
