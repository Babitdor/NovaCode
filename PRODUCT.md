# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Developers who want a coding agent in their terminal: installing Nova into a repository,
evaluating it against other terminal agents, then looking up documentation and release
notes. Secondary: AI agents themselves — the site serves a readable "agent view" mirror
(`data-view="agent"`, `llms.txt`) and generated changelog/archive pages.

## Product Purpose

NovaCode (Nova) is an open-source (MIT) terminal coding agent. Give it a task in your
repository; it investigates, plans, edits files and runs your checks, then reports what it
observed rather than a completion claim. Success on this site: a developer installs Nova
(`uv tool install --python 3.11 novacode-cli`) or goes on to the docs/changelog/GitHub
convinced the product is credible and controllable.

## Positioning

A coding agent with readable memory. What a neighboring product could not truthfully copy:
- Delegation that keeps work separate: specialist agents, `/rooms` project channels that can
  embed Claude Code or Codex CLIs, and `/council` advisory votes.
- Memory in plain sight: a linked Markdown wiki under `~/.nova/agents/<agent>/memories/`
  with visible recall; `/dream` consolidation; source-validated `/cache`.
- Verification as evidence: `/prime` Deep Work's six checkpoints (define, investigate,
  implement, verify, challenge, deliver) bound to real tool-call output.
- Control: enforced read-only plan mode, approvals that fail closed, hard-coded denials
  that policy cannot disable, Git-worktree isolation.
Constraints stated on the page: Python 3.11+, MIT, hosted (18 provider IDs) or local
(Ollama) models, macOS/Linux/Windows. Not a benchmark; feature availability depends on
extras/provider/platform (the `±` tolerance lines).

## Operating Context

GitHub Pages serves the `gh-pages` branch at `/NovaCode/` — every asset path is relative
(never a leading slash). Release Markdown lives on `main/changelog/`; this checkout holds
only the generated `changelog/index.html`. Zero build, no framework, no bundler: one
`index.html` with inline CSS/JS plus small self-hosted assets (`assets/`). Any redesign
must keep `python tools/verify_site.py` passing (links, deep links, mobile, no-JS,
reduced-motion, the `/NovaCode/` prefix).

## Durable assets and commitments

- **The Coin** (user-locked): the 3D emblem coin band — drag/arrow-key spin, rim segments,
  orbiting sparks, engraved faces (`assets/coin.webp`), ring text — must survive any
  redesign.
- **The hero layers** (user-locked): the four-plane parallax artwork
  (`assets/hero-layers/`: sky, room, subject, plants; light/dark + flat composite with
  decode gating) must survive any redesign as the hero's image.
- Self-hosted type in `assets/fonts/` (Archivo variable, Geist, Geist Mono). No CDN, no
  network dependencies.
- Real product evidence: terminal captures (`Screen.png`, `assets/tutorial-*.webp`) are
  genuine current-TUI demonstrations; captions say so.
- Accessibility floor: full no-JS readability, `prefers-reduced-motion` collapse, visible
  focus, AA contrast in both themes.
- The "agent view" toggle and `#boot`-style content milestone behavior are function, kept
  in some form.

## Voice

Plain, concrete, honest about limits. Claims are stated as facts with their tolerance;
no superlatives, no invented benchmarks, no fake testimonials. Limits print next to
claims rather than in a footnote.

## Open decisions

- None recorded. (2026 session: user confirmed scope = landing page redesign + shared
  chrome restyle on docs/ and 404; factual copy stays verbatim.)
