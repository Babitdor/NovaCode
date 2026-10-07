# NovaCode 1.0.11

- Batch shell output and yield between pipe reads so keyboard input, cancellation and repainting can run during noisy commands. Handle long lines and split UTF-8 without `readline` overflow.
- Reuse wrap calculations for unchanged output lines, evict discarded lines from the cache, and parse only the retained display tail of large command results.
- Remove per-line forced scrolling from live shell, tool and background-agent output. Coalesced log paints follow the latest output when the user is already at the bottom.
- Bound shell/execute result capture while the command runs, instead of retaining all output until completion; keep live output streaming and the existing truncated-result notice.
- Cover cancellation while waiting for a shell process to exit after its output closes.
