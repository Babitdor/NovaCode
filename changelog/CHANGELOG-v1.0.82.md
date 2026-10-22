# NovaCode v1.0.82

## Fixes

- Apply the latest `/trace setup` configuration inside each running agent stream,
  overriding stale task-local tracing settings and selecting the current SDK
  client and project. Close nested streams together so early exits restore the
  tracing and usage contexts.
- Preserve an explicit tracing-off flag when rebuilding agents. Keep the enabled
  flag boolean and honor the project environment when no project override is given.
- Pass the configured endpoint as the SDK's `api_url` argument, and propagate
  the optional workspace ID.

## Improvements

- Add optional workspace, agent ID and agent environment fields to setup. The
  agent fields are trace metadata labels with the bundled SDK; project remains
  the destination. Keep CLI API-key entry masked and the setup form scrollable.
- Test runtime changes against stale async contexts and a real LangGraph graph
  containing model and tool calls. Trace uploads are intercepted locally with
  fake credentials; live authentication and ingestion are not exercised.
