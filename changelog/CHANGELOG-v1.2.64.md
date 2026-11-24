# NovaCode 1.2.64

## Composer toggles restyled: state is the highlight, not the label

- Toggle buttons show only their name — Prime, Passing, plan, browser, lite,
  serena, Code Mode, Oracle. Selection is the highlight (accent tint + bold);
  no more `:off/:on` suffixes anywhere.
- The `lite` toggle is coloured by its active compression mode: Caveman tints
  amber, Ponytail tints green; off stays muted.
- The Oracle `Settings` button is gone from the strip — the same screen stays
  available via `/oracle settings`.
- The grid was rebalanced for the shorter labels (row now fits at ~75
  columns before wrapping) and the obsolete Oracle-settings handler and CSS
  were removed.

## Agent profiles: per-agent identity, boundaries and workers

- New `agents/profiles.py` defines composer agent profiles — `nova`, `serena`,
  `ralph`, `nora` — each with its own display identity and colour, plus
  per-profile capability boundaries (control, delegation and research-MCP
  toolsets) enforced by a request-scoped middleware, and a system-message
  append per profile.
- New `core/profile_workers.py` runs profile-scoped worker subagents: the
  agent loop threads a workers context through the turn, re-renders profile
  identity in the transcript, and forwards profile worker subagent/preview
  events into the dynamic agents panel. Plan-mode approval requests inside a
  worker inherit the owner's original request text.
- `summon_agent` is blocked during plan mode like the other implementation
  subagents; the harness adapter stamps AI messages with their agent profile;
  Ralph's progress card shows live context usage.
- New MCP presets: Obscura (headless browser navigation, path-based install)
  and Browser Use (session screenshots).

## Housekeeping

- Removed the stale `Subagent-failed.png` / `Subagent-success.png` screenshots
  and `trace-subagent.html` from the repo root.
