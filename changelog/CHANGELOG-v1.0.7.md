# NovaCode 1.0.7

## Added

- `/auth` can connect OpenAI with a ChatGPT account using OpenAI's Sign in with ChatGPT flow, while preserving API-key setup. The ChatGPT-plan path uses the Responses API with streaming and response storage disabled; OAuth tokens are stored separately in the OS credential store and refreshed as needed.
- OpenAI ChatGPT sign-in status is shown in `/auth` and can be removed independently of an API key.

## Notes

- ChatGPT-plan access is subject to OpenAI account eligibility and the current preview limits. Some hosted Responses tools are unavailable in this mode; OpenAI API-key access remains the full existing path.
