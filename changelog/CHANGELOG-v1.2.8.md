# NovaCode 1.2.8

## Reliability

- Prune superseded checkpoints at startup to limit long-lived session database
  growth. Large databases are vacuumed only when stale checkpoints were removed.
- Clear buffered terminal input before an update handoff and relaunch, preventing
  mouse reports from appearing as text in the resumed composer.
- Keep summary-preview state consistent when streamed output is discarded.

## Configuration and testing

- Support `NOVA_HOME` as an override for Nova's configuration directory.
- Isolate the test home before collection-time imports cache user data paths.
