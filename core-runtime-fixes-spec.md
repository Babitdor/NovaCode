# Core runtime fixes

Scope: the four findings in the runtime review, plus session isolation for middleware
events and remembered approvals. Existing user edits remain untouched. No new
dependencies, releases, pushes, or commits are planned.

Spec approval: not obtained (autonomous run following the request to fix all findings).
Assurance tier: 3, because authorization and concurrent execution are affected.

Acceptance criteria:

- Local execute traverses the remaining tool middleware in both sync and async
  graphs. Plan Mode, hard policy denials and workspace denials prevent dispatch.
  Permitted commands retain local shell behavior.
- Planning blocks the installed Python kernel, detached daemon and other native
  mutation tools, while file reads, approved MCP reads and plan-file writes work.
- Git command-name prefixes and remote mutations require approval. Reviewed Git
  inspection commands remain automatically allowed; explicit deny rules win.
- Concurrent kernel calls serialize their entire request/response exchange.
  Cancelling an active request kills its child before another request can reuse it;
  cancelling a queued request does not kill an active one. Sessions have separate
  namespaces and closing a session closes its kernel.
- Events and remembered approvals remain in their originating project/thread,
  including worker-thread production and approval UI callbacks. Session cleanup
  removes these resources. Standalone callers retain their existing API behavior.

Clarification from tracing installed tools: native mutation coverage includes file
recovery, background-process restart/termination, artifact creation/publication,
and non-read-only HTTP methods. GET/HEAD/OPTIONS investigation remains available.
Kernel lifecycle checks also cover closing during an active request, rejecting
use from a foreign event loop, and discarding malformed protocol responses before
the next call. These preserve the same ownership and response-order invariants.

Failure model: guard bypass (composed graph tests); hostile shell tokens (table
and generated tests); concurrent reads, cancellation and stale responses (real
kernel tests); identity leaks and resource retention (two-session tests and cleanup
checks). Network/provider behavior and OS sandbox implementation are unchanged.

Verification: focused pytest regression tests first, existing nearby suites,
ruff on changed files, targeted mypy, coverage and manual mutation controls,
then the offline full suite. Results and any unavailable checks will be recorded
in core-runtime-fixes-evidence.md. No independent agent verification is planned.
