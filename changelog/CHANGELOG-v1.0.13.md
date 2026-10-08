# NovaCode 1.0.13

- Read the version shown by `nova --version`, startup ASCII art and the agent API from the shared release version constant instead of the stale hardcoded `1.0.6` value.
- Handle `nova --version` through the lightweight entry point without importing the agent runtime or starting onboarding.
- Include the current version beneath the compact startup portrait and use UTF-8-capable console output for `--version` on Windows.
