# NovaCode 1.2.60

## Reasoning effort: new tiers, honest footer, one semantic

- **`xhigh` and `max` join the effort dial.** OpenAI exposes `xhigh` (deep
  research / long agentic runs) and `max` on GPT-5.5+; Anthropic's adaptive
  models take `max` (Sonnet 4.6+); Grok 4.6+ takes `xhigh`. `/effort xhigh`
  and `/effort max` now pass through natively on OpenAI, ride `max` on
  Claude, and clamp to each provider's verified ceiling elsewhere (Gemini,
  GLM, DeepSeek → `high`; Ollama → `high`; OpenRouter forwards verbatim).
  Available everywhere effort is: `/effort`, `/settings`, the router editor,
  and remote clients.
- **The footer stops advertising effort a model never receives.** Several
  providers silently drop the field (catalog providers, plain
  openai-compatible endpoints, gateway families without a verified control).
  The composer line now shows "model with high effort" only when the built
  model actually consumes it — a new `model_accepts_effort` predicate mirrors
  the construction branches.
- **`/effort default` means one thing everywhere.** The legacy CLI handler
  used to normalize `default`/`none` to `off` — silently pinning *minimum*
  thinking instead of restoring the provider default. Both handlers now
  clear the override (stored unset), and every settings surface displays an
  unset value as "Default · provider" instead of misleadingly showing "Off".
- **GLM-5 effort is real.** Live-verified 2026-11-24 against the Zen
  gateway: `reasoning_effort` is accepted on glm-5.3-flash — `none` disables
  thinking, `low`/`high` reason, raw `medium` produced no reasoning at all
  (so it follows `high`). The footer previously claimed an effort the model
  never received.
- **One effort renderer.** The opt-in `effort` statusline field is gone; the
  composer line is the single place effort is shown (fixes the duplicated
  "with high effort · high effort" footer).

## Code Mode subagents appear in the dynamic agents panel

- A `task()` delegation from a Code Mode snippet now renders in the
  SubagentsDock — the same panel async background tasks use — with
  running → done/failed states, duration, and error text. They previously
  showed only as inline transcript cards.
- Cleaned up as a proper phase kind (`code`) rather than a string-prefix
  hack: the panel's model column resolves it to the subagent role like
  direct/eval dispatches, and the delegation no longer double-renders
  inline.

## Small TUI fixes

- The plan-mode indicator is plain text — no symbol prefix, matching the
  accept-edits indicator style.
- `?` on an empty composer opens the command/shortcuts reference, as the
  modes line has always advertised. Mid-text `?` still types itself.
