# NovaCode 1.0.21 — Zen Jev decisions and Nova exit screen

- Add OpenCode Zen Jev and Jev Free presets in /model → Decisions and /router. Use the Zen API key for the exact official System One endpoint, including decision-based tool pruning and opt-in auto-approval.
- Keep TypeSafe, Zen, and custom endpoint credentials isolated. Carry NovaCode's user agent and stable session header on Zen decision requests. Manage the Zen credential directly from the Decisions tab.
- Print Nova's wordmark, current version, session label, and `nova --continue <session-id>` after the Textual application exits. Check saved root-session metadata before offering continuation; adapt the wordmark for narrow terminals and safely render session names.

Zen Jev documentation: https://opencode.ai/docs/zen/#jev.
