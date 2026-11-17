# NovaCode 1.2.32

## Oracle beside Code Mode, with visible controls and test evidence

- Move the Oracle toggle beside Code Mode in the composer. Keep the two toggles
  together when controls wrap, using the composer's actual width so centered
  layouts and narrow terminals retain access to both controls.
- Fix Oracle's compact button labels disappearing behind Textual's default
  borders. The status strip, Settings, reviewer-thread links and decision controls
  now use compact buttons, with enough room for the Settings label.
- Include captured test-tool output in each Oracle review. Reviewers receive
  bounded, redacted execution evidence along with the request, lead response,
  diff and changed file contents, instead of having to rely on the lead's claims.
- Add an end-to-end regression using the real agent graph, Code Mode JavaScript
  bridge, disposable Git workspace, Oracle fix loop and TUI renderer. The test
  checks that a low first-round score triggers a fix and fresh passing reviews.

## Validation

A live OpenCode `deepseek-v4.1-flash` run completed three Code Mode calls and
passed four checks in a disposable workspace. The independent reviewers scored
the result 9/10 and 8/10, meeting the target of 8. A separate controlled test
verified the automatic fix/review cycle from 5/10 to 9/10.

Automated regressions cover Oracle controls, composer placement at multiple
widths, captured test evidence, fresh scores and learning integration. Other
providers and token/speed savings have not been benchmarked. No new runtime
dependency is added.

See [Oracle mode](../docs/oracle-mode.md) for controls and settings.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.32
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Preserve any optional extras you use:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.32"
```
