# NovaCode 1.0.95

## GitHub-linked co-author attribution

- Add optional `attribution.coAuthor.name` and `attribution.coAuthor.email` settings so future Nova-generated commit trailers can credit an existing GitHub account.
- Apply the configured identity to model instructions and structured commit tools while preserving human authors, sign-offs, and independent commit/PR attribution switches.
- Reject malformed identities and multiline trailer injection, falling back to the existing default identity.
- Document GitHub email association, contributor eligibility, account avatars, and the limits of attribution for existing history and commits made outside Nova.
- Test persistence, prompt and tool integration, valid Git trailers, idempotence, opt-outs, and malformed identity handling.

No dependencies added. Existing commits are not rewritten; GitHub account and email association must be configured separately.
