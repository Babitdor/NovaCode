# NovaCode v1.0.86

## Fixes

- Generalize image-capability detection across routed model aliases using common
  vision model-name markers and provider-declared profile metadata.
- Prefer a recognized vision-capable model family over stale negative metadata
  from generic provider integrations while retaining explicit `/vision off`.

## Verification

- Cover routed aliases, common profile formats, stale negative profiles, and
  preservation of explicit user overrides.
