# NovaCode 1.2.84

## Fixed

- Concurrent Nova sessions no longer print a `--- Logging error ---`
  traceback to the terminal. Every Nova process appends boot diagnostics to
  `~/.nova/logs/startup.log`; when several run at once, the log's 64 KB
  rollover fails on Windows (another session holds the file open) and the
  logging machinery printed the whole `PermissionError` traceback to stderr.
  The startup trace is diagnostic: a failed rollover now drops the record
  silently and the log self-heals on the next successful rotation.
