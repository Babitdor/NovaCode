# NovaCode 1.2.76

## TUI smoothness: incremental output rows and off-loop parsing

Profiling the streaming path (`cProfile` over live tool panels + background
jobs) showed the remaining cost concentrated in two places; both are fixed:

- **Per-flush re-wrapping.** Every 50 ms flush re-wrapped *every retained
  line* — for a saturated 2000-line log of long build lines that re-created
  ~100,000 row tuples per panel on the UI loop. `OutputBody` now maintains
  its prepared rows incrementally (append/trim only), and ASCII lines wrap
  through an O(length/width) scan instead of rich's per-word divider.
  Unchanged retained lines never re-wrap.
- **ANSI parsing of the display tail.** The bounding, `Text.from_ansi` parse
  and tail split moved to a single coalescing worker thread ("stream
  painter"); the UI thread only paints ready content. Bursts coalesce per
  call with a bounded backlog, so a fast producer cannot starve input.

Also repaint-only (no full-screen layout) on their 1 Hz timers: per-tool
elapsed labels, the Background Tasks panel title, and Ralph progress cards.

Measured with the real harness (3 live tool panels with elapsed timers, 3
background jobs driving the tasks-bar ticker, ANSI-heavy output at up to
~16 MB/s per tool): the profile's `divide_line` hotspot (5.6 s of a 14.4 s
wall) drops to noise, and key-press latency under load sits near idle
(median ~190-220 ms vs 110-140 ms idle, p90 ~300-400 ms) on a machine that
was concurrently building other projects.
