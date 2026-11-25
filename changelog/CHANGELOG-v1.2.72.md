# NovaCode 1.2.72

## TUI responsiveness: cheaper ticks when work piles up

- **Transcript prune fast path** — the weight scan walked every transcript
  child's whole subtree on every prune pass (once per refresh cycle while
  lines stream; the stall watchdog historically recorded minutes of freeze in
  this path). When the child set is unchanged and the session is comfortably
  under both caps, the scan is skipped entirely and re-syncs on any new child
  or near a cap.
- **Subagent dock** — a collapsed dock no longer renders its O(rows) body per
  tick (the CSS hides it anyway), and finished rows are cached: the 1 Hz
  repaint over a dock holding up to 200 rows now rebuilds only running rows.
- **Background Tasks panel** — the 1 Hz refresh diffs each row's text and only
  touches the OptionList when a row actually changed; frozen completed rows no
  longer churn the list every second.
- **Home screen** — the 2 s dashboard tick no longer rebuilds all home art and
  detail lines when nothing changed (style, size, theme, identity, remount are
  the triggers); scene frames paint through the layout-skipping `_paint`
  helper instead of `Static.update`, which re-arranged the whole screen at
  6 fps; the modal fade repair runs at 20 Hz instead of 60 Hz with no visual
  difference.
