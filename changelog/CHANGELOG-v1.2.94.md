# v1.2.94 — The eval sandbox gets timers and honest recovery hints

**Released 2026-11-27** · PyPI `novacode-cli==1.2.94` · tag `v1.2.94`

Two eval-tool failures from a live trace, both reproduced and fixed:

## `ReferenceError: setTimeout is not defined`

The eval sandbox has no DOM timers, and agents polling a freshly started dev
server naturally write `setTimeout`. The sandbox bootstrap now provides:

- **`await sleep(ms)`** — a bounded host call on the guarded dispatch
  (capped host-side at 10s; it consumes the snippet's own wall budget, so a
  runaway poll stays inside the timeout).
- **`setTimeout(fn, ms)`** — a shim mapped onto `sleep`, awaitable like any
  promise-returning call.

## `SyntaxError: redeclaration of 'r'`

Top-level `let`/`const` bindings **persist between evals in one thread** (that
is what makes the interpreter inspectable), so re-declaring the same name in a
later eval throws. The recovery hint now names the actual trap and the escape
hatch — wrap the snippet in `{ ... }` to scope it, use fresh names, or assign
to `globalThis.x` — instead of the generic return-statement advice.

Regression tests reproduce both failures against the real QuickJS runtime,
including the block-scoping escape hatch and the host-side sleep cap.
