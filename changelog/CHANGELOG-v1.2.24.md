# NovaCode 1.2.24

## Code Mode tool outlines

- Show recorded tool names, such as `read_file x4`, inside an outline that
  follows the active theme. Remove the repeated Code Mode label, total call
  count and completion suffix from the disclosure title.
- Retain exact JavaScript, full selected output, keyboard disclosures and
  failure details. Failed runs expand and use the theme's error color; active
  and focused runs use a stronger accent outline. Omitted outcome records
  remain documented inside the disclosure.

## Native command reports

- Replace `/cache` JSON in the local TUI with readable metrics and controls
  for enable/disable, clear and refresh. Keep operations scoped to the invoking
  workspace and assistant, including workspace transitions, and prevent
  overlapping mutations. Errors remain visible and refresh can recover.
- Open `/ui inspect` and `/lite-mode audit|blocks` as keyboard-accessible
  diagnostic trees with full selected values. UI mutations show brief status
  notifications rather than adding configuration JSON to chat.
- Group `/cache` with knowledge commands in the command guide. Headless cache
  commands retain structured JSON output through the shared cache controller.

## Knowledge cache usage

- Explain when the knowledge cache has not been used: ordinary file reads and
  shell calls do not populate it. `/cache` counts `knowledge_lookup` activity;
  provider prompt-cache usage is reported separately in `/cost`.
- Add Code Mode guidance for checking existing summaries and supplying relevant
  workspace-relative paths when an overview is useful. Exact source remains
  required for edits, quotations and verification.
- Verify the complete tool-to-command path with a scripted graph: a miss,
  generated overview and exact reuse update the same stored counters shown by
  `/cache`. This is not automatic caching of every read. Creating an overview
  uses a model call; live adoption and net billing savings are not measured.

## Validation

The final focused UI, cache, native Code Mode and version selection passed
58 tests. The broader related UI/cache selection passed 81 tests. Desktop and
40-column states were rendered headlessly and inspected, including theme
changes, keyboard access, cache error recovery and namespace isolation. A
Windows symlink test was skipped because this account cannot create symlinks.
New command-report regressions are included in platform CI. No new runtime
dependency is required.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.24
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Retain optional extras required by your
installation; for example:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.24"
```
