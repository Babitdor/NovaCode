# NovaCode 1.2.75

## TUI smoothness: the last per-second full-screen layouts

Each of these fired a `Static.update()` — a full-screen layout pass — on a
timer while background work ran; all now repaint through the app's
layout-skipping `_paint` helper:

- **Per-tool elapsed timers** (`code_mode_panel.py`): one timer per running
  tool row, so N concurrent tools re-arranged the screen N times per second.
- **Background Tasks panel title** (`screens.py`): a full layout every second
  while the panel is open, even when the running count never changed.
- **Ralph progress card** (`ralph.py`): one layout per card per second.

Measured with the real harness (3 live background jobs driving the tasks-bar
ticker + two ~16 MB/s tool-output streams): key-press latency median
126–128 ms / p90 175–188 ms — at idle level (111–141 ms).
