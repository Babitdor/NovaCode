# NovaCode 1.0.14 — Background commands and release notices

- Give Ctrl+B priority over text editing shortcuts. Detach running shell, bash and execute commands even when the editor contains a draft; preserve that draft.
- Keep the Background Tasks panel single-instance during repeated Ctrl+B presses. Ignore repeated handoff requests so preserved drafts are not submitted accidentally.
- Register `!` commands with the background-task controller, retain their process and logs during handoff, and track completion/termination in the task panel.
- Remove detached commands from the foreground control list so Esc cannot kill work already moved to the background. Clean up shell process trees on cancellation.
- Display a `Ctrl+B · run in background` hint beneath running execution-tool output and hide it once the command finishes.
- Show the available release's version/title and a short changelog preview in the transcript. Add View more pages and official changelog links to automatic notices and `/update`.
- Fetch release metadata off the UI loop, deduplicate notices by revision, bound changelog content, and keep update notices usable when metadata is unavailable.
- Display animated retry progress (1/3, 2/3, 3/3) for failed provider requests before reporting a final error. Keep retries confined to model calls, stop on cancellation, and skip authentication, quota and context overflow failures.
- Add `general-purpose-async` for background coding, investigation, testing, research and analysis, with workspace file tools, shell/bash, web/docs tools, model retries and unattended approval enforcement.
- Prefer async delegation for general work as well as specialists. Return control to the user when delegated work is pending and continue on completion, keeping synchronous general-purpose as a fallback.
