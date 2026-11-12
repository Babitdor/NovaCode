# NovaCode 1.2.10

## Command Center

- Add `/command-center` (`/cc`) with project and status grouping, status filters,
  search, session details and paginated saved conversations.
- Show live tab state and measured token usage when available; label missing
  metadata honestly and keep details accessible in narrow terminals.
- Switch to live tabs or open saved conversations through the existing tab-resume
  flow, preserving workspace approval and duplicate-session checks.

## Reliability

- Bound OS keychain discovery and credential reads so an unavailable backend
  cannot indefinitely block startup; preserve credential-file lock ownership.
- Record startup import boundaries and keep tracing failures from aborting startup.
- Preserve lexical topic matches during semantic memory search, reject nonfinite
  semantic scores and fall back when provider reranking fails.
- Preserve Code Mode warnings on errors and improve timeout diagnostics, including
  native QuickJS timeout exceptions.
- Migrate evaluation run queries to the current async LangSmith API and raise the
  LangSmith minimum to 0.14.17 in the application and evaluation environment.
- Clarify that learning auto-approval applies to generated skills; wiki lessons and
  prompt changes still require review.

## Validation and limits

- Command Center, help, tab-resume and session-launch checks: 43 passed. The new
  screen passed Ruff and Mypy; wide and narrow fixture renders were reviewed.
- Code Mode randomized tests: 127 passed with 91% coverage across the measured
  modules; all four negative controls failed the intended assertions.
- Factory review: 109 passed with extended test deadlines. Additional focused
  startup, memory, learning, SDK and async-agent checks are documented in
  `docs/github-issue-review.md`.
- The repository-wide test run and full Code Mode verifier did not finish cleanly
  because of test timeouts. Live provider, reboot and Docker behavior have not
  been verified. These results do not establish unconditional production readiness.

## Install or upgrade

```text
uv tool install --python 3.11 --force "novacode-cli==1.2.10"
```

With optional integrations, select the extras you need, for example:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice,agents-server,server,bedrock,computer]==1.2.10"
```

Existing PyPI tool installations can also use `uv tool upgrade novacode-cli`.
