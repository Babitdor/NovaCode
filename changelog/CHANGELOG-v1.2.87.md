# NovaCode 1.2.87

## Fixed

- The phantom background task: a detached command (Ctrl+B, or a foreground
  command auto-promoted to a server task) whose main process exits while a
  grandchild it spawned holds the inherited stdout pipe left the job stuck
  "running" forever — the Tasks panel showed it active, `wait_for_task`
  waited on it, and the auto-resume never fired, while nothing was actually
  running. The drain loop now notices the main process's exit and completes
  the job with its real exit code (measured: stuck forever → done in 0.6s).
