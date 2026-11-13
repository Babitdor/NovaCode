# NovaCode 1.2.20

## Agent loop

- Make the repeat guard see `write_todos`. State updates return a `Command`
  rather than a `ToolMessage`, so they were never recorded and the repeat counter
  stayed at zero — the threshold could not be reached. One traced session showed
  ten byte-identical todo snapshots in a row with no guard message, while the
  same guard stopped a `read_file` loop at the fourth call. State updates now
  count toward the window, hashed on their content so the per-call tool call ID
  does not make every update look unique.
- Reword the Code Mode guidance for todo updates. "Once per model response" was
  read as an obligation for every response instead of "not several in parallel";
  25 of 26 calls in that trace were standalone snapshots with an empty text body.

## CI and tests

- Gate PyPI publishing on a successful CI run for the exact tagged commit, and
  split the offline regression suite into four shards driven by a manifest so a
  single slow test no longer decides the whole job.
- Keep the test process off Textual's Windows sleep thread pool. Textual's
  Windows sleep blocks a default-executor thread for the whole wait and
  `set_interval` uses it, so each armed timer pins a thread; with Nova's thirteen
  timers a 2-core runner pins all six workers and a 10 ms Textual sleep takes
  0.54 s. That stalled Windows regression runs until they were killed with no
  failing assertion. The same measurement after the change: no pinned threads and
  0.001 s. Runtime timing in installed builds is unchanged.
- Give the plan approval TUI tests the suite's TUI timeout, so a stall reports its
  own stack instead of a bare 10-second kill.

## Documentation

- Rewrite the README and add `docs/project-onboarding.md` and
  `docs/response-modes.md`.
- Add `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, and a `SECURITY.md` policy.

## Validation

The loop guard has regression tests covering state updates, content-only hashing,
and the annotation path. All thirteen CI jobs pass on the release commit, with the
Windows regression shard that previously stalled now green. Forty-four focused TUI
tests pass locally, and a desktop probe of the sleep change measured zero pinned
threads at a six-worker pool. Full cold-start performance was not re-benchmarked.
No new runtime dependency is required.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.20
uv tool upgrade novacode-cli
```

Optional extras:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.20"
```
