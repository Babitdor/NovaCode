# NovaCode 1.0.12

- Show `NovaCode · <session name>` in the terminal window/tab title and update it when switching sessions or refreshing their names.
- Keep the main session tab and terminal title synchronized with the current session ID after `/resume` and `/clear`.
- Sanitize session names before writing terminal controls, skip terminal writes in headless mode, and avoid repeating unchanged titles.
- Add `/settings` → Performance → Live animation frame rate with persistent 15, 30 and 60 FPS choices, defaulting to 30 FPS. Apply changes immediately to status/tool spinners, shimmer and enabled Matrix Rain.
- Keep motion speed independent of refresh rate, limit background-state polling separately, and retain low-resource overrides and focus/visibility pauses.
