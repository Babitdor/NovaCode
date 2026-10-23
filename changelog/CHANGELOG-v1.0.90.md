# NovaCode 1.0.90

## Security guardrails

- Add default-on prompt-injection handling across the main agent and built-in subagent paths. Tool results are passed as explicitly untrusted evidence, credential-like values are redacted from model-visible copies, and tool arguments that expose secret environment values are blocked.
- Add offline regression coverage, mutation checks, and a CI guardrail verification step. The focused guardrail verifier passed with 100% statement coverage for the new middleware; the configured offline CI regression suite passed (711 passed, 4 skipped, 11 subtests).
- Document the guardrail threat model and limitations in `docs/security-guardrails.md`.

These checks validate the implemented protections and regression behavior; they are not a guarantee against every prompt-injection technique.
