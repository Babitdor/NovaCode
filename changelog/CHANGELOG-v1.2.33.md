# NovaCode 1.2.33

## Live Oracle reviewer progress

- Show each reviewer's running, complete, failed, timed-out or cancelled status
  with elapsed time, refreshed once per second. Completed timers stop.
- Show the finished reviewer count, elapsed round time and the two-minute
  timeout per reviewer. Individual results appear while other reviewers are
  still running; open reviewer threads also update when their results arrive.
- Open the compact reviewer panel when a new round starts. Keep it collapsible,
  size it to its contents and avoid labelling pending reviews as stale.
- Move Oracle Settings beside the Oracle toggle in the composer, keeping
  Code Mode, Oracle and Settings together when controls wrap.

## Validation

Oracle regressions cover reviewer timing, errors and cancellation, independent
completion, live thread updates, narrow terminal layouts, composer placement and
the real Code Mode graph's automatic fix/review cycle. No runtime dependency is
added. These indicators improve visibility; model response speed is unchanged.

See [Oracle mode](https://github.com/Babitdor/NovaCode/blob/v1.2.33/docs/oracle-mode.md)
for controls and settings.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.33
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Preserve any optional extras you use:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.33"
```
