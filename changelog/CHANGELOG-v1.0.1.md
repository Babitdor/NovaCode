# Changelog - v1.0.1

## New Features

- **Prompt cache visibility**: The TUI status bar and `/context` now show the provider-reported cache hit rate and cumulative input tokens read from and written to the prompt cache. The compact status bar adapts the indicator to narrower terminals.

## Improvements

- Cache read, cache creation, and input-token counts are accumulated across model calls in a turn, so the hit rate reflects the whole session run rather than just the last response.
- Cache metrics remain hidden for providers that do not report prompt-cache usage; Nova does not estimate hits.
