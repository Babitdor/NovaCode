# NovaCode v1.0.65

## Fixed

- `/share artifacts` and `/share all` now use the same hosting path as artifact
  creation, fixing the unexpected `server` keyword error.
- Artifact hosting requires Cloudflare Tunnel and reports a missing `cloudflared`
  installation while retaining local artifact access. It no longer silently
  falls back to ngrok.
- Added integration coverage for automatic hosting, both share commands, tunnel
  reuse, updates, restored artifacts, session cleanup, and startup failures.
