# NovaCode v1.0.75

## Maintenance

- Updated release metadata following the agent mention routing audit.
- Verified existing in-process, file-defined, and background-only agent invocation with 28 offline invocation and async-startup checks. Runtime routing remains unchanged in this release.
- Known limitations: dual-mode agents default to in-process delegation through plain `@name`; built-in async names require matching agent files for mention recognition.
