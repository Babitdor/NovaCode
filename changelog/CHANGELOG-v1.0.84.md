# NovaCode v1.0.84

## Fixes

- Clear LangSmith optional settings when the setup form is saved blank, so a
  stale workspace ID can be removed and does not keep breaking authentication.
- Reset LangSmith's cached client and environment after startup credential
  hydration, so tracing uses the saved key and settings in the agent stream.
- Clarify that Workspace ID is optional and should be left blank unless the
  LangSmith workspace requires it. The interactive setup accepts `clear` to
  remove an existing optional value.

## Verification

- Cover persisted startup configuration, stale-client reset, and clearing an
  invalid workspace value with offline tests.
- Verify a LangGraph chain and model trace can be sent and read back from
  LangSmith with the configured account.
