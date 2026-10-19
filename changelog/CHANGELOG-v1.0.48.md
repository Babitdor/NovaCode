# NovaCode v1.0.48

## Added

- Figma, Vercel and Higgsfield MCP presets use the providers' official hosted
  endpoints with browser OAuth and Nova's existing secret-backed token storage.
- A Figma Desktop preset connects to the enabled local Dev Mode server. Figma's
  remote service currently restricts access to approved clients; desktop use
  requires an eligible seat. Higgsfield requires a paid subscription.
- The landing site includes a redesigned home page, verification page, custom
  404 page, responsive navigation, motion effects and local assets.

## Improved

- HTTP presets skip local package installation and unnecessary input prompts.
  CLI installation explains how to authenticate through `/mcp`.
- The README is condensed around setup, essential commands and optional
  capabilities, with brief instructions for the new integrations and a Classic
  terminal preview. The original Nova logo is retained.

## Validation and limits

- 74 focused MCP preset, authentication, session and tool-discovery tests passed.
  Preset/test lint and formatting, changed-line installer lint and preset typing
  checks passed. Account authorization and live provider tool calls were not run.
- All three landing pages passed a local Chrome smoke check at desktop and mobile
  widths with reduced motion: no JavaScript errors, failed assets or horizontal
  overflow. Local asset/link/anchor checks and application JavaScript syntax passed.
  This is not a full accessibility or browser compatibility evaluation.
- Python dependencies are unchanged. The static site includes local GSAP,
  ScrollTrigger, SplitText and Lenis browser libraries with their bundled notices.
- `FEATURES.md`, private credentials and local review artifacts are not published.
