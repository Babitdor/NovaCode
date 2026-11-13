# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues, pull requests, or discussions.**

If you believe you have found a security vulnerability in NovaCode:

1. **Report privately** using a maintainer-approved channel:
   - **Preferred:** GitHub private vulnerability reporting for this repository (if enabled by maintainers).
   - **Contact:** `[MAINTAINER: add a monitored private security contact email or URL]`
2. Include enough detail to help reproduce and assess the issue (affected version/commit, environment, steps, impact, and any proof-of-concept). **Do not include secrets or credentials.**
3. Allow maintainers reasonable time to investigate and remediate before public disclosure.

> **Maintainer action required:** replace the placeholder contact above and confirm the preferred private reporting channel.

## What to expect

- Maintainers will acknowledge receipt and triage the report as capacity allows.
- You may be asked for additional information to reproduce or validate the fix.
- Once resolved, maintainers may publish an advisory/release note appropriate to the severity and impact.

## Security guidance for contributors

- Never post secrets, API keys, tokens, passwords, or private keys in issues, PRs, commits, logs, or screenshots.
- Follow the project's security and permissions model described in [docs/security-guardrails.md](docs/security-guardrails.md) (product guardrails; not a vulnerability reporting channel).
- If you accidentally commit sensitive data, notify maintainers immediately so credentials can be rotated and remediated.

## Supported versions

Security fixes are provided at the maintainers' discretion, typically for the latest released version and the default branch. If you are using an older release, please mention the exact version in your report.
