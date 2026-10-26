# NovaCode 1.1.0

## Workspace knowledge and teammate approvals

- Add a default-enabled workspace knowledge cache with source validation,
  isolated assistant namespaces, and `/cache status|clear|on|off` controls.
- Add persistent, per-teammate Auto Approve for desktop input, off by default,
  while retaining durable decisions, pause controls, and recovery safeguards.
- Hide the dedicated Windows QEMU console during computer and teammate startup.

## Session tabs and interface layouts

- Add an opt-in experimental side-by-side diff preview in `/themes`, with line
  numbers, syntax highlighting, theme-aware change shading, and a narrow-screen fallback.
- Add a `+` button for opening a blank session with the home screen and focused input.
- Add close controls to extra session tabs. Dismiss tabs immediately while worker
  shutdown and worktree cleanup continue; restore tabs when shutdown fails.
- Preserve the banner canvas and logo position when opening or switching sessions.
- Keep session navigation at the top in every interface style, including Centered.
- Extend regression coverage across all 14 home styles at wide and narrow sizes.
- Display provider-exposed thinking and reasoning summaries across SDK formats,
  including completed replies and gateway metadata, without repeating streamed text.
- Request visible summaries for supported OpenAI and Azure reasoning models.
- Respect manual scrolling during thinking, tool updates, and final answers;
  keep expanded thinking available while the reader is scrolled away.

## Installation and CI

- Constrain Parakeet to `sherpa-onnx>=1.10.0,<2` to avoid missing wheels in 2.0.1.
- Quote version constraints in installation hints and retain the matching lockfile.
- Stabilize resize checks by waiting for the rendered terminal layout.

Validation: focused cache, approval, computer, session, interface, installation,
and release metadata checks. Parakeet wheel installation and import were verified
on Windows Python 3.11; no speech model download or live transcription was required.
