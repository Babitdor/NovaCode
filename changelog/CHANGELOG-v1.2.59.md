# NovaCode 1.2.59

## Fixed: Background Tasks panel crashed on open

- v1.2.58's entry-point refactor wrapped `_open_tasks_panel` in a second
  `run_worker`. That method is decorated with `@work` — calling it already
  spawns its own worker and returns the Worker object, so passing that Worker
  as work raised `WorkerError: Unsupported attempt to run an async worker` the
  first time the panel was opened from the down-arrow gesture, Ctrl+B, a click
  on the tasks bar, or `/tasks`. `open_background_tasks` now calls the
  `@work`-decorated method directly.
- The v1.2.58 changelog's "dangling coroutines" root-cause claim was wrong —
  the pre-1.2.58 call sites worked fine via `@work`. Regression test added
  that exercises the real `@work`-decorated path end to end (the earlier tests
  monkeypatched the method away, which is why the crash shipped).
