# NovaCode v1.0.57

## Changed

- Separated remote session labels, progress, plans, and recent tool activity into
  readable sections, with bounded previews of long plan items.
- Made long Telegram diff previews collapsible while retaining the patch text.
- Rendered Telegram tables as stacked records for phone screens, preserving
  links and inline formatting.
- Rendered Markdown task lists with completed and pending checkboxes.

## Fixed

- Made desktop sleep regression tests explicitly cover Windows, Linux, and macOS
  commands instead of assuming the host is Windows.

## Added

- GitHub Actions CI for Windows and Linux regressions, release metadata checks,
  focused lint, and Windows package installation and updater checks.

## Verification

- 116 release version, Telegram, remote status, streaming, and bridge tests passed;
  live Telegram client rendering remains unverified.
- All three hosted CI jobs passed after the portable desktop sleep test fix.
