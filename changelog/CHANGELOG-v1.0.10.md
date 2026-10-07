# NovaCode 1.0.10

- Add Google browser sign-in under `/auth` for Gemini Developer API, using a user-owned Desktop OAuth client and Cloud quota project. Includes PKCE, state validation, cancellation, refresh, saved-account selection and disconnection; API keys remain available.
- Add guided Anthropic Console setup alongside API-key entry. Claude subscription OAuth is not offered because Anthropic explicitly excludes third-party applications.
- Display the selected Google authentication method in provider readiness and credential screens.
